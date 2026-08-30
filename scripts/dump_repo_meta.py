"""从 ClickHouse playground 的 repos 快照(2022-07)导出趋势库仓库的元数据。

产出: data/raw/repo_meta_snapshot.csv
快照不含 2022-07 之后创建的仓库,这部分由 enrich_github_api.py 用 GitHub API 补全。
"""
import csv
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import CH_PLAYGROUND_URL, RAW_DIR

COLS = ("full_name, owner_type, description, fork, created_at, pushed_at, homepage, "
        "stargazers_count, forks_count, subscribers_count, language, archived, "
        "open_issues_count, license_key, topics, default_branch")

QUERY_TEMPLATE = """
SELECT {cols} FROM repos
WHERE full_name IN ({names})
FORMAT CSVWithNames
"""


def run_query(sql: str) -> str:
    for attempt in range(1, 5):
        # Windows 命令行有 32K 上限,查询体经临时文件传给 curl
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".sql", delete=False) as tf:
            tf.write(sql)
            tf_path = tf.name
        try:
            r = subprocess.run(
                ["curl", "-s", "-m", "300", CH_PLAYGROUND_URL, "--data-binary", "@" + tf_path],
                capture_output=True, text=True, encoding="utf-8",
            )
        finally:
            os.unlink(tf_path)
        text = r.stdout
        if text.lstrip().startswith('"full_name"'):
            return text
        print(f"  attempt {attempt}: bad response head: {text[:120]!r}")
        time.sleep(4 * attempt)
    raise RuntimeError("repos snapshot query failed")


def main():
    trends = RAW_DIR / "trends_gharchive.csv"
    names = sorted({row["repo"] for row in csv.DictReader(trends.open(encoding="utf-8"))})
    print(f"unique repos: {len(names)}")

    out = RAW_DIR / "repo_meta_snapshot.csv"
    found = 0
    chunk = 3000
    with out.open("w", encoding="utf-8", newline="") as f:
        writer = None
        for i in range(0, len(names), chunk):
            part = names[i:i + chunk]
            name_list = ",".join("'" + n + "'" for n in part)
            sql = QUERY_TEMPLATE.format(cols=COLS, names=name_list)
            t0 = time.time()
            text = run_query(sql)
            reader = csv.DictReader(text.splitlines())
            if writer is None:
                writer = csv.DictWriter(f, fieldnames=reader.fieldnames)
                writer.writeheader()
            n = 0
            for row in reader:
                writer.writerow(row)
                n += 1
            f.flush()
            found += n
            print(f"  chunk {i // chunk + 1}: +{n} ({time.time() - t0:.1f}s)")
            time.sleep(1)
    print(f"DONE meta rows={found}/{len(names)} -> {out}")


if __name__ == "__main__":
    main()
