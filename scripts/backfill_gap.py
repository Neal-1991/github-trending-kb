"""一次性工具:把 config 区间内尚未落盘的历史日期增量补提取进 trends_gharchive.csv。

背景:playground github_events 样本密度随月份衰减,2026-03 起跌破可用阈值,
当年提取因此停在 2026-01-31。上游数据回升的月份可用本脚本增量补齐:
- 只追加 CSV 中尚不存在的日期(幂等,重复运行无副作用);
- 复用 extract_history 的窗口查询与完整性校验,任一窗口不完整即 fail closed;
- 全部成功后原子替换正式 CSV,失败时正式文件保持不变。
用法: python scripts/backfill_gap.py [--from YYYY-MM-DD]
  --from 只回填该日期(含)之后的缺失日期;缺省时从最早缺失日开始。
  早年的历史洞(如 2021-10)若上游已无法完整复现,会被 fail-closed 拒绝,
  可用 --from 跳过它们、聚焦近期缺口。
"""
import argparse
import csv
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import ARCH_END, ARCH_START, RAW_DIR, month_quality
from scripts.atomic_io import replace_file_with_retry
from scripts.extract_history import QueryError, fetch_window, windows


def expected_dates(start: str, end: str) -> set[str]:
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    return {(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days)}


def main() -> int:
    ap = argparse.ArgumentParser(description="增量回填历史重建榜缺失日期")
    ap.add_argument("--from", dest="from_date", default="",
                    help="只回填该日期(含)之后的缺失日期")
    args = ap.parse_args()

    out = RAW_DIR / "trends_gharchive.csv"
    with out.open(encoding="utf-8", newline="") as f:
        existing = list(csv.DictReader(f))
    have = {r["date"] for r in existing}
    polluted = sum(1 for r in existing if r["date"] == "date")
    if polluted:
        print(f"ERROR: CSV 内含 {polluted} 行表头污染,先修复源文件(git restore)再回填",
              file=sys.stderr)
        return 1
    missing = sorted(expected_dates(ARCH_START, ARCH_END) - have)
    if args.from_date:
        skipped_earlier = [d for d in missing if d < args.from_date]
        missing = [d for d in missing if d >= args.from_date]
        if skipped_earlier:
            print(f"跳过 {len(skipped_earlier)} 天 {args.from_date} 之前的缺失"
                  f"(如需处理请单独评估): {skipped_earlier[:6]}")
    if not missing:
        print(f"DONE 区间 {ARCH_START} ~ {ARCH_END} 内无待回填日期")
        return 0
    start = missing[0]
    print(f"待回填 {len(missing)} 天({start} 起),按窗口增量提取...")

    new_rows: list[tuple[str, str, str, str]] = []
    try:
        for w_start, w_end in windows(start, ARCH_END):
            rows = fetch_window(w_start, w_end)
            added = 0
            for row in rows:
                # 只跳过 CSV 已有的日期(重叠窗口);新日期的全部行都要保留
                if row["d"] in have:
                    continue
                ym = int(row["d"].replace("-", "")[:6])
                new_rows.append((row["d"], row["repo"], row["stars"], month_quality(ym)))
                added += 1
            print(f"{w_start} ~ {w_end}: 新增 {added} 行")
    except QueryError as exc:
        print(f"ERROR: 窗口不完整,按 fail-closed 中止,正式 CSV 未改动: {exc}",
              file=sys.stderr)
        return 1

    # 防御:窗口理论互不重叠,仍按 (date, repo) 去重避免意外双写
    seen: set[tuple[str, str]] = set()
    deduped = []
    for row in new_rows:
        key = (row[0], row[1])
        if key not in seen:
            seen.add(key)
            deduped.append(row)
    new_rows = deduped

    fd, tmp_name = tempfile.mkstemp(dir=str(out.parent), prefix=out.name + ".",
                                    suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["date", "repo", "stars", "quality"])
            # existing 是 DictReader 的 dict:csv.writer 对 dict 会写出键名,
            # 必须显式按列取值,否则整份 CSV 会被表头覆盖
            writer.writerows(
                [r["date"], r["repo"], r["stars"], r["quality"]] for r in existing)
            writer.writerows(new_rows)
        replace_file_with_retry(Path(tmp_name), out)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    print(f"DONE 新增 {len(new_rows)} 行 -> {out}(总计 {len(existing) + len(new_rows)} 行)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
