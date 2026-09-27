"""採集:下載遠端 / 讀取本地來源並解析"""
import asyncio
import gzip
import json
import logging
import re

import aiohttp

from .parser import parse_playlist

log = logging.getLogger(__name__)


def decode_bytes(data: bytes) -> str:
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "ignore")


async def fetch_bytes(session, url, timeout=30, retries=2) -> bytes:
    last = None
    for attempt in range(retries + 1):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                r.raise_for_status()
                return await r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries:
                await asyncio.sleep(1.5 * (attempt + 1))
    raise last


async def fetch_text(session, url, timeout=30, retries=2) -> str:
    return decode_bytes(await fetch_bytes(session, url, timeout, retries))


async def fetch_json(session, url, timeout=60, retries=2):
    return json.loads(await fetch_text(session, url, timeout, retries))


# 從 iptv-org 風格的檔名推測國家:countries/tw.m3u、streams/hk_xxx.m3u
_CC_HINT = re.compile(r"/(?:countries|streams)/([a-z]{2})(?:_[\w-]+)?\.m3u8?$", re.I)


def _src_meta(src):
    """來源可寫成字串,或 {url: ..., country: TW, group: 分組}"""
    if isinstance(src, dict):
        url = src.get("url") or src.get("path") or ""
        return url, (src.get("country") or "").upper(), src.get("group") or ""
    m = _CC_HINT.search(src)
    return src, (m.group(1).upper() if m else ""), ""


async def _load_source(session, src, timeout, retries):
    url, country, group = _src_meta(src)
    if url.startswith(("http://", "https://")):
        text = await fetch_text(session, url, timeout, retries)
    else:
        with open(url, "rb") as f:
            text = decode_bytes(f.read())
    streams = parse_playlist(text, source=url)
    for s in streams:
        s.country_hint = country
        if group and not s.group:
            s.group = group
    return streams


async def collect(session, cfg):
    fc = cfg.get("fetch", {})
    timeout, retries = int(fc.get("timeout", 30)), int(fc.get("retries", 2))
    sources = cfg.get("sources") or []
    results = await asyncio.gather(
        *(_load_source(session, s, timeout, retries) for s in sources), return_exceptions=True
    )
    seen = {}
    for src, res in zip(sources, results):
        src = _src_meta(src)[0]
        if isinstance(res, Exception):
            log.warning("來源失敗 %s: %s", src, res)
            continue
        log.info("來源 %-70s %6d 條", src, len(res))
        for s in res:
            if not s.url or not s.name:
                continue
            old = seen.get(s.url)
            if old is None:
                seen[s.url] = s
            else:  # 相同網址:補齊缺少的欄位
                for f in ("tvg_id", "tvg_name", "logo", "group"):
                    if not getattr(old, f) and getattr(s, f):
                        setattr(old, f, getattr(s, f))
    log.info("網址去重後共 %d 條", len(seen))
    return list(seen.values())
