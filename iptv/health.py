"""追蹤每個來源(source url)近期執行是否還能貢獻可用頻道。

用途:
  - 自動探索(iptv/discover.py)加進來的來源,如果連續 N 次執行都测不到任何
    可用頻道(下載失敗 / 檔案變空 / 裡面線路全部失效),視為已經失效,自動從
    config/discovered_sources.yaml 移除,不用等人工發現。
  - 人工在 config.yaml 手動維護的來源不會被自動移除(尊重人工判斷),但如果
    同樣連續失效會列進 stats 的警示清單,方便你自己決定要不要拿掉。

只有在有做效驗/測速(checked=True)的執行才會呼叫,因為需要 Stream.ok。
"""
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone

import yaml

log = logging.getLogger(__name__)

HEALTH_FILE = "config/source_health.json"
DISCOVERED_FILE = "config/discovered_sources.yaml"

DEFAULT_RULES = {
    "enabled": True,
    "history_len": 20,                  # 每個來源保留最近幾次執行的紀錄
    "prune_after_consecutive_zero": 12, # 連續幾次「0 條可用頻道」就自動移除(僅限自動探索來源)
    "warn_after_consecutive_zero": 4,   # 連續幾次「0 條可用頻道」就在 stats 警示(含手動來源)
}


def _rules(cfg):
    d = dict(DEFAULT_RULES)
    d.update(cfg.get("source_health") or {})
    return d


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)


def _load_yaml(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _configured_urls(cfg):
    urls = set()
    for s in cfg.get("sources") or []:
        u = (s.get("url") if isinstance(s, dict) else s) or ""
        if u:
            urls.add(u)
    return urls


def update_and_prune(streams, cfg):
    """更新健康記錄,並剔除自動探索來源裡持續失效的項目。
    回傳 {"pruned": [(url, 連續次數), ...], "warnings": [(url, 連續次數), ...]}
    """
    rules = _rules(cfg)
    if not rules["enabled"]:
        return {}

    by_source = {u: [0, 0] for u in _configured_urls(cfg)}  # url -> [total, ok]
    for s in streams:
        src = s.source or "?"
        total_ok = by_source.setdefault(src, [0, 0])
        total_ok[0] += 1
        if s.ok:
            total_ok[1] += 1

    health = _load_json(HEALTH_FILE)
    now = datetime.now(timezone.utc).isoformat()
    for src, (total, ok) in by_source.items():
        rec = health.setdefault(src, {"history": [], "consecutive_zero": 0})
        rec["history"] = [*rec.get("history", []), {"at": now, "total": total, "ok": ok}][-rules["history_len"]:]
        rec["consecutive_zero"] = 0 if ok else rec.get("consecutive_zero", 0) + 1
    _save_json(HEALTH_FILE, health)

    pruned = []
    disc_cfg = _load_yaml(DISCOVERED_FILE)
    disc_sources = disc_cfg.get("sources") or []
    if disc_sources:
        kept = []
        for s in disc_sources:
            url = s.get("url") if isinstance(s, dict) else s
            streak = health.get(url, {}).get("consecutive_zero", 0)
            if streak >= rules["prune_after_consecutive_zero"]:
                pruned.append((url, streak))
                log.warning("自動剔除失效來源(連續 %d 次執行 0 條可用頻道): %s", streak, url)
            else:
                kept.append(s)
        if pruned:
            with open(DISCOVERED_FILE, "w", encoding="utf-8") as f:
                f.write("# 由 .github/workflows/discover-sources.yml 每週自動探索產生\n")
                f.write("# 結果會開 Pull Request,人工核准合併後才會被正式抓取(main.py 啟動時自動併入 sources)\n")
                f.write("# 底線開頭欄位(_repo/_stars/_checked_at/_reason)僅供人工參考,程式讀取時會忽略\n")
                f.write("# (本檔案也會被 iptv/health.py 自動剔除持續失效的項目)\n\n")
                yaml.safe_dump({"sources": kept}, f, allow_unicode=True, sort_keys=False)

    pruned_urls = {u for u, _ in pruned}
    warnings = sorted(
        ((src, rec["consecutive_zero"]) for src, rec in health.items()
         if rec["consecutive_zero"] >= rules["warn_after_consecutive_zero"] and src not in pruned_urls),
        key=lambda x: -x[1],
    )
    return {"pruned": pruned, "warnings": warnings}
