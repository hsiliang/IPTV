"""輸出:m3u / txt / 分國家 m3u / report.json / README.md"""
import json
import logging
import os
import re
import shutil
from collections import defaultdict
from dataclasses import replace
from datetime import datetime
from urllib.parse import urlsplit

log = logging.getLogger(__name__)
OTHER = "其他"


def natural_key(s: str):
    return [(0, int(t)) if t.isdigit() else (1, t.lower()) for t in re.split(r"(\d+)", s) if t]


def _q(v: str) -> str:
    return (v or "").replace('"', "'")


class Grouper:
    def __init__(self, cfg, matcher):
        o = cfg.get("output", {})
        self.rules = []
        for r in o.get("group_rules") or []:
            self.rules.append((
                r["name"],
                re.compile(r["pattern"], re.I) if r.get("pattern") else None,
                {c.upper() for c in r.get("countries") or []},
                {c.lower() for c in r.get("categories") or []},
            ))
        self.mode = o.get("group_mode", "rules")
        self.fallback = o.get("group_fallback", "country")
        self.order = o.get("group_order") or []
        self.m = matcher

    def _country(self, cc):
        c = self.m.countries.get(cc)
        if not c:
            return cc or OTHER
        return f"{c.get('flag', '')} {c.get('name', cc)}".strip()

    def assign(self, ch):
        mode = self.mode
        if mode == "rules":
            for name, pat, ccs, cats in self.rules:
                if not (pat or ccs or cats):
                    continue
                if pat and not (pat.search(ch.display) or pat.search(ch.key)):
                    continue
                if ccs and ch.country not in ccs:
                    continue
                if cats and not (cats & {c.lower() for c in ch.categories}):
                    continue
                return name
            mode = self.fallback
        if mode == "country":
            return self._country(ch.country) if ch.country else OTHER
        if mode == "category":
            if not ch.categories:
                return OTHER
            return self.m.categories.get(ch.categories[0], ch.categories[0]).title()
        if mode == "source":
            return ch.streams[0].group or OTHER
        return str(mode)

    def sort_key(self, g):
        if g in self.order:
            return (0, self.order.index(g), "")
        names = [r[0] for r in self.rules]
        if g in names:
            return (1, names.index(g), "")
        if g == OTHER:
            return (3, 0, "")
        return (2, 0, g)


def _entry(ch, s, group) -> str:
    attrs = [f'tvg-id="{_q(ch.tvg_id)}"', f'tvg-name="{_q(ch.display)}"', f'tvg-logo="{_q(ch.logo)}"']
    if ch.country:
        attrs.append(f'tvg-country="{ch.country}"')
    attrs.append(f'group-title="{_q(group)}"')
    lines = [f'#EXTINF:-1 {" ".join(attrs)},{ch.display}']
    if s.headers.get("User-Agent"):
        lines.append(f'#EXTVLCOPT:http-user-agent={s.headers["User-Agent"]}')
    if s.headers.get("Referer"):
        lines.append(f'#EXTVLCOPT:http-referrer={s.headers["Referer"]}')
    lines.append(s.url)
    return "\n".join(lines)


