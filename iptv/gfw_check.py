"""每週檢查目前所有頻道線路的網域是否在中國大陸被 GFW(防火長城)封鎖。

資料來源:GreatFire.org 的公開 JSON API(https://en.greatfire.org/data)——
他們從美國(對照組)與中國大陸境內雙重視角實際量測,不是猜測或轉抄別人的清單。

GitHub Actions 本身沒有大陸網路出口,沒辦法真的從大陸內部測連線,這裡是查詢
GreatFire 既有的量測資料庫,涵蓋率取決於他們是否測過這個網域——IPTV 常見的
雜牌 CDN/動態網域多半沒被測過,查不到資料時一律當作「未封鎖」處理(不確定就
保留,而不是不確定就排除),只有查到「blocked」時才會被排除。

結果寫進 config/gfw_status.json,main.py 每次執行(每 6 小時)都會讀取這份
資料,額外輸出 live_cn.m3u / live_cn.txt 給大陸用戶使用。
"""
import argparse
import asyncio
import json
import logging
import os
import socket
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlsplit

import aiohttp

from .fetcher import collect
from .main import apply_filters, build_channels, cap_candidates, load_config, merge_by_key
from .matcher import Matcher

log = logging.getLogger("iptv.gfw_check")

STATUS_FILE = "config/gfw_status.json"
API = "https://en.greatfire.org/api/url/https/{host}"
CONCURRENCY = 4
DELAY = 0.3          # 每次查詢後的延遲(秒),對這個免費公開服務保持基本禮貌
TIMEOUT = 15


def _hostnames(channels):
    hosts = set()
    for ch in channels:
        for s in ch.streams:
            h = urlsplit(s.url).hostname
            if h:
                hosts.add(h.lower())
    return hosts


async def _check_one(session, sem, host):
    async with sem:
        try:
            async with session.get(API.format(host=host), timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
                data = await r.json() if r.status == 200 else {}
        except Exception as e:  # noqa: BLE001
            log.debug("查詢 %s 失敗: %s", host, type(e).__name__)
            data = {}
        finally:
            await asyncio.sleep(DELAY)
    return host, data.get("verdict") or "not enough data"


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

    hosts = _hostnames(channels)
    log.info("待檢查網域數:%d", len(hosts))

    sem = asyncio.Semaphore(CONCURRENCY)
    async with aiohttp.ClientSession(headers={"User-Agent": "iptv-auto-updater (+https://github.com)"}) as gf:
        results = await asyncio.gather(*(_check_one(gf, sem, h) for h in sorted(hosts)))

    now = datetime.now(timezone.utc).isoformat()
    status = {host: {"verdict": verdict, "checked_at": now} for host, verdict in results}
    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=1, sort_keys=True)

    counts = Counter(v["verdict"] for v in status.values())
    log.info("GFW 檢查完成:%s", dict(counts))
    return counts


def main():
    ap = argparse.ArgumentParser(description="檢查目前頻道線路網域是否被中國大陸網路(GFW)封鎖")
    ap.add_argument("-c", "--config", default="config/config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
