"""一次性工具:把 trends.jsonl 中尚无 canonical 快照的抓取日回填为快照。

canonical 快照成为每日榜主来源后,早期只写 trends.jsonl 的抓取日缺少可校验的
不可变快照。本脚本按日期回填,内容与 trends.jsonl 完全一致,不改动任何现有数据;
重复运行幂等(已有快照的日期跳过,同内容快照落盘时本身也是幂等返回)。
用法: python scripts/backfill_snapshots.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import DAILY_DIR
from scripts.snapshot_store import build_snapshot, iter_snapshots, load_day_records, save_snapshot


def main() -> int:
    existing = {snapshot["date"] for snapshot in iter_snapshots()}
    dates = set()
    trends_jsonl = DAILY_DIR / "trends.jsonl"
    if trends_jsonl.exists():
        for line in trends_jsonl.read_text(encoding="utf-8").splitlines():
            if line.strip():
                date = json.loads(line).get("date")
                if date:
                    dates.add(date)
    todo = sorted(d for d in dates if d not in existing)
    for date in todo:
        records, source = load_day_records(date)
        if records is None:
            print(f"跳过 {date}: 无可用数据")
            continue
        snap = build_snapshot(date, records)
        path = save_snapshot(snap)
        print(f"回填 {date}: snapshot_id={snap['snapshot_id'][:24]}... "
              f"lists={len(snap['lists'])} -> {path}")
    print(f"DONE 回填 {len(todo)} 天(共 {len(dates)} 个抓取日,已有快照 {len(existing)} 天)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
