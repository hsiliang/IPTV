"""頻道名稱清洗與正規化

clean_display(): 產生給人看的名稱(溫和清洗)
normalize():     產生比對用的鍵(激進清洗:全形→半形、繁→簡、去畫質標籤/符號、大寫)
"""
import re
import unicodedata

try:  # 繁簡轉換(可選)
    from opencc import OpenCC

    _t2s = OpenCC("t2s")
    _s2t = OpenCC("s2t")

    def to_simplified(s: str) -> str:
        return _t2s.convert(s)

    def to_traditional(s: str) -> str:
        # OpenCC 會把「台」轉成「臺」(如「台視」→「臺視」),但台灣當代實際使用(含官方/媒體)
        # 幾乎一律寫「台」,「臺」反而少見,轉回來才符合真實慣用寫法
        return _s2t.convert(s).replace("臺", "台")
except Exception:  # noqa: BLE001
    def to_simplified(s: str) -> str:
        return s

    def to_traditional(s: str) -> str:
        return s

_BRACKETS = re.compile(r"\[[^\]]*\]|\([^)]*\)|【[^】]*】|<[^>]*>")
_DISPLAY_TAIL = re.compile(
    r"(?i)[\s\-_]+(?:UHD|FHD|HD|SD|HEVC|H\.?26[45]|\d{3,4}[PI]|超高清|高清|超清|標清|标清|藍光|蓝光)\s*$"
)
_QUALITY = re.compile(
    r"(?i)(?<![A-Z0-9])(?:UHD|FHD|HD|SD|HEVC|H\.?26[45]|4K|8K|\d{3,4}[PI]|\d{2}FPS)(?![A-Z0-9])"
)
_CCTV = re.compile(r"^CCTV[\s\-_]*(4K|8K|\d{1,2}\+?)")
_PUNCT = re.compile(r"[\s\-_·•|:：.,，!！?？'\"/\\~]+")
_TAIL = re.compile(r"(?:UHD|FHD|HD|SD|HEVC|4K|8K|\d{3,4}[PI]|超高清|高清|超清|标清|蓝光|频道|台)$")


def clean_display(name: str) -> str:
    raw = (name or "").strip()
    s = unicodedata.normalize("NFKC", raw)
    s = _BRACKETS.sub(" ", s)
    while True:
        t = _DISPLAY_TAIL.sub("", s)
        if t == s:
            break
        s = t
    s = re.sub(r"\s+", " ", s).strip(" -_|·")
    return s or raw


def normalize(name: str) -> str:
    if not name:
        return ""
    full = to_simplified(unicodedata.normalize("NFKC", name)).upper().strip()
    s = _BRACKETS.sub(" ", full).strip()

    m = _CCTV.match(s)  # CCTV-1 综合 / cctv1高清 / CCTV 5+ → CCTV1 / CCTV5+
    if m:
        suffix = "欧洲" if "欧洲" in full else ("美洲" if "美洲" in full else "")
        return "CCTV" + m.group(1) + suffix

    s = _QUALITY.sub(" ", s)
    s = _PUNCT.sub("", s)
    while True:
        t = _TAIL.sub("", s)
        if t == s or not t:
            break
        s = t
    return s
