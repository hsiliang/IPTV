"""節目單 (XMLTV):下載多個來源 → 依頻道匹配 → 裁剪時間窗 → 合併輸出 epg.xml.gz
使用串流解析 (iterparse),可處理數百 MB 的大型 EPG 檔案。
"""
import asyncio
import copy
import gzip
import logging
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import aiohttp

from .normalizer import normalize

log = logging.getLogger(__name__)


def _t(s):
    """解析 XMLTV 時間:20260927120000 +0800"""
    if not s:
        return None
    parts = s.strip().split()
    base = parts[0][:14]
    tz = parts[1] if len(parts) > 1 else "+0000"
    try:
        return datetime.strptime(base + tz, "%Y%m%d%H%M%S%z")
    except ValueError:
        return None


class EPGBuilder:
    def __init__(self, cfg):
        e = cfg.get("epg", {})
        self.sources = e.get("sources") or []
        self.days = float(e.get("days", 3))
        self.timeout = int(e.get("timeout", 300))
        self.cache_dir = e.get("cache_dir", ".cache/epg")
        self.files = []     # [(來源序號, 路徑)]
        self.owner = {}     # epg 頻道 id → 來源序號(先到先得)
        self.chan = {}      # epg 頻道 id → <channel> 元素
        self.by_key = {}    # 正規化名稱 → epg 頻道 id

    async def download(self, session):
        os.makedirs(self.cache_dir, exist_ok=True)

        async def one(i, url):
            path = os.path.join(self.cache_dir, f"epg_{i}.xml")
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=self.timeout)) as r:
                    r.raise_for_status()
                    with open(path, "wb") as f:
                        async for chunk in r.content.iter_chunked(1 << 16):
                            f.write(chunk)
                log.info("EPG 下載完成 %s (%.1f MB)", url, os.path.getsize(path) / 1e6)
                return i, path
            except Exception as e:  # noqa: BLE001
                log.warning("EPG 下載失敗 %s: %s", url, e)
                return None

        res = await asyncio.gather(*(one(i, u) for i, u in enumerate(self.sources)))
        self.files = [r for r in res if r]

    @staticmethod
    def _open(path):
        with open(path, "rb") as f:
            magic = f.read(2)
        return gzip.open(path, "rb") if magic == b"\x1f\x8b" else open(path, "rb")

    def index(self):
        for i, path in self.files:
            n = 0
            try:
                with self._open(path) as f:
                    for _, el in ET.iterparse(f, events=("end",)):
                        if el.tag == "channel":
                            cid = el.get("id")
                            if cid and cid not in self.owner:
                                self.owner[cid] = i
                                self.chan[cid] = copy.deepcopy(el)
                                names = [d.text for d in el.findall("display-name") if d.text]
                                for nm in [*names, cid, cid.split(".")[0]]:
                                    k = normalize(nm)
                                    if k:
                                        self.by_key.setdefault(k, cid)
                                n += 1
                            el.clear()
                        elif el.tag == "programme":
                            break  # XMLTV 規範:channel 皆在 programme 之前
            except (ET.ParseError, OSError, EOFError) as e:
                log.warning("EPG 解析失敗 %s: %s", path, e)
            log.info("EPG 來源 #%d:%d 個頻道", i, n)

    def match(self, ch):
        if ch.channel_id and ch.channel_id in self.owner:
            return ch.channel_id
        for k in (ch.key, normalize(ch.display)):
            if k and k in self.by_key:
                return self.by_key[k]
        return None

    def write(self, path, wanted: dict) -> int:
        now = datetime.now(timezone.utc)
        lo, hi = now - timedelta(hours=6), now + timedelta(days=self.days)
        count = 0
        with gzip.open(path, "wt", encoding="utf-8") as out:
            out.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="iptv-auto-updater">\n')
            for cid, names in wanted.items():
                el = self.chan[cid]
                existing = {d.text for d in el.findall("display-name")}
                for nm in sorted(names):  # 加入我們的顯示名稱,方便以名稱比對的播放器
                    if nm not in existing:
                        d = ET.Element("display-name")
                        d.text = nm
                        el.insert(0, d)
                el.tail = "\n"
                out.write(ET.tostring(el, encoding="unicode"))
            for i, p in self.files:
                try:
                    with self._open(p) as f:
                        root = None
                        for ev, el in ET.iterparse(f, events=("start", "end")):
                            if root is None:
                                root = el
                                continue
                            if ev != "end" or el.tag != "programme":
                                continue
                            cid = el.get("channel")
                            if cid in wanted and self.owner.get(cid) == i:
                                st = _t(el.get("start"))
                                sp = _t(el.get("stop")) or st
                                if st and st <= hi and sp >= lo:
                                    el.tail = "\n"
                                    out.write(ET.tostring(el, encoding="unicode"))
                                    count += 1
                            root.clear()
                except (ET.ParseError, OSError, EOFError) as e:
                    log.warning("EPG 寫入時解析失敗 %s: %s", p, e)
            out.write("</tv>\n")
        log.info("EPG 輸出:%d 頻道 / %d 節目", len(wanted), count)
        return count
