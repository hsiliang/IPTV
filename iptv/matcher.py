"""頻道匹配:別名表 → tvg-id → iptv-org 名稱/別名 → 模糊比對
自動補齊:國家地區、分類、台標
資料來源:https://github.com/iptv-org/database (API: https://iptv-org.github.io/api/)
"""
import asyncio
import logging
import re
from collections import defaultdict

import yaml

from .fetcher import fetch_json
from .normalizer import clean_display, normalize

try:
    from rapidfuzz import fuzz, process
except ImportError:  # pragma: no cover
    process = None

log = logging.getLogger(__name__)
_DIGITS = re.compile(r"\D")
_CJK = re.compile(r"[㐀-鿿]")

try:  # 簡體判斷(可選,缺套件時退化成永遠判斷失敗,不影響其他邏輯)
    from opencc import OpenCC

    _s2t = OpenCC("s2t")

    def _looks_simplified(s: str) -> bool:
        return bool(s) and _s2t.convert(s) != s
except Exception:  # noqa: BLE001
    def _looks_simplified(s: str) -> bool:
        return False


# 頻道名稱裡的強特徵關鍵字 → 國家/地區,用來修正「頻道資料庫沒收錄、只能靠來源
# 猜測國家」導致的誤判。由上而下、由窄而寬,愈前面愈具體、優先權愈高。
_COUNTRY_KEYWORDS = [
    ("MO", re.compile(r"澳門|澳门|澳视|TDM", re.I)),
    # TVB(?!S):避免誤配到台灣的 TVBS(名稱含 TVB 子字串,但跟香港 TVB 是不同頻道)
    # R\.?T?\.?HK:同時涵蓋 RTHK 與部分來源打字漏字的 RHK(香港電台)
    ("HK", re.compile(r"TVB(?!S)|ViuTV|R\.?T?\.?HK|無綫|无线|翡翠台|明珠台|鳳凰衛視|凤凰卫视|香港開電視|香港开电视|HOY\s*TV",
                      re.I)),
    ("TW", re.compile(
        r"民視|民视|三立|中天|東森|东森|台視|台视|中視|中视|華視|华视|公視|公视|年代新聞|年代新闻|"
        r"非凡新聞|非凡新闻|壹電視|壹电视|寰宇新聞|寰宇新闻|鏡電視|镜电视|客家電視|客家电视|"
        r"原住民族|好消息電視|好消息电视|大愛|大爱|momo|TVBS", re.I)),
    ("CN", re.compile(r"CCTV|CGTN|央視|央视|衛視|卫视", re.I)),
]


def _country_from_name(*names) -> str:
    """依名稱裡的關鍵字判斷國家;都沒命中時,簡體字內容視為大陸頻道(TW/HK/MO 慣用繁體)"""
    for name in names:
        if not name:
            continue
        for cc, pat in _COUNTRY_KEYWORDS:
            if pat.search(name):
                return cc
    for name in names:
        if name and _looks_simplified(name):
            return "CN"
    return ""


def _prefer_display(name_disp: str, tvg_disp: str) -> str:
    """未命中別名/資料庫時的顯示名稱:同時有兩種來源時優先選含中文的那個"""
    if not name_disp:
        return tvg_disp
    if not tvg_disp or tvg_disp == name_disp:
        return name_disp
    if _CJK.search(tvg_disp) and not _CJK.search(name_disp):
        return tvg_disp
    return name_disp


