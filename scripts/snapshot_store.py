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
from scripts.atomic_io import atomic_write_json

SNAPSHOT_SCHEMA_VERSION = 2
SNAPSHOT_DIR = DAILY_DIR / "snapshots"
SNAPSHOT_HISTORY_DIR = SNAPSHOT_DIR / "history"
TZ = ZoneInfo("Asia/Shanghai")


class SnapshotExistsError(RuntimeError):
    """canonical 快照已存在且未显式要求刷新。"""


def canonical_content(snapshot: dict) -> dict:
    """参与 snapshot_id 计算与落盘的内容(剔除时间戳与 id 自身)。"""
    return {k: v for k, v in snapshot.items() if k not in ("captured_at", "snapshot_id")}


def compute_snapshot_id(snapshot: dict) -> str:
    payload = json.dumps(canonical_content(snapshot), ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
    """写 canonical 快照。已存在时:内容相同则幂等返回;不同则要求 overwrite(旧版归档)。"""
    path = snapshot_path(snapshot["date"], base)
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        if old.get("snapshot_id") == snapshot["snapshot_id"]:
            return path
        if not overwrite:
            raise SnapshotExistsError(
                f"{path} 已存在(snapshot_id={old.get('snapshot_id')});"
                f"刷新请用 --refresh-snapshot,旧版本会自动归档")
        history_dir = Path(base or SNAPSHOT_DIR) / "history" / snapshot["date"][:4] / snapshot["date"][5:7]
        archive = history_dir / f"{snapshot['date']}T{datetime.now(TZ).strftime('%H%M%S')}.json"
        atomic_write_json(archive, old)
    atomic_write_json(path, snapshot)
    return path


def load_snapshot(date: str, base: Path | None = None) -> dict | None:
    path = snapshot_path(date, base)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def snapshot_to_records(snapshot: dict) -> list[dict]:
    """快照 → daily_job/trends.jsonl 兼容的 records 结构。"""
    return [{"list_type": l["list_type"], "entries": l["entries"]} for l in snapshot["lists"]]


def load_day_records(date: str, base: Path | None = None) -> tuple[list[dict] | None, str]:
    """加载某日榜单:优先 canonical 快照,回退历史 trends.jsonl。

    返回 (records, source);source 为 snapshot_id 或 'legacy:trends.jsonl'。
    """
    snap = load_snapshot(date, base)
    if snap:
        return snapshot_to_records(snap), snap["snapshot_id"]
    legacy = DAILY_DIR / "trends.jsonl"
    if legacy.exists():
        for line in legacy.read_text(encoding="utf-8").splitlines():
            if line.strip() and json.loads(line).get("date") == date:
                rec = json.loads(line)
                rec.pop("date", None)
                return [rec], "legacy:trends.jsonl"
    return None, ""
