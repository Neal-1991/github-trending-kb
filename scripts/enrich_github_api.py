"""用 GitHub API 补全快照缺失仓库的元数据(需要 GITHUB_TOKEN)。

产出: data/raw/repo_meta_api.jsonl (追加写,可断点续跑)
      data/raw/repo_gone.jsonl  (404/410,已删除/改名仓库)

模式区分(review P1-03):
  默认        backfill-missing: 只补 verified=0 且未抓过的核心仓库(repo_gone 跳过)
  --refresh-stale   刷新: TTL 过期的核心仓库全部重抓,含 repo_gone(可发现恢复/迁移/同名重建)
  --all(遗留别名)   等价于 backfill 的全量候选集,不做刷新

响应保存 repository id、node_id 与 canonical full_name(API 权威),请求名记为
requested_name,为 repo identity v2 迁移准备。
速率: 认证后 5000 次/小时,自动在配额耗尽时休眠到重置;429/ secondary 限流按
Retry-After 退避;5xx/网络错误重试。
"""
import argparse
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
DEFAULT_TTL_DAYS = 30

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "User-Agent": "trending-kb/0.1",
})


def load_fetched_at() -> dict:
    """full_name → 最近一次成功抓取时间(ISO)。"""
    fetched = {}
    if OUT.exists():
        for line in OUT.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                fetched[r.get("requested_name") or r.get("full_name")] = r.get("fetched_at")
    return fetched


def load_gone() -> set:
    gone = set()
    if GONE.exists():
        for line in GONE.read_text(encoding="utf-8").splitlines():
            if line.strip():
                gone.add(json.loads(line).get("full_name"))
    return gone


def wait_for_quota(r: requests.Response):
    remaining = int(r.headers.get("X-RateLimit-Remaining", "1"))
    if remaining <= 1:
        reset = int(r.headers.get("X-RateLimit-Reset", "0"))
        sleep_s = max(reset - time.time(), 0) + 5
        print(f"  rate limit exhausted, sleeping {sleep_s:.0f}s ...")
        time.sleep(min(sleep_s, 3700))


def _request_repo(full_name: str) -> requests.Response:
    delay = 5
    for attempt in range(1, 4):
        try:
            r = session.get(f"https://api.github.com/repos/{full_name}", timeout=30)
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(delay)
            delay *= 2
            continue
        if r.status_code in (403, 429) or 500 <= r.status_code < 600:
            retry_after = r.headers.get("Retry-After")
            wait = int(retry_after) if retry_after else delay
            print(f"  {full_name}: HTTP {r.status_code}, backoff {wait}s (attempt {attempt})")
            time.sleep(min(wait, 120))
            delay *= 2
            continue
        return r
    return r


def fetch(full_name: str):
    r = _request_repo(full_name)
    if r.status_code == 200:
        m = r.json()
        return {
            "full_name": m["full_name"],            # API canonical 名(权威)
            "requested_name": full_name,             # 请求名(可能已 rename/迁移)
            "repo_id": m["id"],                      # 稳定身份,identity v2 主键来源
            "node_id": m.get("node_id"),
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


def _due(name: str, fetched_at: dict, ttl_days: int, now_ts: float) -> bool:
    ts = fetched_at.get(name)
    if not ts:
        return True
    try:
        age = now_ts - datetime.fromisoformat(ts).timestamp()
        return age > ttl_days * 86400
    except ValueError:
        return True


def main():
    ap = argparse.ArgumentParser(description="GitHub API 元数据补全/刷新")
    ap.add_argument("--all", action="store_true",
                    help="(遗留)扩大候选到全部核心仓库,已抓取的仍跳过")
    ap.add_argument("--refresh-stale", action="store_true",
                    help="刷新 TTL 过期的核心仓库(含 repo_gone,可发现恢复/迁移)")
    ap.add_argument("--ttl-days", type=int, default=DEFAULT_TTL_DAYS)
    args = ap.parse_args()
    if not GITHUB_TOKEN:
        print("GITHUB_TOKEN 未配置(.env),无法调用 GitHub API。")
        sys.exit(1)

    conn = connect()
    if args.refresh_stale:
        names = [r["full_name"] for r in conn.execute(
            "SELECT full_name FROM repos WHERE core_days >= 1 ORDER BY core_days DESC")]
        fetched_at = load_fetched_at()
        now_ts = time.time()
        todo = [n for n in names if _due(n, fetched_at, args.ttl_days, now_ts)]
        mode = f"refresh-stale(ttl={args.ttl_days}d,含 repo_gone 复查)"
    else:
        base = conn.execute(
            "SELECT full_name FROM repos WHERE core_days >= 1 ORDER BY core_days DESC").fetchall()
        names = [r["full_name"] for r in base]
        if args.all:
            todo = names
        else:
            todo = [r["full_name"] for r in conn.execute(
                "SELECT full_name FROM repos WHERE verified = 0 AND core_days >= 1 "
                "ORDER BY core_days DESC")]
        gone = load_gone()
        done = set(load_fetched_at())
        todo = [n for n in todo if n not in done and n not in gone]
        mode = "backfill-missing"

    print(f"mode={mode}, to enrich: {len(todo)}")

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
            if args.refresh_stale:
                # 复查仍不存在才追加,避免重复记录
                already = any(json.loads(l).get("full_name") == name
                              for l in GONE.read_text(encoding="utf-8").splitlines()
                              if l.strip()) if GONE.exists() else False
                if not already:
                    with GONE.open("a", encoding="utf-8") as f:
                        f.write(json.dumps({"full_name": name, "status": r.status_code,
                                            "checked_at": datetime.now(timezone.utc).isoformat()}) + "\n")
            else:
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
