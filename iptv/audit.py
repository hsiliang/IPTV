"""每週資料品質稽核:檢查頻道名稱/國家/台標是否正確。

流程:
  1. 用現有的採集+匹配流程(不含測速)取得目前完整頻道清單
  2. 規則式抓出「名稱關鍵字指向的國家 跟 目前標的國家不一致」與「大陸頻道
     但顯示名稱是拼音/英文」兩類可疑項目
  3. 若有設定 GEMINI_API_KEY,把抓到的可疑項目(數量有上限,不會每週把全部
     頻道都送出去)送給 Gemini 複核,決定正確國家 / 建議中文名 / 是否根本
     不是頻道(建議排除)
  4. 台標問題已經由 logos.py 在每次執行時自動處理(跳過共用同一網址的誤植台標、
     驗證網址並改用同頻道其他可用的候選),這裡只彙報有幾個頻道跳過了共用台標,不重複處理

Gemini 建議會直接寫回 config/alias.yaml / config/config.yaml(用文字層級插入,
不是整檔重新序列化,才不會清掉裡面大量手寫註解),由 workflow 開 PR 讓人工核准
後才會真正生效 —— 跟 discover.py 的作法一致。
"""
import argparse
import asyncio
import json
import logging
import os
import re
import socket
from datetime import datetime, timezone

import aiohttp
import yaml

from .fetcher import collect
from .logos import resolve_logos
from .main import apply_filters, build_channels, cap_candidates, load_config, merge_by_key
from .matcher import _CJK, _COUNTRY_KEYWORDS, Matcher

log = logging.getLogger("iptv.audit")

ALIAS_FILE = "config/alias.yaml"
CONFIG_FILE = "config/config.yaml"
REPORT_FILE = "audit_report.md"
MAX_PER_CATEGORY = 25  # 每類最多抽幾個送 Gemini,避免用量爆掉


def _country_hits(name: str):
    hits = []
    for cc, pat in _COUNTRY_KEYWORDS:
        if pat.search(name or ""):
            hits.append(cc)
    return hits


def find_country_mismatches(channels):
    out = []
    for ch in channels:
        hits = _country_hits(ch.display)
        if hits and ch.country not in hits:
            out.append(ch)
    return out


_STANDARD_CN_SUFFIX = re.compile(r"(頻道|频道|電視台|电视台|衛視|卫视|台)$")


def find_low_confidence_cn(channels):
    """country=CN,但既沒有頻道資料庫比對(channel_id 空)也沒有人工別名表命中(alias_hit),
    代表是靠關鍵字/簡體字這種較弱的訊號猜的,可能是「Fox新聞」「半島新聞」這種外國頻道
    剛好用簡體中文命名而被誤判,或是拿電視劇名稱直接當「頻道」的輪播源 —— 這類案例名稱
    本身不會跟任何國家關鍵字矛盾,find_country_mismatches() 抓不到,需要另外送 Gemini 複核。

    依「像不像正常地方台/衛視命名(結尾有 頻道/電視台/衛視/台)」與名稱長度排序,
    沒有標準結尾、名稱越短的排越前面 —— 這類最可能是劇名輪播源或外國頻道中文名,
    數量遠大於一般地方台,同一批候選裡優先讓真正可疑的被送去複核。"""
    out = [ch for ch in channels if ch.country == "CN" and not ch.channel_id and not ch.alias_hit]
    out.sort(key=lambda ch: (bool(_STANDARD_CN_SUFFIX.search(ch.display)), len(ch.display)))
    return out


def find_untranslated_cn(channels):
    # alias_hit=True 表示這個顯示名稱已經是人工判斷過的固定選擇(例如刻意保留 CCTV1 這種
    # 國際通用代號、不翻譯),不需要每週再重新建議一次
    return [ch for ch in channels
            if ch.country == "CN" and ch.display and not ch.alias_hit and not _CJK.search(ch.display)]


# 購物/輪播頻道常見特徵字;已知是正牌獨立頻道品牌的先排除,避免誤送 Gemini 複核
_SUSPICIOUS_NAME = re.compile(r"购物|購物|shopping|轮播|輪播|重播", re.I)
_KNOWN_REAL_CHANNELS = {
    "第一剧场", "第一劇場", "风云剧场", "風雲劇場", "怀旧剧场", "懷舊劇場", "都市剧场", "都市劇場",
    "家庭剧场", "家庭劇場", "军旅剧场", "軍旅劇場", "古装剧场", "古裝劇場", "欢笑剧场", "歡笑劇場",
    "欢乐剧场", "歡樂劇場", "CHC家庭影院", "CHC動作電影", "CHC影迷電影", "iHOT 爱院线", "iHOT 愛院線",
    "NewTV 欢乐剧场", "NewTV 歡樂劇場",
}