class Matcher:
    def __init__(self, cfg):
        db = cfg.get("database", {})
        self.db_cfg = db
        self.fuzzy = int(db.get("fuzzy_threshold", 0) or 0) if process else 0
        self.prefer_db_name = bool(db.get("prefer_database_name", False))
        self.logo_template = db.get("logo_template") or ""
        self.pref = [c.upper() for c in db.get("preferred_countries") or []]

        self.alias: dict[str, str] = {}
        self.alias_country: dict[str, str] = {}
        self.by_id: dict[str, dict] = {}
        self.by_key: dict[str, list] = defaultdict(list)
        self.logos: dict[str, str] = {}
        self.countries: dict[str, dict] = {}
        self.categories: dict[str, str] = {}
        self._keys: list[str] = []
        self._fuzzy_cache: dict[str, str | None] = {}
        self._load_alias(db.get("alias_file"))

    # ---------------- 載入 ----------------
    def _load_alias(self, path):
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            log.warning("找不到別名檔 %s", path)
            return
        for country, entries in data.items():
            country = str(country).upper()
            for display, names in (entries or {}).items():
                display = str(display)
                for n in [display, *(names or [])]:
                    k = normalize(str(n))
                    if k:
                        self.alias.setdefault(k, display)
                        self.alias_country.setdefault(k, country)
        log.info("載入別名 %d 條", len(self.alias))

    async def load(self, session):
        db = self.db_cfg

        async def get(key):
            url = db.get(key)
            if not url:
                return []
            try:
                return await fetch_json(session, url, timeout=180, retries=2)
            except Exception as e:  # noqa: BLE001
                log.warning("資料庫 %s 下載失敗: %s", key, e)
                return []

        channels, logos, countries, categories = await asyncio.gather(
            get("channels"), get("logos"), get("countries"), get("categories")
        )
        for c in channels:
            cid = c.get("id")
            if not cid:
                continue
            self.by_id[cid] = c
            keys = {normalize(n) for n in [c.get("name", ""), *(c.get("alt_names") or [])]}
            for k in keys:
                if k:
                    self.by_key[k].append(c)
            if c.get("logo"):  # 舊版 API 將 logo 放在 channels.json
                self.logos[cid] = c["logo"]

        best = {}  # 新版 API:logos.json,優先無 feed、寬度較大的
        for lg in logos:
            cid, url = lg.get("channel"), lg.get("url")
            if not cid or not url:
                continue
            rank = (lg.get("in_use", True) is not False, lg.get("feed") is None, lg.get("width") or 0)
            if cid not in best or rank > best[cid][0]:
                best[cid] = (rank, url)
        for cid, (_, url) in best.items():
            self.logos[cid] = url

        self.countries = {c["code"].upper(): c for c in countries if c.get("code")}
        self.categories = {c["id"]: c.get("name", c["id"]) for c in categories if c.get("id")}
        self._keys = list(self.by_key)
        log.info("頻道資料庫:%d 頻道 / %d 台標 / %d 國家", len(self.by_id), len(self.logos), len(self.countries))

    # ---------------- 匹配 ----------------
    def _pick(self, cands, hint=""):
        pref = self.pref

        def rank(c):
            cc = (c.get("country") or "").upper()
            return (bool(c.get("closed")), cc != hint, pref.index(cc) if cc in pref else len(pref))

        return min(cands, key=rank)

    def _lookup(self, key, hint=""):
        if not key:
            return None
        if key in self.by_key:
            return self._pick(self.by_key[key], hint)
        if not self.fuzzy or len(key) < 3 or not self._keys:
            return None
        if key not in self._fuzzy_cache:
            hit = None
            for cand, _score, _ in process.extract(
                key, self._keys, scorer=fuzz.ratio, score_cutoff=self.fuzzy, limit=5
            ):
                # 數字必須一致,避免 CCTV1 ↔ CCTV11 這類誤配
                if _DIGITS.sub("", cand) == _DIGITS.sub("", key):
                    hit = cand
                    break
            self._fuzzy_cache[key] = hit
        hit = self._fuzzy_cache[key]
        return self._pick(self.by_key[hit], hint) if hit else None

    def apply(self, s, ch):
        s.channel_id = ch["id"]
        s.country = (ch.get("country") or "").upper()
        s.categories = list(ch.get("categories") or [])
        s.closed = bool(ch.get("closed"))
        s.nsfw = bool(ch.get("is_nsfw"))
        if self.prefer_db_name and not s.alias_hit:
            s.display = ch.get("name") or s.display
        if not s.logo:
            s.logo = self.logos.get(ch["id"], "")

    def match(self, s):
        raw_key = normalize(s.name) or normalize(s.tvg_name)
        tvg_key = normalize(s.tvg_name) if s.tvg_name else ""
        alias_key = raw_key if raw_key in self.alias else (tvg_key if tvg_key in self.alias else None)
        display = self.alias.get(alias_key) if alias_key else None
        if display:
            s.alias_hit, s.display, s.key = True, display, normalize(display)
        else:
            name_disp = clean_display(s.name)
            tvg_disp = clean_display(s.tvg_name) if s.tvg_name else ""
            s.display = _prefer_display(name_disp, tvg_disp) or tvg_disp or name_disp
            s.key = raw_key

        ch = None
        if s.tvg_id:
            ch = self.by_id.get(s.tvg_id.split("@")[0].strip())
        if ch is None:
            ch = self._lookup(s.key, s.country_hint)
        if ch is None and raw_key != s.key:
            ch = self._lookup(raw_key, s.country_hint)
        if ch:
            self.apply(s, ch)

        # 國家判斷優先權(由高到低):人工別名表 > 頻道資料庫 > 頻道名稱關鍵字/簡繁判斷 > 來源提示
        alias_country = self.alias_country.get(alias_key) if alias_key else None
        name_country = _country_from_name(s.display, s.name, s.tvg_name)
        s.country = alias_country or s.country or name_country or s.country_hint or ""

        if not s.logo and self.logo_template:
            s.logo = self.logo_template.format(name=s.display, key=s.key)
        return s
