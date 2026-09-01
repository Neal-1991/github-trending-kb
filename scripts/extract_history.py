"""从 ClickHouse 公共 playground 的 github_events 提取历史每日星标增速 Top N。

产出: data/raw/trends_gharchive.csv  (date, repo, stars, quality)
区间: config.ARCH_START ~ ARCH_END;每月数据质量以 config.month_quality 标注。

可靠性设计(原子 + fail closed):
- curl 检查 returncode 与 stderr,响应头校验,行数不足 98% 重试;
- 全部窗口成功前只写同目录临时文件;任一窗口最终失败 → 删除临时文件、退出非零,
  正式 CSV 保持不变;全部成功才原子替换正式文件。
"""
import csv
import os
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import (
    ARCH_END,
    ARCH_EXTRACT_TOP_N,
    ARCH_START,
    CH_PLAYGROUND_URL,
    RAW_DIR,
    month_quality,
)
from scripts.atomic_io import replace_file_with_retry

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
COMPLETE_RATIO = 0.98


class QueryError(RuntimeError):
    pass


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
    if r.returncode != 0:
        raise QueryError(f"curl 失败(rc={r.returncode}): {(r.stderr or '')[:200]}")
    return r.stdout


def fetch_window(w_start: str, w_end: str) -> list[dict]:
    """返回窗口行;最终仍不完整则抛 QueryError(由 main 决定整体失败)。"""
    sql = QUERY_TEMPLATE.format(start=w_start, end=w_end, top=ARCH_EXTRACT_TOP_N)
    want = expected_rows(w_start, w_end, ARCH_EXTRACT_TOP_N)
    rows: list[dict] | None = None
    last_head = ""
    for attempt in range(1, 5):
        text = run_query(sql)
        if not text.lstrip().startswith(HEADER):
            last_head = text[:120]
            print(f"  attempt {attempt}: bad response head: {last_head!r}")
            time.sleep(4 * attempt)
            continue
        rows = list(csv.DictReader(text.splitlines()))
        if len(rows) >= want * COMPLETE_RATIO:
            return rows
        print(f"  attempt {attempt}: got {len(rows)}/{want} rows, retrying")
        time.sleep(4 * attempt)
    if rows is None:
        raise QueryError(f"{w_start} 窗口 4 次响应均异常,最后响应头: {last_head!r}")
    raise QueryError(f"{w_start} 窗口不完整(最多 {len(rows)}/{want} 行),按 fail-closed 中止")


def main():
    out = RAW_DIR / "trends_gharchive.csv"
    rows_total = 0
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(out.parent), prefix=out.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
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
        replace_file_with_retry(Path(tmp_name), out)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    print(f"DONE total={rows_total} -> {out}")


if __name__ == "__main__":
    main()
