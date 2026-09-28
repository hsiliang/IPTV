"""自動探索 GitHub 上近期更新、star 數尚可的 IPTV/M3U repo,評估是否值得
加入抓取來源清單。

流程:
  1. 用關鍵字搜尋 GitHub repo,依 star 數 / 更新時間篩選候選
  2. 在候選 repo 裡找看起來像頻道清單的 .m3u/.m3u8/.txt 檔案
  3. 下載後做規則式檢查(頻道數、中文頻道名比例)
  4. 若有設定 GEMINI_API_KEY,再讓 Gemini 做最後判斷
  5. 通過的來源寫入 config/discovered_sources.yaml(不直接改 config.yaml),
     連同報告一起由 workflow 開 PR,人工核准後才會真正被抓取

state 檔 config/discovery_state.json 記錄每個 repo 上次檢查結果,避免每週
重複檢查同一批已接受/剛拒絕的 repo。
"""
import argparse
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import aiohttp
import yaml

from .fetcher import fetch_text
from .parser import parse_playlist

log = logging.getLogger("iptv.discover")

GITHUB_API = "https://api.github.com"
STATE_FILE = "config/discovery_state.json"
DISCOVERED_FILE = "config/discovered_sources.yaml"
REPORT_FILE = "discovery_report.md"

_CJK = re.compile(r"[㐀-鿿]")
_FILE_EXT = re.compile(r"\.(m3u8?|txt)$", re.I)
_NAME_HINT = re.compile(r"(live|iptv|zhibo|直播|result|channel|频道|頻道|tv)", re.I)

DEFAULT_RULES = {
    "keywords": ["iptv 直播源", "iptv m3u", "直播源 m3u8", "IPTV playlist"],
    "min_stars": 30,
    "max_age_days": 60,
    "min_entries": 30,
    "min_cjk_ratio": 0.3,
    "max_candidates_per_run": 8,
    "recheck_rejected_after_days": 90,
}


def load_yaml(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path, state):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


def known_urls(cfg):
    urls = set()
    for s in cfg.get("sources") or []:
        u = (s.get("url") if isinstance(s, dict) else s) or ""
        if u:
            urls.add(u.split("?")[0])
    return urls


def merge_rules(cfg):
    d = dict(DEFAULT_RULES)
    d.update(cfg.get("discovery") or {})
    return d


