"""IPTV 直播源自動更新工具 — 入口

採集 → 匹配(名稱/國家/台標) → 過濾 → IPv4 效驗測速 → 擇優 → EPG → 輸出
"""
import argparse
import asyncio
import logging
import os
import socket
import sys
import time
from collections import Counter, defaultdict
from urllib.parse import urlsplit

import aiohttp
import yaml

from .checker import Checker
from .epg import EPGBuilder
from .fetcher import collect
from .health import update_and_prune
from .matcher import Matcher
from .models import Channel
from .output import Grouper, write_outputs

log = logging.getLogger("iptv")
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def load_config(path):
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    # 自動探索(見 iptv/discover.py + .github/workflows/discover-sources.yml)通過
    # 人工核准的候選來源,合併進正式抓取清單
    extra_path = os.path.join(os.path.dirname(path), "discovered_sources.yaml")
    if os.path.exists(extra_path):
        with open(extra_path, encoding="utf-8") as f:
            extra = yaml.safe_load(f) or {}
        extra_sources = extra.get("sources") or []
        cfg["sources"] = [*(cfg.get("sources") or []), *extra_sources]
        log.info("併入自動探索來源 %d 條 (%s)", len(extra_sources), extra_path)
    return cfg


def public_base(cfg):
    base = cfg.get("output", {}).get("public_base_url") or ""
    if not base and os.getenv("GITHUB_REPOSITORY"):
        branch = os.getenv("PUBLISH_BRANCH", "release")
        base = f"https://raw.githubusercontent.com/{os.environ['GITHUB_REPOSITORY']}/{branch}"
    return base.rstrip("/")


def merge_by_key(streams, matcher):
    """同名但只有部分線路帶 tvg-id 的情況,將其餘線路併入同一頻道"""
    key_to_id = {}
    for s in streams:
        if s.channel_id:
            key_to_id.setdefault(s.key, s.channel_id)
    for s in streams:
        if not s.channel_id and s.key in key_to_id:
            matcher.apply(s, matcher.by_id[key_to_id[s.key]])


def _reason(err: str) -> str:
    return err if err.startswith("HTTP") else err.split(" ")[0]


def apply_filters(streams, cfg):
    f = cfg.get("filter", {})
    countries = {c.upper() for c in f.get("countries") or []}
    keep_unmatched = f.get("keep_unmatched", True)
    inc = [k.lower() for k in f.get("include_keywords") or []]
    exc = [k.lower() for k in f.get("exclude_keywords") or []]
    exc_url = f.get("exclude_url_keywords") or []
    exc_groups = {g.lower() for g in f.get("exclude_groups") or []}
    out, reasons = [], Counter()
    for s in streams:
        p = urlsplit(s.url)
        host = p.hostname or ""
        text = f"{s.display} {s.name} {s.group}".lower()
        matched = bool(s.channel_id or s.alias_hit)
        if p.scheme not in ("http", "https"):
            reasons["協議不支援"] += 1
        elif ":" in host:
            reasons["IPv6位址"] += 1
        elif inc and not any(k in text for k in inc):
            reasons["未含關鍵字"] += 1
        elif s.group and s.group.lower() in exc_groups:
            reasons["排除分組"] += 1
        elif any(k in text for k in exc):
            reasons["排除關鍵字"] += 1
        elif any(k in s.url for k in exc_url):
            reasons["排除網址"] += 1
        elif f.get("exclude_nsfw", True) and s.nsfw:
            reasons["NSFW"] += 1
        elif f.get("exclude_closed", True) and s.closed:
            reasons["已停播"] += 1
        elif not matched and not keep_unmatched:
            reasons["未匹配"] += 1
        elif countries and s.country not in countries and (s.country or not keep_unmatched):
            reasons["國家不符"] += 1
        else:
            out.append(s)
    return out, reasons


def dedupe_shared_logos(channels):
    """同一個台標網址被多個不同頻道共用,幾乎都是來源資料本身貼錯(常見於自動採集工具:
    整批頻道複製貼上時漏改 tvg-logo)。寧可沒台標也不要顯示錯的,一律清掉重複使用的網址。"""
    by_logo = defaultdict(set)
    for ch in channels:
        if ch.logo:
            by_logo[ch.logo].add(ch.gid)
    bad = {url for url, gids in by_logo.items() if len(gids) > 1}
    n = 0
    for ch in channels:
        if ch.logo in bad:
            ch.logo = ""
            n += 1
    if n:
        log.info("清除疑似誤植的共用台標:%d 個頻道 / %d 個重複網址", n, len(bad))
    return n


def cap_candidates(streams, n):
    groups = defaultdict(list)
    for s in streams:
        groups[s.group_key].append(s)
    return [s for g in groups.values() for s in g[:n]]


