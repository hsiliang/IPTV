"""台標擇優:收集候選 → 剔除共用(誤植)與失效 → 選出最合適的一個

同一頻道通常有多條線路,每條都可能帶自己的 tvg-logo,再加上頻道資料庫,候選往往有好幾個;
過去只取第一個非空的網址,結果常常選到 404 / 已被 imgur 移除的圖,而同頻道其他來源的
台標其實是好的。這裡改成逐一驗證,跳過確定失效的候選。

網址判定(見 _classify / _probe):
  可用   = HTTP 200 且內容是圖片
  失效   = 404/410/403/401、200 但不是圖片(錯誤頁)、被導向 imgur 的 removed.png、連不上
  不確定 = 429 / 5xx / 逾時 —— 暫時性問題,照樣可用(upload.wikimedia.org 幾乎一定回 429,
           不能因此就把 iptv-org 資料庫的台標都丟掉)

擇優順序:
  1. 來源與資料庫的候選:跳過共用網址與確定失效的;檔名就是頻道名稱的優先
     (例如 .../龙华卡通.png 優先於 .../LTV8.png),其餘維持來源順序
  2. 上面都沒有可用的,才用備援模板依名稱組網址(只限中文名稱,這類台標庫都以中文命名;
     英文短名容易撞名,例如 DW.png 在 epg.112114.xyz 其實是 Animal Planet),且必須確定可用
"""
import asyncio
import logging
import os
import re
from collections import defaultdict
from urllib.parse import unquote, urlsplit

import aiohttp

from .normalizer import normalize, to_simplified

log = logging.getLogger(__name__)

# PNG / JPEG / GIF / WEBP(RIFF) / ICO / BMP
_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF", b"\x00\x00\x01\x00", b"BM")
_DEAD_STATUS = {401, 403, 404, 410}
_CJK = re.compile(r"[㐀-鿿]")


def _looks_image(ctype: str, head: bytes) -> bool:
    if head.startswith(_MAGIC):
        return True
    text = head.lstrip().lower()
    if text.startswith(b"<svg") or (text.startswith(b"<?xml") and "svg" in ctype):
        return True
    return ctype.startswith("image/") and not text.startswith((b"<!doctype", b"<html"))


def _classify(status: int, final_url: str, ctype: str, head: bytes):
    """True = 可用,False = 失效,None = 不確定"""
    if status == 200:
        if "imgur.com/removed" in final_url:  # imgur 已刪除的圖會 302 到這張「圖片不存在」佔位圖
            return False
        return _looks_image(ctype, head)
    if status in _DEAD_STATUS:
        return False
    return None


_MAX_HOST_TIMEOUTS = 5  # 同一主機連續逾時幾次後(視為整台掛掉),剩下的網址不再等待,直接當作不確定


async def _probe(session, host_sems, timeouts, url, timeout, retries):
    host = urlsplit(url).hostname or ""
    async with host_sems[host]:
        if timeouts[host] >= _MAX_HOST_TIMEOUTS:
            return None
        for attempt in range(retries + 1):
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout),
                                       allow_redirects=True) as r:
                    timeouts[host] = 0
                    if (r.status == 429 or r.status >= 500) and attempt < retries:
                        await asyncio.sleep(2 * (attempt + 1))
                        continue
                    head = await r.content.read(64)
                    return _classify(r.status, str(r.url), r.headers.get("Content-Type", "").lower(), head)
            except asyncio.TimeoutError:
                timeouts[host] += 1  # 逾時通常重試也一樣,不重試
                return None
            except (aiohttp.ClientConnectorError, aiohttp.ServerDisconnectedError, aiohttp.InvalidURL):
                if attempt >= retries:  # DNS 失敗 / 拒絕連線 / 防盜鏈直接斷線:播放器一樣抓不到
                    return False
            except (aiohttp.ClientError, ValueError):
                if attempt >= retries:
                    return None
        return None


async def verify_urls(urls, ua, concurrency=48, per_host=6, timeout=10, retries=1):
    """回傳 {url: True/False/None}"""
    urls = sorted(set(urls))
    if not urls:
        return {}
    # 每台主機各自限流(而不是先佔全域名額再排主機),慢主機才不會卡住其他主機的檢查
    host_sems = defaultdict(lambda: asyncio.Semaphore(per_host))
    timeouts = defaultdict(int)
    conn = aiohttp.TCPConnector(limit=concurrency, limit_per_host=per_host)
    async with aiohttp.ClientSession(connector=conn, headers={"User-Agent": ua}) as session:
        results = await asyncio.gather(*(_probe(session, host_sems, timeouts, u, timeout, retries) for u in urls))
    status = dict(zip(urls, results))
    n_dead = sum(1 for v in results if v is False)
    n_unknown = sum(1 for v in results if v is None)
    slow = sorted(h for h, n in timeouts.items() if n >= _MAX_HOST_TIMEOUTS)
    log.info("驗證台標網址 %d 個:可用 %d / 失效 %d / 不確定 %d%s",
             len(urls), len(urls) - n_dead - n_unknown, n_dead, n_unknown,
             f"(逾時過多略過:{', '.join(slow)})" if slow else "")
    return status


