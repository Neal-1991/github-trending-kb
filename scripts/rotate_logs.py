"""日志保留/归档:data/daily 下的 JSONL 日志明细保留 N 天,过期行移入月度归档。

范围与行为:
- push_log.jsonl(推送记录)与 delivery_log.jsonl(投递事件流)逐行检查 "date" 字段;
- date 早于截止日(默认北京时间今天-90 天,ISO 日期字符串直接比较)的行,
  移入 data/daily/archive/YYYY-MM.jsonl(按行内 date 所在月份命名,
  两种日志的行可混在同一归档文件,每行自描述);
- 原文件以原子替换重写,只保留未过期行;归档合并按"整行 JSON 文本"去重,
  重复运行无任何变化(幂等);
- 输入文件不存在则跳过(delivery_log.jsonl 在首次通知成功前可能尚未产生);
- 归档不是删除:scripts/db.py 的 _import_sources 会把 live 文件与
  archive/*.jsonl 合并导入 SQLite push_log 表,归档行不从数据库消失。

写入顺序:先合并归档、后原子重写 live 文件;中途崩溃最多留下"归档与
live 短暂并存",下次运行按整行文本去重后收敛,不会丢数据。

用法:
  python scripts/rotate_logs.py           # 按默认 90 天保留
  python scripts/rotate_logs.py --days 30 # 覆盖保留天数
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import DAILY_DIR
from scripts.atomic_io import atomic_write_text

ARCHIVE_DIR_NAME = "archive"
TZ = ZoneInfo("Asia/Shanghai")
LOG_FILES = ("push_log.jsonl", "delivery_log.jsonl")


def cutoff_date(days: int) -> str:
    """截止日:date 早于该日(不含当日)的行过期。"""
    return (datetime.now(TZ).date() - timedelta(days=days)).isoformat()


def _month_of(date: str) -> str:
    return date[:7]  # YYYY-MM


def _split_by_cutoff(path: Path, cutoff: str) -> tuple[list[str], dict[str, list[str]]]:
    """把文件行按截止日分为(未过期行, {月份: 过期行})。

    行解析失败或缺少合法 date 字段时抛错(fail closed):宁可轮转失败,
    也不能静默决定无法判定的行的去留。
    """
    kept, expired = [], defaultdict(list)
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            date = json.loads(line)["date"]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            raise ValueError(f"{path.name}:{i} 无法判定过期(缺整行 JSON 或 date 字段): {e}") from e
        if not isinstance(date, str) or len(date) < 10:
            raise ValueError(f"{path.name}:{i} date 字段不是 ISO 日期: {date!r}")
        line = line.strip()
        if date < cutoff:
            expired[_month_of(date)].append(line)
        else:
            kept.append(line)
    return kept, expired


def _merge_into_archive(apath: Path, lines: list[str]) -> int:
    """把行按"整行 JSON 文本"去重后合并进月度归档,返回实际新增行数。

    无新增时不重写文件(重复运行零改动)。
    """
    existing = []
    if apath.exists():
        existing = [x for x in apath.read_text(encoding="utf-8").splitlines() if x.strip()]
    seen = set(existing)
    added = []
    for line in lines:
        if line not in seen:
            seen.add(line)
            added.append(line)
    if not added:
        return 0
    atomic_write_text(apath, "".join(x + "\n" for x in existing + added))
    return len(added)


def rotate_file(path: Path, cutoff: str, archive_dir: Path | None = None) -> dict | None:
    """单文件轮转:过期行入月度归档,原文件原子重写为未过期行。

    文件不存在返回 None(调用方按跳过处理)。
    """
    if not path.exists():
        return None
    archive_dir = archive_dir or path.parent / ARCHIVE_DIR_NAME
    kept, expired = _split_by_cutoff(path, cutoff)
    archived, months = 0, {}
    for month in sorted(expired):
        added = _merge_into_archive(archive_dir / f"{month}.jsonl", expired[month])
        archived += added
        if added:
            months[month] = added
    if expired:
        # 归档已落盘后才重写 live 文件;全部行过期时重写为空文件
        atomic_write_text(path, "".join(x + "\n" for x in kept))
    return {"kept": len(kept), "archived": archived, "months": months}


def rotate_all(daily_dir: Path | None = None, days: int = 90) -> dict:
    """轮转 data/daily 下所有受管日志,返回摘要(供 CLI 打印与测试断言)。"""
    daily_dir = Path(daily_dir) if daily_dir else DAILY_DIR
    cutoff = cutoff_date(days)
    files = {}
    for name in LOG_FILES:
        files[name] = rotate_file(daily_dir / name, cutoff, daily_dir / ARCHIVE_DIR_NAME)
    return {"cutoff": cutoff, "days": days, "files": files}


def print_summary(result: dict) -> None:
    print(f"[rotate] 截止日 {result['cutoff']}(保留 {result['days']} 天,早于该日的行归档)")
    total_archived = 0
    for name, info in result["files"].items():
        if info is None:
            print(f"[rotate] {name}: 文件不存在,跳过")
            continue
        total_archived += info["archived"]
        months = ", ".join(f"archive/{m}.jsonl +{n}" for m, n in sorted(info["months"].items()))
        detail = f"({months})" if months else "(无变化)"
        print(f"[rotate] {name}: 保留 {info['kept']} 行,归档 {info['archived']} 行{detail}")
    print(f"[rotate] 完成:本次新增归档 {total_archived} 行")


def main():
    ap = argparse.ArgumentParser(
        description="data/daily 日志保留/归档(明细保留 N 天,过期行移入 archive/YYYY-MM.jsonl)")
    ap.add_argument("--days", type=int, default=90,
                    help="保留天数(默认 90;date 早于 北京时间今天-N天 的行归档)")
    args = ap.parse_args()
    if args.days < 0:
        ap.error("--days 不能为负")
    print_summary(rotate_all(days=args.days))


if __name__ == "__main__":
    main()