def build_channels(streams, cfg, checked):
    max_per = int(cfg.get("output", {}).get("max_per_channel", 3))
    groups = defaultdict(list)
    for s in streams:
        if not checked or s.ok:
            groups[s.group_key].append(s)
    channels = []
    for gid, ss in groups.items():
        display = next((s.display for s in ss if s.alias_hit), ss[0].display)
        logo = next((s.logo for s in ss if s.logo), "")
        if checked:
            ss.sort(key=lambda s: (-(s.speed or 0), s.latency or 99))
        first = ss[0]
        channels.append(Channel(
            gid=gid, display=display, key=first.key, channel_id=first.channel_id, logo=logo,
            country=first.country, categories=first.categories, tvg_id=first.channel_id,
            streams=ss[:max_per],
        ))
    return channels


async def run(args):
    cfg = load_config(args.config)
    ua = cfg.get("fetch", {}).get("user_agent") or DEFAULT_UA
    ecfg = cfg.get("epg", {})
    epg = EPGBuilder(cfg) if ecfg.get("enabled") and ecfg.get("sources") else None
    stats, t0 = {}, time.monotonic()

    # 1. 採集 + 下載頻道資料庫 + 下載 EPG(並行)
    conn = aiohttp.TCPConnector(family=socket.AF_INET, limit=16)
    async with aiohttp.ClientSession(connector=conn, headers={"User-Agent": ua}) as session:
        matcher = Matcher(cfg)
        jobs = [collect(session, cfg), matcher.load(session)]
        if epg:
            jobs.append(epg.download(session))
        streams = (await asyncio.gather(*jobs))[0]
    stats["採集線路"] = len(streams)

    # 2. 匹配
    for s in streams:
        matcher.match(s)
    merge_by_key(streams, matcher)
    stats["匹配資料庫"] = sum(1 for s in streams if s.channel_id)
    stats["命中別名"] = sum(1 for s in streams if s.alias_hit)

    # 3. 過濾
    streams, reasons = apply_filters(streams, cfg)
    stats["過濾後"] = len(streams)
    stats["過濾原因"] = dict(reasons)
    streams = cap_candidates(streams, int(cfg.get("filter", {}).get("max_candidates_per_channel", 8)))
    if args.limit:
        streams = streams[: args.limit]
    stats["待測線路"] = len(streams)
    log.info("待測線路 %d 條", len(streams))

    # 4. 效驗 + 測速
    checked = cfg.get("check", {}).get("enabled", True) and not args.skip_check
    if checked:
        await Checker(cfg, ua).run(streams)
        stats["可用線路"] = sum(1 for s in streams if s.ok)
        stats["失敗原因"] = dict(Counter(_reason(s.error) for s in streams if not s.ok).most_common(15))
        health = update_and_prune(streams, cfg)
        if health.get("pruned"):
            stats["來源健康-已剔除"] = {u: n for u, n in health["pruned"]}
        if health.get("warnings"):
            stats["來源健康-警示"] = {u: n for u, n in health["warnings"]}

    # 5. 聚合擇優
    channels = build_channels(streams, cfg, checked)
    n_bad_logo = dedupe_shared_logos(channels)
    if n_bad_logo:
        stats["清除誤植台標"] = n_bad_logo
    stats["頻道數"] = len(channels)
    stats["輸出線路"] = sum(len(c.streams) for c in channels)
    min_ch = int(cfg.get("output", {}).get("min_channels", 1))
    if len(channels) < min_ch:
        log.error("頻道數 %d 低於下限 %d,放棄輸出以免覆蓋舊清單", len(channels), min_ch)
        return 1

    # 6. EPG
    out_dir = cfg.get("output", {}).get("dir", "output")
    os.makedirs(out_dir, exist_ok=True)
    base = public_base(cfg)
    epg_url = ecfg.get("x_tvg_url") or ""
    if epg:
        epg.index()
        wanted = defaultdict(set)
        for ch in channels:
            eid = epg.match(ch)
            if eid:
                ch.tvg_id = eid
                wanted[eid].add(ch.display)
        fname = ecfg.get("file", "epg.xml.gz")
        stats["EPG節目數"] = epg.write(os.path.join(out_dir, fname), wanted)
        stats["EPG頻道數"] = len(wanted)
        if not epg_url and base:
            epg_url = f"{base}/{fname}"

    # 7. 輸出
    stats["耗時秒"] = round(time.monotonic() - t0, 1)
    write_outputs(channels, cfg, Grouper(cfg, matcher), epg_url, stats, base,
                  streams=streams if checked else None)
    for k, v in stats.items():
        log.info("%-8s %s", k, v)
    return 0


def main():
    ap = argparse.ArgumentParser(description="IPTV 直播源自動更新工具")
    ap.add_argument("-c", "--config", default="config/config.yaml")
    ap.add_argument("--skip-check", action="store_true", help="跳過測速(除錯用)")
    ap.add_argument("--limit", type=int, default=0, help="只測前 N 條(除錯用)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
