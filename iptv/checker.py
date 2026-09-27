"""效驗 + 測速(只走 IPv4)

流程:
  1. 協議只接受 http/https,主機須為公網 IPv4 或可解析出 A 記錄的網域
  2. 連線強制 AF_INET,記錄首包延遲 (TTFB)
  3. 若是 HLS:解析主清單 → 選最高碼率子清單(記錄解析度)→ 下載一個分片測速
     若是直連串流 (ts/flv/…):持續讀取 N 秒測速
  4. 依最低速度 / 最大延遲 / 最低解析度判定可用
"""
import asyncio
import ipaddress
import logging
import re
import socket
import time
from urllib.parse import urljoin, urlsplit

import aiohttp

log = logging.getLogger(__name__)

MAX_PLAYLIST = 2 * 1024 * 1024
MIN_BYTES = 8 * 1024
_BW = re.compile(r"BANDWIDTH=(\d+)")
_RES = re.compile(r"RESOLUTION=(\d+)x(\d+)")


class ProbeError(Exception):
    pass


async def _read_n(resp, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = await resp.content.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


def _is_m3u8(head: bytes) -> bool:
    return head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"#EXTM3U")


class Checker:
    def __init__(self, cfg, user_agent):
        c = cfg.get("check", {})
        self.concurrency = int(c.get("concurrency", 64))
        self.per_host = int(c.get("limit_per_host", 8))
        self.timeout = float(c.get("timeout", 8))
        self.duration = float(c.get("speed_duration", 4))
        self.min_speed = float(c.get("min_speed", 0) or 0)
        self.max_latency = float(c.get("max_latency", 0) or 0)
        self.min_height = int(c.get("min_height", 0) or 0)
        self.allow_private = bool(c.get("allow_private_ip", False))
        self.retries = int(c.get("retries", 1))
        self.ua = user_agent
        self._dns: dict[str, bool] = {}

    # ---------------- IPv4 判斷 ----------------
    def _public(self, ip) -> bool:
        if self.allow_private:
            return True
        return not (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_multicast or ip.is_reserved or ip.is_unspecified)

    async def is_ipv4_ok(self, url: str) -> bool:
        p = urlsplit(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        host = p.hostname
        if host in self._dns:
            return self._dns[host]
        try:
            ip = ipaddress.ip_address(host)
            ok = ip.version == 4 and self._public(ip)
        except ValueError:
            try:
                infos = await asyncio.get_running_loop().getaddrinfo(
                    host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
                ok = any(self._public(ipaddress.ip_address(i[4][0])) for i in infos)
            except (OSError, UnicodeError):
                ok = False
        self._dns[host] = ok
        return ok

    # ---------------- 主流程 ----------------
    async def run(self, streams):
        sem = asyncio.Semaphore(self.concurrency)
        conn = aiohttp.TCPConnector(family=socket.AF_INET, ssl=False, limit=self.concurrency,
                                    limit_per_host=self.per_host, ttl_dns_cache=600)
        total, done, t0 = len(streams), 0, time.monotonic()
        async with aiohttp.ClientSession(connector=conn, headers={"User-Agent": self.ua}) as session:
            async def worker(s):
                nonlocal done
                async with sem:
                    await self.check(session, s)
                done += 1
                if done % 200 == 0 or done == total:
                    log.info("測速進度 %d/%d (%.0fs)", done, total, time.monotonic() - t0)

            await asyncio.gather(*(worker(s) for s in streams))
        log.info("可用線路 %d / %d", sum(1 for s in streams if s.ok), total)

    async def check(self, session, s):
        s.ok = False
        if not await self.is_ipv4_ok(s.url):
            s.error = "非公網IPv4"
            return
        for _ in range(self.retries + 1):
            try:
                await asyncio.wait_for(self._probe(session, s), timeout=self.timeout * 3 + self.duration * 2)
                s.error = ""
                break
            except ProbeError as e:
                s.error = str(e)
                break
            except asyncio.TimeoutError:
                s.error = "逾時"
            except Exception as e:  # noqa: BLE001
                s.error = type(e).__name__
        else:
            return
        if s.error:
            return
        if s.speed is None or s.speed < self.min_speed:
            s.error = f"速度過低 {s.speed or 0:.0f}KB/s"
        elif self.max_latency and (s.latency or 0) > self.max_latency:
            s.error = f"延遲過高 {s.latency:.1f}s"
        elif self.min_height and s.height and s.height < self.min_height:
            s.error = f"解析度過低 {s.height}p"
        else:
            s.ok = True

    # ---------------- 探測細節 ----------------
    def _timeout(self):
        return aiohttp.ClientTimeout(total=None, sock_connect=self.timeout, sock_read=self.timeout)

    async def _read_for(self, resp):
        total, t0 = 0, time.monotonic()
        async for chunk in resp.content.iter_chunked(65536):
            total += len(chunk)
            if time.monotonic() - t0 >= self.duration:
                break
        return total, max(time.monotonic() - t0, 0.001)

    async def _probe(self, session, s):
        t0 = time.monotonic()
        async with session.get(s.url, headers=s.headers or None, timeout=self._timeout()) as r:
            if r.status >= 400:
                raise ProbeError(f"HTTP {r.status}")
            head = await _read_n(r, 4096)
            s.latency = round(time.monotonic() - t0, 3)
            if not head:
                raise ProbeError("空回應")
            if _is_m3u8(head):
                body = head + await _read_n(r, MAX_PLAYLIST)
                text, base = body.decode("utf-8", "ignore"), str(r.url)
            else:
                low = head[:512].lower()
                if b"<html" in low or b"<!doctype" in low:
                    raise ProbeError("非串流內容")
                total, el = await self._read_for(r)
                total += len(head)
                if total < MIN_BYTES:
                    raise ProbeError("資料量不足")
                s.speed = round(total / 1024 / el, 1)
                return
        s.speed, s.width, s.height = await self._probe_hls(session, base, text, s.headers)

    async def _get_text(self, session, url, headers):
        async with session.get(url, headers=headers or None, timeout=self._timeout()) as r:
            if r.status >= 400:
                raise ProbeError(f"HTTP {r.status} (子清單)")
            data = await _read_n(r, MAX_PLAYLIST)
            return data.decode("utf-8", "ignore"), str(r.url)

    async def _probe_hls(self, session, base, text, headers, w=0, h=0, depth=0):
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if any(ln.startswith("#EXT-X-STREAM-INF") for ln in lines):
            if depth >= 3:
                raise ProbeError("清單巢狀過深")
            best = None
            for i, ln in enumerate(lines):
                if not ln.startswith("#EXT-X-STREAM-INF"):
                    continue
                j = i + 1
                while j < len(lines) and lines[j].startswith("#"):
                    j += 1
                if j >= len(lines):
                    continue
                bw = int(m.group(1)) if (m := _BW.search(ln)) else 0
                rm = _RES.search(ln)
                cand = (bw, int(rm.group(1)) if rm else 0, int(rm.group(2)) if rm else 0,
                        urljoin(base, lines[j]))
                if best is None or cand[0] > best[0]:
                    best = cand
            if not best:
                raise ProbeError("無可用子清單")
            text2, base2 = await self._get_text(session, best[3], headers)
            return await self._probe_hls(session, base2, text2, headers,
                                         best[1] or w, best[2] or h, depth + 1)

        segs = [urljoin(base, ln) for ln in lines if not ln.startswith("#")]
        if not segs:
            raise ProbeError("清單無分片")
        seg = segs[-2] if len(segs) > 1 else segs[-1]
        async with session.get(seg, headers=headers or None, timeout=self._timeout()) as r:
            if r.status >= 400:
                raise ProbeError(f"HTTP {r.status} (分片)")
            total, el = await self._read_for(r)
        if total < MIN_BYTES:
            raise ProbeError("分片資料不足")
        return round(total / 1024 / el, 1), w, h