def find_suspicious_names(channels):
    return [ch for ch in channels if _SUSPICIOUS_NAME.search(ch.display) and ch.display not in _KNOWN_REAL_CHANNELS]


async def ask_gemini(session, api_key, model, items):
    """items: [{"name","country","issue"}]
    issue: country_mismatch | low_confidence_cn | needs_chinese_name | not_real_channel
    回傳對應順序的 [{"action": "country"|"chinese_name"|"exclude"|"skip", "value", "reason"}]
    """
    prompt = (
        "你是 IPTV 頻道資料品質審核員。以下是規則判斷不出來、需要你確認的頻道清單(JSON 陣列),"
        "每項有 name(目前顯示名稱)、country(目前標的國家/地區代碼)、issue(問題類型)。\n"
        "issue=country_mismatch:名稱關鍵字看起來屬於某個國家/地區,但目前標的不一樣,"
        "請判斷正確國家代碼(ISO 3166-1 alpha-2;台灣=TW,香港=HK,澳門=MO)。"
        "只有在你確定目前標的是錯的時候才回傳 action=country;如果你想給的國家代碼跟輸入的 country 欄位"
        "一樣(也就是其實沒錯),回傳 action=skip,不要重複確認。如果這其實根本不是電視頻道"
        "(廣告/測試資料/景點直播鏡頭之類),回傳 action=exclude。\n"
        "issue=low_confidence_cn:目前標成中國大陸(CN),但只是靠名稱含簡體字這種較弱的訊號猜的,"
        "沒有真正比對到頻道資料庫,可能是三種情況之一:\n"
        "  (a) 其實是外國媒體的中文頻道,例如半島電視台(Al Jazeera)是卡達(QA)、美國之音是"
        "美國(US)、BBC/DW/RFI/RFA 等國際媒體的中文頻道也都不是中國大陸頻道,即使名稱是簡體"
        "中文,因為那是外國媒體針對中文讀者/觀眾製作的內容,不代表頻道屬於中國大陸——"
        "如果你確定屬於這種情況,回傳 action=country 給正確的國家代碼。\n"
        "  (b) 名稱本身就是知名電視劇、電影、動畫、綜藝節目的劇名/番名(而不是地名、機構名稱"
        "或頻道品牌),例如「陳情令」「慶餘年」「雪豹」「狂飆」這種——這幾乎可以肯定是把單一"
        "節目直接當「頻道」的輪播源,不是正牌電視頻道,請果斷回傳 action=exclude,"
        "不要因為它同時也可能是某個地名的一部分就猶豫,你的中文知識庫應該足以認得知名作品"
        "名稱,不需要查得到官方資料才能判斷。\n"
        "  (c) 其他無法判斷但看起來合理的地名/機構名稱(例如 TVB J1、龍華xx、緯來xx"
        "這類頻道品牌,或看起來像縣市名稱的短名稱),回傳 action=skip,交給人工判斷,"
        "不要瞎猜國家、也不要因為看起來陌生就當作 exclude。\n"
        "issue=needs_chinese_name:這是中國大陸頻道,但名稱是拼音或英文,"
        "請給出這個頻道實際通用的正式中文名稱。如果這個名稱本身就是國際通用代號"
        "(例如 CCTV1、CCTV-8K、CGTN 這種頻道編號/代號,業界跟一般中文語境都直接沿用,不會另外翻譯),"
        "回傳 action=skip,不要硬翻成別的寫法。不確定正式名稱時也回傳 action=skip,不要瞎猜。\n"
        "issue=not_real_channel:名稱含有「購物」「輪播」「重播」等字樣,可能是電視購物頻道,"
        "或只是單一節目/景點/宣傳片段循環播放、不是一個真正獨立經營的電視頻道。"
        "如果你判斷這確實不是一個正常的電視頻道,回傳 action=exclude;"
        "如果這其實是知名的正牌頻道品牌(不確定也算),回傳 action=skip,不要亂排除。\n"
        "看起來合理但你不確定的一律回傳 action=skip,不要亂猜。\n\n"
        f"輸入:\n{json.dumps(items, ensure_ascii=False)}\n\n"
        "只回傳 JSON 陣列,順序對應輸入,每項包含 name/action/value/reason,"
        "action 是 country/chinese_name/exclude/skip 其中之一,value 是對應的國家代碼或中文名稱"
        "(action=exclude 或 skip 時 value 給空字串)。"
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "name": {"type": "STRING"},
                        "action": {"type": "STRING"},
                        "value": {"type": "STRING"},
                        "reason": {"type": "STRING"},
                    },
                    "required": ["name", "action", "value", "reason"],
                },
            },
        },
    }
    headers = {"x-goog-api-key": api_key}
    async with session.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as r:
        r.raise_for_status()
        data = await r.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise ValueError(f"Gemini 回應缺少內容({e}),可能被安全過濾攔截") from e
    return json.loads(text)


