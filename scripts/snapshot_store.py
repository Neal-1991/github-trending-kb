"""每日榜单不可变快照:schema v2、canonical 单日文件、内容寻址 snapshot_id。

布局:
  data/daily/snapshots/YYYY/MM/YYYY-MM-DD.json          canonical(默认不可覆盖)
  data/daily/snapshots/history/YYYY/MM/<ISO时间>.json    刷新时归档的旧版本

snapshot_id = 对"去除 captured_at/snapshot_id 后的规范 JSON"计算 SHA-256,
因此内容相同则 id 相同,数据库/画像/日报/推送均可引用它做一致性追溯。
trends.jsonl 保留为兼容导出(由 capture 写入),不再是主写入入口;
读取端对没有快照文件的历史日期回退 trends.jsonl。
"""
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import DAILY_DIR
from scripts.atomic_io import atomic_write_json, atomic_write_text

SNAPSHOT_SCHEMA_VERSION = 2
SNAPSHOT_DIR = DAILY_DIR / "snapshots"
SNAPSHOT_HISTORY_DIR = SNAPSHOT_DIR / "history"
TZ = ZoneInfo("Asia/Shanghai")


class SnapshotExistsError(RuntimeError):
    """canonical 快照已存在且未显式要求刷新。"""


class SnapshotValidationError(ValueError):
    """快照结构或内容哈希不可信。"""


def canonical_content(snapshot: dict) -> dict:
    """参与 snapshot_id 计算与落盘的内容(剔除时间戳与 id 自身)。"""
    return {k: v for k, v in snapshot.items() if k not in ("captured_at", "snapshot_id")}


def compute_snapshot_id(snapshot: dict) -> str:
    payload = json.dumps(canonical_content(snapshot), ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_snapshot(snapshot: dict, *, expected_date: str | None = None) -> None:
    """读取前校验最小结构、日期和内容寻址哈希，损坏时 fail closed。"""
    if not isinstance(snapshot, dict):
        raise SnapshotValidationError("快照根节点必须是对象")
    if snapshot.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise SnapshotValidationError(
            f"不支持的快照 schema_version={snapshot.get('schema_version')!r}")
    date = snapshot.get("date")
    if not isinstance(date, str) or len(date) != 10:
        raise SnapshotValidationError("快照缺少合法 date")
    if expected_date and date != expected_date:
        raise SnapshotValidationError(f"快照日期 {date} 与文件日期 {expected_date} 不一致")
    lists = snapshot.get("lists")
    if not isinstance(lists, list) or not lists:
        raise SnapshotValidationError("快照 lists 必须是非空数组")
    seen = set()
    for item in lists:
        if not isinstance(item, dict) or not isinstance(item.get("list_type"), str):
            raise SnapshotValidationError("快照榜单缺少 list_type")
        if item["list_type"] in seen:
            raise SnapshotValidationError(f"快照榜单重复: {item['list_type']}")
        seen.add(item["list_type"])
        entries = item.get("entries")
        if not isinstance(entries, list):
            raise SnapshotValidationError(f"{item['list_type']} entries 必须是数组")
        if item.get("entry_count") != len(entries):
            raise SnapshotValidationError(f"{item['list_type']} entry_count 与 entries 不一致")
    expected_id = compute_snapshot_id(snapshot)
    if snapshot.get("snapshot_id") != expected_id:
        raise SnapshotValidationError("snapshot_id 与快照内容不一致")


def snapshot_path(date: str, base: Path | None = None) -> Path:
    root = base or SNAPSHOT_DIR
    y, m = date[:4], date[5:7]
    return Path(root) / y / m / f"{date}.json"


def build_snapshot(date: str, records: list[dict], *, source_version: str = "parser-v2") -> dict:
    """records: [{"list_type", "entries"}] → 完整快照结构(含 validation 摘要)。"""
    lists = []
    for rec in records:
        entries = rec["entries"]
        covered = sum(1 for e in entries if (e.get("stars_today") or 0) > 0)
        lists.append({
            "list_type": rec["list_type"],
            "entry_count": len(entries),
            "validation": {
                "valid": True,
                "stars_today_coverage": round(covered / len(entries), 4) if entries else 0.0,
            },
            "entries": entries,
        })
    snap = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "date": date,
        "timezone": "Asia/Shanghai",
        "captured_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "source": "github-trending-html",
        "source_version": source_version,
        "lists": lists,
    }
    snap["snapshot_id"] = compute_snapshot_id(snap)
    return snap


