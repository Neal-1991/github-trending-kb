"""外部通知投递状态:append-only 事件日志,从事件重建状态。

设计约束(review P0-05):飞书无幂等键,只能做到"至少一次 + 可检测重复"。
每种通知独立管理状态:
  daily_message   日报最终入口(文档模式=链接卡片;webhook 模式=摘要卡片)
  daily_doc       日报云文档(created → link_sent 两段状态,link 失败可复用 document_id 重试)
  weekly_message  周报最终入口
  weekly_doc      周报云文档
事件只追加不修改;读取端按 (kind, date) 取最新状态。
兼容:迁移期同时识别旧 doc_log.jsonl / push_log.jsonl,避免升级当天重复推送。
"""
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import DAILY_DIR
from scripts.atomic_io import atomic_append_jsonl

DELIVERY_LOG = DAILY_DIR / "delivery_log.jsonl"
LEGACY_DOC_LOG = DAILY_DIR / "doc_log.jsonl"
LEGACY_PUSH_LOG = DAILY_DIR / "push_log.jsonl"
TZ = ZoneInfo("Asia/Shanghai")


def append_event(path: Path | None = None, **event) -> dict:
    event.setdefault("ts", datetime.now(TZ).isoformat(timespec="seconds"))
    atomic_append_jsonl(path or DELIVERY_LOG, event)
    return event


def _iter_events(path: Path | None = None):
    p = path or DELIVERY_LOG
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def latest_event(kind: str, date: str, path: Path | None = None) -> dict | None:
    """某 (kind, date) 的最新事件;无则 None。"""
    found = None
    for e in _iter_events(path):
        if e.get("kind") == kind and e.get("date") == date:
            found = e
    return found


def legacy_doc_done(date: str) -> bool:
    """旧 doc_log.jsonl 中的日期(含 week- 前缀)视为已发送,防止升级后重复推送。"""
    if not LEGACY_DOC_LOG.exists():
        return False
    for line in LEGACY_DOC_LOG.read_text(encoding="utf-8").splitlines():
        if line.strip() and json.loads(line).get("date") in (date, f"week-{date}"):
            return True
    return False


def legacy_daily_pushed(date: str) -> bool:
    if not LEGACY_PUSH_LOG.exists():
        return False
    for line in LEGACY_PUSH_LOG.read_text(encoding="utf-8").splitlines():
        if line.strip() and json.loads(line).get("date") == date:
            return True
    return False