def _insert_alias_block(path, country, entries):
    """entries: [(display, [aliases])]。文字層級插入到指定國家區塊末尾,
    不整檔重新序列化,避免清掉現有註解。找不到該國家頂層 key 時就新增一個。"""
    if not entries:
        return
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    block = [f"  # ---------- 自動稽核建議({today}),待人工確認 ----------\n"]
    for display, aliases in entries:
        alist = ", ".join(aliases)
        block.append(f"  {display}: [{alist}]\n")

    start = next((i for i, ln in enumerate(lines) if ln.rstrip("\n") == f"{country}:"), None)
    if start is None:
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.append(f"{country}:\n")
        lines.extend(block)
    else:
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if lines[j].strip() and not lines[j].startswith((" ", "\t")):
                end = j
                break
        lines[end:end] = block
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def _insert_exclude_keywords(path, words):
    """在 config.yaml 的 filter.exclude_keywords 多行陣列收尾 "]" 前插入新項目。"""
    if not words:
        return
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    for i, ln in enumerate(lines):
        if ln.strip().startswith("exclude_keywords:") and ln.rstrip().endswith("["):
            for j in range(i + 1, len(lines)):
                if lines[j].strip() == "]":
                    new_lines = [f'    "{w}",\n' for w in words]
                    lines[j:j] = new_lines
                    break
            break
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


async def run(args):
    cfg = load_config(args.config)
    ua = cfg.get("fetch", {}).get("user_agent") or "Mozilla/5.0"
    conn = aiohttp.TCPConnector(family=socket.AF_INET, limit=16)
    async with aiohttp.ClientSession(connector=conn, headers={"User-Agent": ua}) as session:
        matcher = Matcher(cfg)
        streams, _ = await asyncio.gather(collect(session, cfg), matcher.load(session))
        for s in streams:
            matcher.match(s)
        merge_by_key(streams, matcher)
        streams, _ = apply_filters(streams, cfg)
        streams = cap_candidates(streams, int(cfg.get("filter", {}).get("max_candidates_per_channel", 8)))
        channels = build_channels(streams, cfg, checked=False)
        n_logo = (await resolve_logos(channels, matcher, ua, verify=False)).get("共用剔除", 0)

        limit = args.limit or MAX_PER_CATEGORY
        keyword_mismatches = find_country_mismatches(channels)
        seen_gid = {ch.gid for ch in keyword_mismatches}
        low_conf = [ch for ch in find_low_confidence_cn(channels) if ch.gid not in seen_gid]
        mismatches = (keyword_mismatches + low_conf)[:limit]
        untranslated = find_untranslated_cn(channels)[:limit]
        suspicious = find_suspicious_names(channels)[:limit]
        log.info("頻道數 %d;國家可疑 %d(關鍵字矛盾 %d + CN低信心 %d);待翻譯 %d;疑似非真頻道 %d;本次跳過共用台標 %d",
                  len(channels), len(mismatches), len(keyword_mismatches), len(low_conf),
                  len(untranslated), len(suspicious), n_logo)

        gemini_key = os.getenv("GEMINI_API_KEY", "")
        gemini_model = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")
        country_fixes, chinese_names, excludes, skipped = [], [], [], []

        if gemini_key and (mismatches or untranslated or suspicious):
            mismatch_gids = {ch.gid for ch in keyword_mismatches}
            items = (
                [{"name": ch.display, "country": ch.country,
                  "issue": "country_mismatch" if ch.gid in mismatch_gids else "low_confidence_cn"}
                 for ch in mismatches]
                + [{"name": ch.display, "country": ch.country, "issue": "needs_chinese_name"} for ch in untranslated]
                + [{"name": ch.display, "country": ch.country, "issue": "not_real_channel"} for ch in suspicious]
            )
            # 分批送(每批 80 個),避免單次 prompt 太大逾時或被截斷;某一批失敗不影響其他批次
            verdicts = []
            batch_size = 80
            batches = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]
            for i, batch in enumerate(batches):
                try:
                    verdicts += await ask_gemini(session, gemini_key, gemini_model, batch)
                except Exception as e:  # noqa: BLE001
                    log.warning("Gemini 複核第 %d/%d 批失敗(%s),這批維持原始可疑清單,不自動修改",
                                i + 1, len(batches), type(e).__name__)
            by_name = {v.get("name"): v for v in verdicts if isinstance(v, dict)}
            for ch in mismatches:
                v = by_name.get(ch.display)
                if not v or v.get("action") == "skip":
                    skipped.append((ch.display, "country 不確定"))
                elif v.get("action") == "exclude" and len(ch.display) >= 4:
                    excludes.append((ch.display, v.get("reason", "")))
                elif (v.get("action") == "country" and re.fullmatch(r"[A-Z]{2}", v.get("value") or "")
                      and v["value"] != ch.country):
                    country_fixes.append((ch.display, ch.country, v["value"], v.get("reason", "")))
                else:
                    skipped.append((ch.display, "country 建議與現況相同或格式異常,已略過"))
            for ch in untranslated:
                v = by_name.get(ch.display)
                if not v or v.get("action") != "chinese_name" or not v.get("value") or not _CJK.search(v["value"]):
                    skipped.append((ch.display, "沒有把握的中文名"))
                else:
                    chinese_names.append((ch.display, v["value"], v.get("reason", "")))
            for ch in suspicious:
                v = by_name.get(ch.display)
                if v and v.get("action") == "exclude" and len(ch.display) >= 4:
                    excludes.append((ch.display, v.get("reason", "")))
                else:
                    skipped.append((ch.display, "疑似非真頻道,但 Gemini 判斷是正牌頻道或沒把握"))

        if country_fixes:
            by_country = {}
            for name, _old, new_cc, _reason in country_fixes:
                by_country.setdefault(new_cc, []).append((name, [name]))
            for cc, entries in by_country.items():
                _insert_alias_block(ALIAS_FILE, cc, entries)
        if chinese_names:
            _insert_alias_block(ALIAS_FILE, "CN", [(zh, [en]) for en, zh, _r in chinese_names])
        if excludes:
            _insert_exclude_keywords(CONFIG_FILE, [name for name, _r in excludes])

    _write_report(len(channels), n_logo, mismatches, untranslated, country_fixes, chinese_names, excludes, skipped,
                  bool(gemini_key))
    return len(country_fixes) + len(chinese_names) + len(excludes)