def save_snapshot(snapshot: dict, *, overwrite: bool = False, base: Path | None = None) -> Path:
    """写 canonical 快照。已存在时:内容相同则幂等返回;不同则要求 overwrite(旧版归档)。

    旧文件损坏时同样按"内容不同"处理:overwrite 会把损坏原文原样归档后替换,
    这是快照损坏自愈(daily_job 捕获阶段)能够走通的前提。
    """
    validate_snapshot(snapshot, expected_date=snapshot.get("date"))
    path = snapshot_path(snapshot["date"], base)
    if path.exists():
        old_raw = path.read_text(encoding="utf-8")
        try:
            old = json.loads(old_raw)
        except json.JSONDecodeError:
            old = None
        old_id = old.get("snapshot_id") if isinstance(old, dict) else None
        if old_id == snapshot["snapshot_id"]:
            try:
                validate_snapshot(old, expected_date=snapshot["date"])
            except SnapshotValidationError:
                pass  # 旧 id 字段不能证明内容完好；损坏原文仍需归档并替换。
            else:
                return path
        if not overwrite:
            raise SnapshotExistsError(
                f"{path} 已存在(snapshot_id={old_id or '损坏/无法解析'});"
                f"刷新请用 --refresh-snapshot,旧版本会自动归档")
        history_dir = Path(base or SNAPSHOT_DIR) / "history" / snapshot["date"][:4] / snapshot["date"][5:7]
        archive = history_dir / f"{snapshot['date']}T{datetime.now(TZ).strftime('%H%M%S%f')}.json"
        if old is not None:
            atomic_write_json(archive, old)
        else:
            atomic_write_text(archive, old_raw)
    atomic_write_json(path, snapshot)
    return path


def load_snapshot(date: str, base: Path | None = None) -> dict | None:
    path = snapshot_path(date, base)
    if not path.exists():
        return None
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SnapshotValidationError(f"快照 JSON 损坏: {path}") from exc
    validate_snapshot(snapshot, expected_date=date)
    return snapshot


def snapshot_to_records(snapshot: dict) -> list[dict]:
    """快照 → daily_job/trends.jsonl 兼容的 records 结构。"""
    return [
        {"list_type": item["list_type"], "entries": item["entries"]}
        for item in snapshot["lists"]
    ]


def load_day_records(date: str, base: Path | None = None) -> tuple[list[dict] | None, str]:
    """加载某日榜单:优先 canonical 快照,回退历史 trends.jsonl。

    返回 (records, source);source 为 snapshot_id 或 'legacy:trends.jsonl'。
    """
    snap = load_snapshot(date, base)
    if snap:
        return snapshot_to_records(snap), snap["snapshot_id"]
    legacy = DAILY_DIR / "trends.jsonl"
    if legacy.exists():
        records = []
        for line in legacy.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("date") == date:
                rec.pop("date", None)
                records.append(rec)
        if records:
            return records, "legacy:trends.jsonl"
    return None, ""


def iter_snapshots(base: Path | None = None):
    """按日期遍历所有 canonical 快照；history 目录不参与当前状态重建。"""
    root = Path(base or SNAPSHOT_DIR)
    if not root.exists():
        return
    for path in sorted(root.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].json")):
        date = path.stem
        snapshot = load_snapshot(date, root)
        if snapshot is not None:
            yield snapshot
