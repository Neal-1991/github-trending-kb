"""rotate_logs 日志保留/归档:过期行入正确月份归档、未过期行留存、幂等、
缺文件容忍、整行去重,以及 db 重建后 push_log 表仍包含归档行。全部离线。"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts import rotate_logs
from scripts.db import rebuild

TZ = ZoneInfo("Asia/Shanghai")


def _date(days_ago: int) -> str:
    return (datetime.now(TZ).date() - timedelta(days=days_ago)).isoformat()


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _push(date: str, name: str = "owner/repo", pushed_at: str = "2026-01-01T08:00:00+08:00") -> dict:
    return {"date": date, "list_type": "total", "full_name": name, "pushed_at": pushed_at}


def _event(date: str, kind: str = "daily_message") -> dict:
    return {"date": date, "kind": kind, "status": "sent", "message_id": "om_test",
            "snapshot_id": f"snap-{date}"}


def test_expired_rows_move_to_month_archive_and_fresh_rows_stay(sandbox):
    daily = sandbox["daily"]
    old, mid, fresh = _date(200), _date(100), _date(1)
    _write_jsonl(daily / "push_log.jsonl",
                 [_push(old, name="a/a"), _push(mid, name="b/b"), _push(fresh, name="c/c")])
    _write_jsonl(daily / "delivery_log.jsonl", [_event(old), _event(fresh)])

    result = rotate_logs.rotate_all(daily_dir=daily)

    assert result["files"]["push_log.jsonl"] == {
        "kept": 1, "archived": 2, "months": {old[:7]: 1, mid[:7]: 1}}
    assert result["files"]["delivery_log.jsonl"]["kept"] == 1
    assert result["files"]["delivery_log.jsonl"]["archived"] == 1
    # 过期行按各自 date 所在月份归档;两种日志的行混在同一月度文件,每行自描述
    arch_old = _read_jsonl(daily / "archive" / f"{old[:7]}.jsonl")
    assert len(arch_old) == 2
    assert [r["full_name"] for r in arch_old if "full_name" in r] == ["a/a"]
    assert [r["kind"] for r in arch_old if "kind" in r] == ["daily_message"]
    arch_mid = _read_jsonl(daily / "archive" / f"{mid[:7]}.jsonl")
    assert [r["full_name"] for r in arch_mid] == ["b/b"]
    # 未过期行留在原文件
    assert [r["full_name"] for r in _read_jsonl(daily / "push_log.jsonl")] == ["c/c"]
    assert [r["date"] for r in _read_jsonl(daily / "delivery_log.jsonl")] == [fresh]


def test_boundary_date_on_cutoff_is_kept(sandbox):
    daily = sandbox["daily"]
    cutoff = rotate_logs.cutoff_date(90)
    _write_jsonl(daily / "push_log.jsonl", [_push(cutoff, name="edge/edge")])

    result = rotate_logs.rotate_all(daily_dir=daily)

    assert result["cutoff"] == cutoff
    assert result["files"]["push_log.jsonl"]["kept"] == 1
    assert result["files"]["push_log.jsonl"]["archived"] == 0
    assert not (daily / "archive").exists()  # 无可归档行时不创建归档目录


def test_days_override_changes_cutoff(sandbox):
    daily = sandbox["daily"]
    _write_jsonl(daily / "push_log.jsonl", [_push(_date(30))])

    result = rotate_logs.rotate_all(daily_dir=daily, days=7)

    assert result["cutoff"] == rotate_logs.cutoff_date(7)
    assert result["files"]["push_log.jsonl"]["archived"] == 1


def test_rotate_is_idempotent(sandbox):
    daily = sandbox["daily"]
    _write_jsonl(daily / "push_log.jsonl", [_push(_date(200)), _push(_date(1), name="x/y")])
    _write_jsonl(daily / "delivery_log.jsonl", [_event(_date(200))])
    rotate_logs.rotate_all(daily_dir=daily)

    before = {p: p.read_bytes() for p in sorted(daily.rglob("*.jsonl"))}
    result = rotate_logs.rotate_all(daily_dir=daily)
    after = {p: p.read_bytes() for p in sorted(daily.rglob("*.jsonl"))}

    assert before == after  # 重复运行无任何变化
    assert result["files"]["push_log.jsonl"]["archived"] == 0
    assert result["files"]["delivery_log.jsonl"]["archived"] == 0


def test_missing_input_files_are_skipped(sandbox):
    daily = sandbox["daily"]
    result = rotate_logs.rotate_all(daily_dir=daily)
    assert result["files"] == {"push_log.jsonl": None, "delivery_log.jsonl": None}
    assert not (daily / "archive").exists()

    _write_jsonl(daily / "push_log.jsonl", [_push(_date(1))])
    result = rotate_logs.rotate_all(daily_dir=daily)
    assert result["files"]["push_log.jsonl"]["kept"] == 1
    assert result["files"]["delivery_log.jsonl"] is None  # delivery_log 可能尚不存在


def test_archive_merge_dedupes_by_line_text(sandbox):
    daily = sandbox["daily"]
    old = _date(200)
    line = json.dumps(_push(old), ensure_ascii=False)
    archive_file = daily / "archive" / f"{old[:7]}.jsonl"
    archive_file.parent.mkdir()
    # 模拟"归档已写、live 重写前崩溃":归档与 live 同时存在同一行
    archive_file.write_text(line + "\n", encoding="utf-8")
    (daily / "push_log.jsonl").write_text((line + "\n") * 2, encoding="utf-8")

    result = rotate_logs.rotate_all(daily_dir=daily)

    assert result["files"]["push_log.jsonl"]["archived"] == 0  # 按整行文本去重,不重复入档
    assert archive_file.read_text(encoding="utf-8") == line + "\n"
    assert (daily / "push_log.jsonl").read_text(encoding="utf-8") == ""  # live 仍被清干净


def test_row_without_date_fails_closed(sandbox):
    daily = sandbox["daily"]
    bad = '{"kind":"daily_message","status":"sent"}\n'
    (daily / "delivery_log.jsonl").write_text(bad, encoding="utf-8")

    with pytest.raises(ValueError, match="date"):
        rotate_logs.rotate_all(daily_dir=daily)
    # 原 文件未被破坏(fail closed,不静默决定无法判定行的去留)
    assert (daily / "delivery_log.jsonl").read_text(encoding="utf-8") == bad


def test_db_rebuild_imports_archived_push_rows(sandbox):
    daily = sandbox["daily"]
    old, fresh = _date(200), _date(1)
    _write_jsonl(daily / "push_log.jsonl",
                 [_push(old, name="old/old", pushed_at="2026-05-01T08:00:00+08:00"),
                  _push(fresh, name="new/new", pushed_at="2026-09-01T08:00:00+08:00")])
    _write_jsonl(daily / "delivery_log.jsonl", [_event(old)])
    rotate_logs.rotate_all(daily_dir=daily)

    conn = rebuild(sandbox["db"])
    try:
        rows = {(r["date"], r["list_type"], r["full_name"]): r["pushed_at"]
                for r in conn.execute("SELECT * FROM push_log")}
    finally:
        conn.close()

    # 归档行不从数据库消失;归档里的投递事件行不混入 push_log 表
    assert rows == {(old, "total", "old/old"): "2026-05-01T08:00:00+08:00",
                    (fresh, "total", "new/new"): "2026-09-01T08:00:00+08:00"}


def test_db_rebuild_live_row_wins_on_same_key(sandbox):
    daily = sandbox["daily"]
    old = _date(200)
    _write_jsonl(daily / "push_log.jsonl",
                 [_push(old, pushed_at="2026-05-01T09:00:00+08:00")])
    rotate_logs.rotate_all(daily_dir=daily)
    # 归档后同键行又以新 pushed_at 重写进 live(回放场景):live 最后导入,当前状态优先
    _write_jsonl(daily / "push_log.jsonl",
                 [_push(old, pushed_at="2026-05-01T10:00:00+08:00")])

    conn = rebuild(sandbox["db"])
    try:
        row = conn.execute(
            "SELECT pushed_at FROM push_log WHERE full_name='owner/repo'").fetchone()
    finally:
        conn.close()

    assert row["pushed_at"] == "2026-05-01T10:00:00+08:00"
