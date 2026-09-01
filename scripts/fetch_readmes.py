"""抓取仓库 README(raw.githubusercontent.com)。

产出: data/readmes/{owner}__{repo}.md (截断至 20KB)
幂等: 已存在的跳过;404(全候选名) 记入 data/readmes/_missing.txt。

状态区分(review P1-04):
  ok / skip / no_readme / rate_limited / temporary_error
- rate_limited、temporary_error 不写入 _missing.txt(非永久缺失),下次运行重试;
- 每线程独立 Session(requests.Session 非线程安全)。
默认范围: 进入过 Top10 的核心仓库 + 数据量允许时的长尾。
"""
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import README_DIR
from scripts.atomic_io import atomic_write_text
from scripts.db import connect

CANDIDATES = ["README.md", "readme.md", "Readme.md", "README.rst", "README.markdown", "README.txt"]
MAX_CHARS = 20000
MISSING_LOG = README_DIR / "_missing.txt"

_local = threading.local()


def _session() -> requests.Session:
    if not hasattr(_local, "session"):
        s = requests.Session()
        s.headers["User-Agent"] = "trending-kb/0.1"
        _local.session = s
    return _local.session


def fetch_one(full_name: str) -> str:
    safe = full_name.replace("/", "__")
    out = README_DIR / (safe + ".md")
    if out.exists():
        return "skip"
    rate_limited = temporary = False
    for name in CANDIDATES:
        url = f"https://raw.githubusercontent.com/{full_name}/HEAD/{name}"
        try:
            r = _session().get(url, timeout=30)
        except requests.RequestException:
            time.sleep(2)
            temporary = True
            continue
        if r.status_code == 200 and r.text.strip():
            out.write_text(r.text[:MAX_CHARS], encoding="utf-8")
            return "ok"
        if r.status_code == 404:
            continue
        if r.status_code == 429:
            time.sleep(10)
            rate_limited = True
    if rate_limited:
        return "rate_limited"
    if temporary:
        return "temporary_error"
    return "no_readme"


def persist_missing_status(results: dict[str, str]) -> None:
    """把永久缺失状态原子合并到 source；成功获取后可清除旧缺失标记。"""
    if not MISSING_LOG.exists() and not any(
            status == "no_readme" for status in results.values()):
        return
    missing = set()
    if MISSING_LOG.exists():
        missing.update(line.strip() for line in MISSING_LOG.read_text(
            encoding="utf-8").splitlines() if line.strip())
    for full_name, status in results.items():
        if status == "no_readme":
            missing.add(full_name)
        elif status in ("ok", "skip"):
            missing.discard(full_name)
    text = "".join(f"{name}\n" for name in sorted(missing))
    atomic_write_text(MISSING_LOG, text)


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    README_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    names = [r["full_name"] for r in conn.execute(
        "SELECT full_name FROM repos WHERE core_days >= 1 ORDER BY core_days DESC")]
    if limit:
        names = names[:limit]
    print(f"fetching READMEs for {len(names)} repos -> {README_DIR}")

    stats = {"ok": 0, "skip": 0, "no_readme": 0, "rate_limited": 0, "temporary_error": 0}
    results = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_one, n): n for n in names}
        for i, fut in enumerate(as_completed(futures), 1):
            res = fut.result()
            results[futures[fut]] = res
            stats[res] = stats.get(res, 0) + 1
            if i % 250 == 0:
                print(f"  {i}/{len(names)} {stats}")
    persist_missing_status(results)
    missing = [name for name, status in results.items() if status == "no_readme"]
    if missing:
        conn.executemany(
            "UPDATE repos SET profile_status='no_readme' "
            "WHERE full_name=? AND profile_status!='done'",
            ((name,) for name in missing),
        )
        conn.commit()
    conn.close()
    print(f"DONE {stats}")


if __name__ == "__main__":
    main()
