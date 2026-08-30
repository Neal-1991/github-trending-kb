"""从 ClickHouse 公共 playground 的 github_events 提取历史每日星标增速 Top N。

产出: data/raw/trends_gharchive.csv  (date, repo, stars, quality)
区间: config.ARCH_START ~ ARCH_END;每月数据质量以 config.month_quality 标注。
用 curl 子进程发查询(requests 长连接复用在 playground 上会拿到无报错的残缺结果),
并按"天数×TopN"校验每个窗口的行数完整性,不足则重试。
"""
import csv
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import ARCH_END, ARCH_EXTRACT_TOP_N, ARCH_START, CH_PLAYGROUND_URL, RAW_DIR, month_quality

QUERY_TEMPLATE = """
WITH daily AS (
  SELECT toDate(e.created_at) d, e.repo_name repo, count() stars
  FROM github_events e
  WHERE e.event_type = 'WatchEvent'
    AND e.created_at >= '{start} 00:00:00'
    AND e.created_at < '{end} 00:00:00'
  GROUP BY d, repo
)
SELECT d, repo, stars FROM (
  SELECT *, row_number() OVER (PARTITION BY d ORDER BY stars DESC, repo) AS rn
  FROM daily
) WHERE rn <= {top}
ORDER BY d, rn
FORMAT CSVWithNames
"""

HEADER = '"d","repo","stars"'


def month_add(y: int, m: int, k: int):
    t = (y * 12 + m - 1) + k
    return t // 12, t % 12 + 1


def windows(start: str, end: str):
    sy, sm = (int(x) for x in start.split("-")[:2])
    ey, em = (int(x) for x in end.split("-")[:2])
    cur = sy * 12 + sm - 1
    stop = ey * 12 + em - 1
    while cur < stop:
        y, m = cur // 12, cur % 12 + 1
        ny, nm = month_add(y, m, 3)
        if (ny, nm) > (ey, em):
            ny, nm = ey, em
        yield f"{y:04d}-{m:02d}-01", f"{ny:04d}-{nm:02d}-01"
        cur = ny * 12 + nm - 1


def expected_rows(start: str, end: str, top: int) -> int:
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    return (d1 - d0).days * top


def run_query(sql: str) -> str:
    r = subprocess.run(
        ["curl", "-s", "-m", "300", CH_PLAYGROUND_URL, "--data-binary", sql],
        capture_output=True, text=True, encoding="utf-8",
    )
    return r.stdout


def fetch_window(w_start: str, w_end: str) -> list[dict]:
    sql = QUERY_TEMPLATE.format(start=w_start, end=w_end, top=ARCH_EXTRACT_TOP_N)
    want = expected_rows(w_start, w_end, ARCH_EXTRACT_TOP_N)
    for attempt in range(1, 5):
        text = run_query(sql)
        if not text.lstrip().startswith(HEADER):
            print(f"  attempt {attempt}: bad response head: {text[:120]!r}")
            time.sleep(4 * attempt)
            continue
        rows = list(csv.DictReader(text.splitlines()))
        if len(rows) >= want * 0.98:
            return rows
        print(f"  attempt {attempt}: got {len(rows)}/{want} rows, retrying")
        time.sleep(4 * attempt)
    print(f"  WARNING: {w_start} window incomplete after retries")
    return rows


def main():
    out = RAW_DIR / "trends_gharchive.csv"
    rows_total = 0
    with out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "repo", "stars", "quality"])
        for w_start, w_end in windows(ARCH_START, ARCH_END):
            t0 = time.time()
            rows = fetch_window(w_start, w_end)
            for row in rows:
                ym = int(row["d"].replace("-", "")[:6])
                writer.writerow([row["d"], row["repo"], row["stars"], month_quality(ym)])
            f.flush()
            rows_total += len(rows)
            print(f"{w_start} ~ {w_end}: {len(rows)} rows ({time.time() - t0:.1f}s)")
            time.sleep(1)
    print(f"DONE total={rows_total} -> {out}")


if __name__ == "__main__":
    main()
