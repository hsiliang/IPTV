"""解析 m3u / m3u8 與 DIYP/TVBox 的 txt 格式"""
import re

from .models import Stream

ATTR_RE = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')
MULTI_URL_RE = re.compile(r'#(?=(?:https?|rtmp|rtsp)://)')


def _split_url(raw: str):
    """處理 Kodi 風格:url|User-Agent=xxx&Referer=yyy"""
    url, _, opts = raw.partition("|")
    headers = {}
    if opts:
        for kv in opts.split("&"):
            k, _, v = kv.partition("=")
            k = k.strip().lower()
            if k in ("user-agent", "useragent"):
                headers["User-Agent"] = v.strip()
            elif k in ("referer", "referrer"):
                headers["Referer"] = v.strip()
    return url.strip(), headers


def _extinf_name(line: str) -> str:
    # 名稱在最後一個屬性引號之後的第一個逗號後面(屬性值內可能含逗號)
    q = line.rfind('"')
    start = q if q != -1 else line.find(":")
    comma = line.find(",", max(start, 0))
    return line[comma + 1:].strip() if comma != -1 else ""


def parse_m3u(text: str, source: str = ""):
    out, info, headers = [], None, {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            attrs = {k.lower(): v for k, v in ATTR_RE.findall(line)}
            info = {
                "name": _extinf_name(line) or attrs.get("tvg-name", ""),
                "tvg_id": attrs.get("tvg-id", ""),
                "tvg_name": attrs.get("tvg-name", ""),
                "logo": attrs.get("tvg-logo", ""),
                "group": attrs.get("group-title", ""),
            }
            headers = {}
            if attrs.get("http-user-agent"):
                headers["User-Agent"] = attrs["http-user-agent"]
            if attrs.get("http-referrer"):
                headers["Referer"] = attrs["http-referrer"]
        elif line.startswith("#EXTVLCOPT:"):
            k, _, v = line[11:].partition("=")
            k = k.strip().lower()
            if k == "http-user-agent":
                headers["User-Agent"] = v.strip()
            elif k in ("http-referrer", "http-referer"):
                headers["Referer"] = v.strip()
        elif line.startswith("#EXTGRP:") and info is not None and not info["group"]:
            info["group"] = line[8:].strip()
        elif line.startswith("#"):
            continue
        else:
            if info is None or not info["name"]:
                info = None
                continue
            url, h2 = _split_url(line)
            out.append(Stream(url=url, source=source, headers={**headers, **h2}, **info))
            info, headers = None, {}
    return out


def parse_txt(text: str, source: str = ""):
    """格式:
    分類名,#genre#
    頻道名,http://a.m3u8#http://b.m3u8
    """
    out, group = [], ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "//")) or "," not in line:
            continue
        name, _, rest = line.partition(",")
        name, rest = name.strip(), rest.strip()
        if rest.startswith("#genre#"):
            group = name
            continue
        for u in MULTI_URL_RE.split(rest):
            u = u.strip()
            if not u:
                continue
            url, h = _split_url(u)
            out.append(Stream(name=name, url=url, group=group, source=source, headers=h))
    return out


def parse_playlist(text: str, source: str = ""):
    head = text.lstrip("\ufeff \r\n\t")[:4096]
    if head.startswith("#EXTM3U") or "#EXTINF" in head:
        return parse_m3u(text, source)
    return parse_txt(text, source)