def _write_report(n_channels, n_logo, mismatches, untranslated, country_fixes, chinese_names, excludes, skipped,
                   used_gemini):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"# 資料品質稽核報告({today})\n",
             f"頻道總數 {n_channels};本次跳過共用台標(誤植) {n_logo} 個頻道。",
             f"國家可疑 {len(mismatches)} 個、疑似拼音待翻譯 {len(untranslated)} 個"
             + ("(已送 Gemini 複核)" if used_gemini else "(未設定 GEMINI_API_KEY,僅規則檢查,未複核/未自動修改)") + "\n"]

    if country_fixes:
        lines.append("## ✅ 建議修正國家(已寫入 config/alias.yaml,待你確認)\n")
        lines += ["| 頻道 | 原國家 | 建議國家 | 原因 |", "|---|---|---|---|"]
        lines += [f"| {n} | {o} | {new} | {r} |" for n, o, new, r in country_fixes]
    if chinese_names:
        lines.append("\n## ✅ 建議中文名稱(已寫入 config/alias.yaml,待你確認)\n")
        lines += ["| 原名 | 建議中文名 | 原因 |", "|---|---|---|"]
        lines += [f"| {en} | {zh} | {r} |" for en, zh, r in chinese_names]
    if excludes:
        lines.append("\n## ✅ 建議排除(已寫入 config/config.yaml exclude_keywords,待你確認)\n")
        lines += ["| 名稱 | 原因 |", "|---|---|"]
        lines += [f"| {n} | {r} |" for n, r in excludes]
    if skipped:
        lines.append(f"\n## ⏭️ Gemini 沒把握,未變動({len(skipped)} 個)\n")
        lines += ["| 名稱 | 類型 |", "|---|---|"]
        lines += [f"| {n} | {t} |" for n, t in skipped[:30]]
    if not (country_fixes or chinese_names or excludes):
        lines.append("\n(本次沒有可自動套用的建議" + ("" if used_gemini else "——沒設定 GEMINI_API_KEY,只做規則檢查") + ")\n")
    lines.append(
        "\n---\n合併此 PR 前請自行檢查 `config/alias.yaml` / `config/config.yaml` 的異動內容是否合理;"
        "台標的部分不需要人工處理,共用台標每次執行 (`iptv/main.py`) 都會自動清除。"
    )
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description="每週資料品質稽核(國家/台標/中文名稱)")
    ap.add_argument("-c", "--config", default="config/config.yaml")
    ap.add_argument("--limit", type=int, default=None,
                     help=f"每類最多送幾個給 Gemini 複核(預設 {MAX_PER_CATEGORY};一次性大範圍稽核可調高)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    n = asyncio.run(run(args))
    log.info("本次套用建議 %d 項,詳見 %s", n, REPORT_FILE)


if __name__ == "__main__":
    main()