def collect_candidates(channels, matcher):
    """依序:各線路來源自帶的 tvg-logo(來源順序)→ 頻道資料庫"""
    for ch in channels:
        db = matcher.logos.get(ch.channel_id, "") if ch.channel_id else ""
        ch.logos = list(dict.fromkeys(u for u in [*ch.logos, db] if u))
        ch.db_logo = db


def template_candidates(ch, matcher):
    """備援模板依顯示名稱組出的網址;只用於中文名稱"""
    if not _CJK.search(ch.display):
        return []
    return [tpl.format(name=ch.display, name_sc=to_simplified(ch.display), key=ch.key)
            for tpl in matcher.logo_templates]


def shared_logo_urls(channels, templates=None):
    """被多個不同頻道當成候選的網址,幾乎都是來源資料貼錯(常見於自動採集工具:
    整批頻道複製貼上時漏改 tvg-logo,或兩個頻道用了同一張圖)。"""
    templates = templates or {}
    users = defaultdict(set)
    for ch in channels:
        for u in [*ch.logos, *templates.get(ch.gid, [])]:
            users[u].add(ch.gid)
    return {u for u, gids in users.items() if len(gids) > 1}


def _stem_key(url: str) -> str:
    """台標檔名(去副檔名)的比對鍵,例如 .../龙华卡通.png → 龙华卡通"""
    stem = os.path.splitext(unquote(urlsplit(url).path).rsplit("/", 1)[-1])[0]
    return normalize(stem) if stem else ""


def select_logos(channels, status=None, templates=None):
    """為每個頻道選出最終台標(寫入 ch.logo),回傳統計。

    共用網址一律跳過,唯一例外是頻道資料庫給這個頻道的台標(iptv-org 人工維護,
    同系列頻道共用同一張圖是正常的,例如 PBS Kids 各地區台)。
    status 為 None 時不做網路驗證(audit.py 只需要規則式結果)。
    templates:{gid: [模板網址]},只在沒有其他可用候選時才用,且必須驗證為可用。"""
    status = status or {}
    templates = templates or {}
    shared = shared_logo_urls(channels, templates)
    keys = {ch.key for ch in channels if ch.key}
    stats = defaultdict(int)
    for ch in channels:
        usable, skipped = [], set()
        for i, u in enumerate(ch.logos):
            stem = _stem_key(u)
            # 資料庫台標共用時通常是同系列頻道(可保留),但若檔名是清單裡另一個頻道的名稱就是資料庫本身
            # 標錯(例如和政电视台、High Channel TV 的台標都是 甘肃卫视.png);只看中文檔名,英文縮寫太容易撞名
            family = u == ch.db_logo and not (stem != ch.key and stem in keys and _CJK.search(stem))
            if u in shared and not family:
                skipped.add("共用剔除")
            elif status.get(u) is False:
                skipped.add("失效剔除")
            else:
                usable.append((stem != ch.key, i, u))
        for k in skipped:  # 以頻道數計
            stats[k] += 1
        if usable:
            ch.logo = min(usable)[2]
        else:
            ch.logo = next((u for u in templates.get(ch.gid, []) if u not in shared and status.get(u) is True), "")
            stats["模板補上"] += bool(ch.logo)
        stats["無台標"] += not ch.logo
    return dict(stats)


async def resolve_logos(channels, matcher, ua, verify=True):
    collect_candidates(channels, matcher)
    if not verify:
        return select_logos(channels)
    shared = shared_logo_urls(channels)
    status = await verify_urls({u for ch in channels for u in ch.logos if u not in shared or u == ch.db_logo}, ua)

    # 只有在來源/資料庫候選全部失效的頻道才去驗證備援模板
    # (模板是依名稱猜的網址,大多數頻道用不到,全部都驗會多出好幾千個請求)
    select_logos(channels, status)
    templates = {ch.gid: template_candidates(ch, matcher) for ch in channels if not ch.logo}
    templates = {gid: urls for gid, urls in templates.items() if urls}
    if templates:
        status.update(await verify_urls({u for urls in templates.values() for u in urls}, ua))
    stats = select_logos(channels, status, templates)
    log.info("台標擇優:%s", stats)
    return stats