class GitHub:
    def __init__(self, session, token):
        self.session = session
        self.headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    async def _get(self, path, **params):
        async with self.session.get(f"{GITHUB_API}{path}", headers=self.headers,
                                     params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status == 403:
                body = await r.text()
                raise RuntimeError(f"GitHub API 拒絕(403),可能觸發速率限制: {body[:200]}")
            r.raise_for_status()
            return await r.json()

    async def search_repos(self, query, per_page=25):
        data = await self._get("/search/repositories", q=query, sort="updated", order="desc", per_page=per_page)
        return data.get("items", [])

    async def contents(self, full_name, path=""):
        p = f"/{path}" if path else ""
        try:
            return await self._get(f"/repos/{full_name}/contents{p}")
        except Exception as e:  # noqa: BLE001
            log.debug("讀取 %s%s 失敗: %s", full_name, p, e)
            return []


async def find_candidate_files(gh, full_name):
    """在 repo 根目錄(必要時往下一層)尋找看起來像頻道清單的檔案"""
    items = await gh.contents(full_name)
    if not isinstance(items, list):
        return []
    found, dirs = [], []
    for it in items:
        name = it.get("name", "")
        if it.get("type") == "file" and _FILE_EXT.search(name):
            found.append((name, it.get("download_url")))
        elif it.get("type") == "dir" and _NAME_HINT.search(name):
            dirs.append(it.get("path", name))
    for d in dirs[:3]:
        sub = await gh.contents(full_name, d)
        if isinstance(sub, list):
            for it in sub:
                name = it.get("name", "")
                if it.get("type") == "file" and _FILE_EXT.search(name):
                    found.append((f"{d}/{name}", it.get("download_url")))
    found.sort(key=lambda f: (0 if _NAME_HINT.search(f[0]) else 1, f[0]))
    return found[:5]


async def evaluate_file(session, name, url, rules):
    try:
        text = await fetch_text(session, url, timeout=20, retries=1)
    except Exception as e:  # noqa: BLE001
        return None, f"下載失敗: {type(e).__name__}"
    try:
        streams = parse_playlist(text, source=url)
    except Exception as e:  # noqa: BLE001
        return None, f"解析失敗: {type(e).__name__}"
    n = len(streams)
    if n < rules["min_entries"]:
        return None, f"頻道數過少({n})"
    # parse_txt() 對「名稱,網址」格式不會驗證第二欄是不是真的網址,像 xisohi/CHINA-IPTV 的
    # channel_mapping.txt 這種其實是「別名,標準名稱」對照表的檔案,兩欄都是頻道名不是網址,
    # 一樣會被解析成看似正常的 Stream,但這種來源實際上一條可用線路都貢獻不了
    with_url = sum(1 for s in streams if s.url.startswith(("http://", "https://", "rtmp://", "rtsp://")))
    if with_url / n < 0.5:
        return None, f"看起來不是真正的頻道清單(只有 {with_url}/{n} 條像網址,可能是名稱對照表之類的檔案)"
    cjk = sum(1 for s in streams if _CJK.search(s.name or s.tvg_name or ""))
    ratio = cjk / n if n else 0
    if ratio < rules["min_cjk_ratio"]:
        return None, f"中文頻道比例過低({ratio:.0%})"
    sample = [s.name or s.tvg_name for s in streams[:15]]
    return {"name": name, "url": url, "entries": n, "cjk_ratio": round(ratio, 2), "sample": sample}, ""


async def ask_gemini(session, api_key, model, repo, best):
    prompt = (
        "你是 IPTV 直播源品質審核員。以下是一個 GitHub repo 的資訊與它的頻道清單抽樣,"
        "請判斷是否值得加入台灣/香港/澳門/大陸中文 IPTV 頻道清單的自動抓取來源(每 6 小時會重新抓取一次)。\n"
        f"Repo: {repo['full_name']}\nstar 數: {repo['stargazers_count']}\n"
        f"最近更新: {repo['pushed_at']}\n說明: {repo.get('description') or ''}\n"
        f"候選檔案: {best['name']}(共 {best['entries']} 條,中文頻道名比例 {best['cjk_ratio']:.0%})\n"
        f"頻道抽樣:\n" + "\n".join(f"- {n}" for n in best["sample"]) + "\n\n"
        '只回傳 JSON,不要有其他文字,格式:{"accept": true 或 false, "reason": "一句話原因"}'
    )
    # 用 header 帶 API key,不放進 URL query string,避免它出現在例外訊息 / 任何記錄 URL 的地方
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        # 強制結構化 JSON 輸出,避免模型夾雜說明文字或 markdown code fence 導致解析失敗
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "accept": {"type": "BOOLEAN"},
                    "reason": {"type": "STRING"},
                },
                "required": ["accept", "reason"],
            },
        },
    }
    headers = {"x-goog-api-key": api_key}
    async with session.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as r:
        r.raise_for_status()
        data = await r.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise ValueError(f"Gemini 回應缺少內容({e}),可能被安全過濾攔截") from e
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 保底:responseSchema 理論上已強制純 JSON,萬一模型仍夾雜文字才會走到這裡
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ValueError(f"Gemini 回應非預期格式: {text[:200]!r}")
        return json.loads(m.group(0))


def write_discovered(accepted):
    disc_cfg = load_yaml(DISCOVERED_FILE)
    sources = list(disc_cfg.get("sources") or [])
    have = known_urls(disc_cfg)
    for repo, best, reason in accepted:
        if best["url"] in have:
            continue
        sources.append({
            "url": best["url"],
            "group": "自動探索補充",
            "_repo": repo["full_name"],
            "_stars": repo["stargazers_count"],
            "_checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "_reason": reason,
        })
    with open(DISCOVERED_FILE, "w", encoding="utf-8") as f:
        f.write("# 由 .github/workflows/discover-sources.yml 每週自動探索產生\n")
        f.write("# 結果會開 Pull Request,人工核准合併後才會被正式抓取(main.py 啟動時自動併入 sources)\n")
        f.write("# 底線開頭欄位(_repo/_stars/_checked_at/_reason)僅供人工參考,程式讀取時會忽略\n\n")
        yaml.safe_dump({"sources": sources}, f, allow_unicode=True, sort_keys=False)
    return sources


def write_report(accepted, rejected, checked):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"# 來源探索報告({today})\n", f"本次深入檢查了 {checked} 個 repo。\n"]
    lines.append("## ✅ 建議加入\n")
    if accepted:
        lines += ["| Repo | ★ | 檔案 | 頻道數 | 中文比例 | 原因 |", "|---|---|---|---|---|---|"]
        for repo, best, reason in accepted:
            lines.append(f"| [{repo['full_name']}]({repo['html_url']}) | {repo['stargazers_count']} | "
                          f"`{best['name']}` | {best['entries']} | {best['cjk_ratio']:.0%} | {reason} |")
    else:
        lines.append("(本次沒有新的候選來源)\n")
    if rejected:
        lines.append("\n## ❌ 檢查後未通過\n")
        lines += ["| Repo | ★ | 原因 |", "|---|---|---|"]
        for repo, reason in rejected:
            lines.append(f"| [{repo['full_name']}]({repo['html_url']}) | {repo['stargazers_count']} | {reason} |")
    lines.append(
        "\n---\n合併此 PR 前請自行檢查 `config/discovered_sources.yaml` 裡新增的網址是否合理、可信任"
        "(它們合併後會被排程每 6 小時自動抓取一次)。"
    )
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


