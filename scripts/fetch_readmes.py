"""抓取仓库 README(无需鉴权,raw.githubusercontent.com 的 HEAD 伪分支)。

产出: data/readmes/{owner}__{repo}.md (截断至 20KB)
幂等:已存在的跳过;404 记入 data/readmes/_missing.txt。
默认范围:进入过 Top10 的核心仓库 + 数据量允许时的长尾。
"""
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import README_DIR, ROOT
from scripts.db import connect

CANDIDATES = ["README.md", "readme.md", "Readme.md", "README.rst", "README.markdown", "README.txt"]
MAX_CHARS = 20000
MISSING_LOG = README_DIR / "_missing.txt"

session = requests.Session()
session.headers["User-Agent"] = "trending-kb/0.1"


def fetch_one(full_name: str) -> str:
    safe = full_name.replace("/", "__")
    out = README_DIR / (safe + ".md")
    if out.exists():
        return "skip"
    for name in CANDIDATES:
        url = f"https://raw.githubusercontent.com/{full_name}/HEAD/{name}"
        try:
            r = session.get(url, timeout=30)
        except requests.RequestException:
            time.sleep(2)
            continue
        if r.status_code == 200 and r.text.strip():
            text = r.text[:MAX_CHARS]
            out.write_text(text, encoding="utf-8")
            return "ok"
        if r.status_code == 404:
            continue
        if r.status_code == 429:
            time.sleep(10)
            return "rate_limited"
    return "no_readme"


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    README_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    names = [r["full_name"] for r in conn.execute(
        "SELECT full_name FROM repos WHERE core_days >= 1 ORDER BY core_days DESC")]
    if limit:
        names = names[:limit]
    print(f"fetching READMEs for {len(names)} repos -> {README_DIR}")

    stats = {"ok": 0, "skip": 0, "no_readme": 0, "rate_limited": 0}
    missing = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_one, n): n for n in names}
        for i, fut in enumerate(as_completed(futures), 1):
            res = fut.result()
            stats[res] = stats.get(res, 0) + 1
            if res == "no_readme":
                missing.append(futures[fut])
            if i % 250 == 0:
                print(f"  {i}/{len(names)} {stats}")
    if missing:
        MISSING_LOG.write_text("\n".join(missing), encoding="utf-8")
    print(f"DONE {stats}")


if __name__ == "__main__":
    main()
