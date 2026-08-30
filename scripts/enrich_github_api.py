"""用 GitHub API 补全快照缺失仓库的元数据(需要 GITHUB_TOKEN)。

产出: data/raw/repo_meta_api.jsonl (追加写,可断点续跑)
      data/raw/repo_gone.jsonl  (404/410,已删除/改名仓库)
速率: 认证后 5000 次/小时,自动在配额耗尽时休眠到重置。
用法: python scripts/enrich_github_api.py [--all]  (默认只补 verified=0 的核心仓库)
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import GITHUB_TOKEN, RAW_DIR
from scripts.db import connect, upsert_repo

OUT = RAW_DIR / "repo_meta_api.jsonl"
GONE = RAW_DIR / "repo_gone.jsonl"

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "User-Agent": "trending-kb/0.1",
})


def load_done() -> set:
    done = set()
    for path in (OUT, GONE):
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    done.add(json.loads(line).get("full_name"))
    return done


def wait_for_quota(r: requests.Response):
    remaining = int(r.headers.get("X-RateLimit-Remaining", "1"))
    if remaining <= 1:
        reset = int(r.headers.get("X-RateLimit-Reset", "0"))
        sleep_s = max(reset - time.time(), 0) + 5
        print(f"  rate limit exhausted, sleeping {sleep_s:.0f}s ...")
        time.sleep(min(sleep_s, 3700))


def fetch(full_name: str):
    r = session.get(f"https://api.github.com/repos/{full_name}", timeout=30)
    if r.status_code == 200:
        m = r.json()
        return {
            "full_name": full_name,
            "description": m.get("description"),
            "language": m.get("language"),
            "topics": m.get("topics") or [],
            "homepage": m.get("homepage") or None,
            "license": (m.get("license") or {}).get("spdx_id"),
            "default_branch": m.get("default_branch"),
            "fork": m.get("fork", False),
            "archived": m.get("archived", False),
            "stars": m.get("stargazers_count"),
            "forks": m.get("forks_count"),
            "open_issues": m.get("open_issues_count"),
            "created_at": m.get("created_at"),
            "pushed_at": m.get("pushed_at"),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }, r
    return None, r


def main():
    if not GITHUB_TOKEN:
        print("GITHUB_TOKEN 未配置(.env),无法调用 GitHub API。")
        sys.exit(1)
    refresh_all = "--all" in sys.argv
    conn = connect()
    if refresh_all:
        names = [r["full_name"] for r in conn.execute(
            "SELECT full_name FROM repos WHERE core_days >= 1 ORDER BY core_days DESC")]
    else:
        names = [r["full_name"] for r in conn.execute(
            "SELECT full_name FROM repos WHERE verified = 0 AND core_days >= 1 ORDER BY core_days DESC")]
    done = load_done()
    todo = [n for n in names if n not in done]
    print(f"to enrich: {len(todo)} (skipped already-done: {len(names) - len(todo)})")

    ok = gone = 0
    for i, name in enumerate(todo, 1):
        result, r = fetch(name)
        wait_for_quota(r)
        if result:
            with OUT.open("a", encoding="utf-8") as f:
                f.write(json.dumps(result, ensure_ascii=False) + "\n")
            upsert_repo(conn, result, update_existing=True)
            ok += 1
        elif r.status_code in (404, 410):
            with GONE.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"full_name": name, "status": r.status_code}) + "\n")
            gone += 1
        else:
            print(f"  {name}: HTTP {r.status_code}, will retry next run")
        if i % 200 == 0:
            print(f"  {i}/{len(todo)} ok={ok} gone={gone}")
    print(f"DONE ok={ok} gone={gone}")


if __name__ == "__main__":
    main()