def _write_m3u(path, sections, epg_url, updated):
    head = f'#EXTM3U x-tvg-url="{epg_url}" url-tvg="{epg_url}"' if epg_url else "#EXTM3U"
    lines = [head, f"# Updated: {updated}"]
    for group, chans in sections:
        for ch in chans:
            for s in ch.streams:
                lines.append(_entry(ch, s, group))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _write_txt(path, sections):
    lines = []
    for group, chans in sections:
        # txt 格式無法攜帶 UA/Referer,需要標頭的線路略過以確保可播
        rows = [f"{ch.display},{s.url}" for ch in chans for s in ch.streams if not s.headers]
        if rows:
            lines += [f"{group},#genre#", *rows, ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def write_failed(streams, grouper, out):
    """依國家分組寫出失敗線路清單(未通過效驗/測速)"""
    by_cc = defaultdict(list)
    for s in streams:
        if s.ok:
            continue
        label = grouper._country(s.country) if s.country else OTHER
        by_cc[label].append({
            "name": s.display or s.name,
            "url": s.url,
            "error": s.error or "",
        })
    for items in by_cc.values():
        items.sort(key=lambda x: natural_key(x["name"]))
    ordered = sorted(by_cc.items(), key=lambda kv: (-len(kv[1]), kv[0]))

    with open(os.path.join(out, "failed_by_country.json"), "w", encoding="utf-8") as f:
        json.dump({g: items for g, items in ordered}, f, ensure_ascii=False, indent=1)

    md = ["# 失敗線路清單(依國家分組)\n"]
    for g, items in ordered:
        md.append(f"## {g}({len(items)})\n")
        md += ["| 頻道 | 原因 | 網址 |", "|---|---|---|"]
        md += [f"| {i['name']} | {i['error']} | {i['url']} |" for i in items]
        md.append("")
    with open(os.path.join(out, "failed_by_country.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")

    return {g: len(items) for g, items in ordered}


def _filter_cn(sections, gfw_status, countries):
    """中國專用版:只保留 countries(預設台港澳陸)的頻道,依頻道國家判斷而不是分組名稱;
    再依 iptv/gfw_check.py 每週產生的網域封鎖狀態,排除「確定被中國大陸網路(GFW)封鎖」
    的線路(不確定/沒資料的一律保留,寧可多留而不是誤刪);頻道的線路全部被封鎖才整個
    頻道拿掉。回傳 None 代表既沒設定國家也沒有 GFW 資料,呼叫端不需要另外產生 live_cn 檔案。"""
    if not gfw_status and not countries:
        return None
    gfw_status = gfw_status or {}
    out = []
    for g, chans in sections:
        kept = []
        for ch in chans:
            if countries and ch.country not in countries:
                continue
            keep = [s for s in ch.streams if gfw_status.get((urlsplit(s.url).hostname or "").lower()) != "blocked"]
            if keep:
                kept.append(replace(ch, streams=keep))
        if kept:
            out.append((g, kept))
    return out


def write_outputs(channels, cfg, grouper, epg_url, stats, base, streams=None, gfw_status=None):
    o = cfg.get("output", {})
    out = o.get("dir", "output")
    os.makedirs(out, exist_ok=True)
    name = o.get("filename", "live")

    groups = defaultdict(list)
    for ch in channels:
        ch.group = grouper.assign(ch)
        groups[ch.group].append(ch)
    sections = [(g, sorted(groups[g], key=lambda c: natural_key(c.display)))
                for g in sorted(groups, key=grouper.sort_key)]
    updated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")

    files = [f"{name}.m3u"]
    _write_m3u(os.path.join(out, f"{name}.m3u"), sections, epg_url, updated)
    if o.get("txt", True):
        _write_txt(os.path.join(out, f"{name}.txt"), sections)
        files.append(f"{name}.txt")

    cn_countries = {c.upper() for c in o.get("cn_countries") or []}
    cn_sections = _filter_cn(sections, gfw_status, cn_countries)
    if cn_sections is not None:
        cn_name = f"{name}_cn"
        _write_m3u(os.path.join(out, f"{cn_name}.m3u"), cn_sections, epg_url, updated)
        files.append(f"{cn_name}.m3u")
        if o.get("txt", True):
            _write_txt(os.path.join(out, f"{cn_name}.txt"), cn_sections)
            files.append(f"{cn_name}.txt")
        stats["中國專用版頻道數(台港澳陸,已排除GFW封鎖)"] = sum(len(c) for _, c in cn_sections)

    if o.get("per_country", True):
        cdir = os.path.join(out, "countries")
        shutil.rmtree(cdir, ignore_errors=True)
        os.makedirs(cdir)
        by_cc = defaultdict(lambda: defaultdict(list))
        for g, chans in sections:
            for ch in chans:
                if ch.country:
                    by_cc[ch.country][g].append(ch)
        for cc, secs in sorted(by_cc.items()):
            _write_m3u(os.path.join(cdir, f"{cc.lower()}.m3u"), list(secs.items()), epg_url, updated)
        stats["國家數"] = len(by_cc)

    failed_by_country = {}
    if streams:
        failed_by_country = write_failed(streams, grouper, out)
        if failed_by_country:
            files.append("failed_by_country.md")
            stats["失敗數(依國家)"] = failed_by_country

    report = {
        "updated": updated,
        "stats": stats,
        "groups": {g: len(c) for g, c in sections},
        "channels": [{
            "name": ch.display, "group": ch.group, "country": ch.country,
            "tvg_id": ch.tvg_id, "logo": ch.logo,
            "streams": [{"url": s.url, "speed_kbps": s.speed, "latency_s": s.latency,
                         "resolution": f"{s.width}x{s.height}" if s.height else ""} for s in ch.streams],
        } for _, chans in sections for ch in chans],
    }
    with open(os.path.join(out, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)

    link = (lambda fn: f"{base}/{fn}") if base else (lambda fn: fn)
    md = [f"# IPTV 直播源\n\n更新時間:`{updated}`\n", "## 訂閱地址\n",
          "| 檔案 | 地址 |", "|---|---|"]
    md += [f"| {fn} | {link(fn)} |" for fn in files]
    if epg_url:
        md.append(f"| EPG | {epg_url} |")
    md += ["\n## 統計\n", "| 項目 | 數值 |", "|---|---|"]
    md += [f"| {k} | {v} |" for k, v in stats.items() if not isinstance(v, dict)]
    md += ["\n## 分組\n", "| 分組 | 頻道數 |", "|---|---|"]
    md += [f"| {g} | {len(c)} |" for g, c in sections]
    if failed_by_country:
        md += ["\n## 失敗線路(依國家,詳見 failed_by_country.md/json)\n",
               "| 國家 | 失敗數 |", "|---|---|"]
        md += [f"| {g} | {n} |" for g, n in failed_by_country.items()]
    with open(os.path.join(out, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    log.info("輸出完成:%d 分組 / %d 頻道 → %s", len(sections), len(channels), out)