async def run(args):
    cfg = load_yaml(args.config)
    rules = merge_rules(cfg)
    state = load_state(STATE_FILE)
    existing = known_urls(cfg) | known_urls(load_yaml(DISCOVERED_FILE))

    token = os.getenv("GITHUB_TOKEN", "")
    gemini_key = os.getenv("GEMINI_API_KEY", "")
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")
    cutoff = datetime.now(timezone.utc) - timedelta(days=rules["max_age_days"])
    recheck_cutoff_days = rules["recheck_rejected_after_days"]

    accepted, rejected, checked = [], [], 0

    async with aiohttp.ClientSession() as session:
        gh = GitHub(session, token)
        seen, candidates = set(), []
        for kw in rules["keywords"]:
            try:
                items = await gh.search_repos(f"{kw} in:name,description,topics")
            except Exception as e:  # noqa: BLE001
                log.warning("搜尋 %r 失敗: %s", kw, e)
                continue
            for repo in items:
                if repo["full_name"] not in seen:
                    seen.add(repo["full_name"])
                    candidates.append(repo)
        candidates.sort(key=lambda r: r["stargazers_count"], reverse=True)
        log.info("搜尋到 %d 個不重複候選 repo", len(candidates))

        for repo in candidates:
            if checked >= rules["max_candidates_per_run"]:
                break
            full = repo["full_name"]
            if repo["stargazers_count"] < rules["min_stars"]:
                continue
            try:
                pushed = datetime.fromisoformat(repo["pushed_at"].replace("Z", "+00:00"))
            except ValueError:
                continue
            if pushed < cutoff:
                continue
            st = state.get(full)
            if st:
                if st["status"] == "accepted":
                    continue
                checked_at = datetime.fromisoformat(st["checked_at"])
                if st["status"] == "rejected" and (datetime.now(timezone.utc) - checked_at).days < recheck_cutoff_days:
                    continue

            checked += 1
            log.info("檢查 %s (%d★, 更新於 %s)", full, repo["stargazers_count"], repo["pushed_at"])
            files = await find_candidate_files(gh, full)
            best, reason = None, "找不到符合格式的頻道清單檔案"
            for name, url in files:
                if not url or url.split("?")[0] in existing:
                    continue
                res, why = await evaluate_file(session, name, url, rules)
                if res:
                    best, reason = res, ""
                    break
                reason = why

            decision = {"checked_at": datetime.now(timezone.utc).isoformat()}
            if not best:
                decision.update(status="rejected", reason=reason)
                rejected.append((repo, reason))
            elif gemini_key:
                try:
                    verdict = await ask_gemini(session, gemini_key, gemini_model, repo, best)
                except Exception as e:  # noqa: BLE001
                    log.warning("Gemini 判斷失敗(%s),改用規則結果通過", type(e).__name__)
                    verdict = {"accept": True, "reason": f"規則篩選通過(Gemini 呼叫失敗: {type(e).__name__})"}
                if verdict.get("accept"):
                    reason = verdict.get("reason", "")
                    decision.update(status="accepted", file=best["url"], reason=reason)
                    accepted.append((repo, best, reason))
                else:
                    reason = verdict.get("reason", "AI 判斷不建議收錄")
                    decision.update(status="rejected", reason=reason)
                    rejected.append((repo, reason))
            else:
                reason = f"規則篩選通過({best['entries']} 頻道,中文比例 {best['cjk_ratio']:.0%})"
                decision.update(status="accepted", file=best["url"], reason=reason)
                accepted.append((repo, best, reason))

            state[full] = decision
            if best:
                existing.add(best["url"].split("?")[0])

    save_state(STATE_FILE, state)
    write_discovered(accepted)
    write_report(accepted, rejected, checked)
    return len(accepted)


def main():
    ap = argparse.ArgumentParser(description="探索新的 IPTV/M3U 直播源 repo")
    ap.add_argument("-c", "--config", default="config/config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    n = asyncio.run(run(args))
    log.info("本次新增候選來源 %d 個,詳見 %s / %s", n, DISCOVERED_FILE, REPORT_FILE)


if __name__ == "__main__":
    main()
