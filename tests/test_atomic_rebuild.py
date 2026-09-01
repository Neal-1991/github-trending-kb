"""原子 IO 与数据库原子重建(T06/T07/T08)。"""
import sqlite3

import pytest

from scripts.atomic_io import atomic_write_text
from scripts.db import connect, rebuild
from scripts.snapshot_store import build_snapshot, save_snapshot
from tests.conftest import write_source_files


def test_atomic_write_keeps_old_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "file.txt"
    atomic_write_text(target, "old-content")
    import scripts.atomic_io as aio
    def boom(src, dst):
        raise PermissionError("injected")
    monkeypatch.setattr(aio.os, "replace", boom)
    with pytest.raises(PermissionError):
        atomic_write_text(target, "new-content")
    assert target.read_text(encoding="utf-8") == "old-content"
    assert not list(tmp_path.glob("*.tmp"))


def test_rebuild_builds_and_replaces(sandbox):
    write_source_files(sandbox)
    conn = rebuild()
    assert conn.execute("SELECT count(*) FROM repos").fetchone()[0] == 3
    assert conn.execute("SELECT count(*) FROM trend_daily").fetchone()[0] > 0
    assert conn.execute("SELECT count(*) FROM profiles").fetchone()[0] == 1
    conn.close()
    assert sandbox["db"].exists()


def test_rebuild_prefers_canonical_snapshot_over_legacy_day(sandbox):
    write_source_files(sandbox, repos=3, real_days=1)
    entries = [{"rank": 1, "repo": "canonical/only", "description": "canonical",
                "language": "Python", "stars_total": 10, "stars_today": 9, "forks": 1}]
    save_snapshot(build_snapshot("2026-09-01", [
        {"list_type": "total", "entries": entries},
        {"list_type": "python", "entries": entries},
    ]))
    conn = rebuild()
    rows = conn.execute(
        "SELECT list_type, full_name FROM trend_daily WHERE date='2026-09-01' ORDER BY list_type"
    ).fetchall()
    assert [(r["list_type"], r["full_name"]) for r in rows] == [
        ("python", "canonical/only"), ("total", "canonical/only")]
    conn.close()


def test_rebuild_restores_no_readme_status_from_source(sandbox):
    write_source_files(sandbox, repos=3, profiles=1)
    (sandbox["readmes"] / "_missing.txt").write_text(
        "owner0/repo0\nowner1/repo1\n", encoding="utf-8")
    conn = rebuild()
    statuses = {r["full_name"]: r["profile_status"] for r in conn.execute(
        "SELECT full_name, profile_status FROM repos WHERE full_name IN "
        "('owner0/repo0','owner1/repo1')")}
    assert statuses == {"owner0/repo0": "done", "owner1/repo1": "no_readme"}
    conn.close()


def test_rebuild_failure_keeps_old_db(sandbox):
    write_source_files(sandbox)
    conn = rebuild()
    conn.execute("UPDATE repos SET stars = 777 WHERE full_name='owner0/repo0'")
    conn.commit()
    conn.close()
    before = sandbox["db"].read_bytes()

    # 注入坏 JSON:导入阶段应失败,旧 DB 保持不变
    with (sandbox["daily"] / "trends.jsonl").open("a", encoding="utf-8") as f:
        f.write("{broken json\n")
    with pytest.raises(Exception):
        rebuild()
    assert sandbox["db"].read_bytes() == before
    conn = connect()
    assert conn.execute("SELECT stars FROM repos WHERE full_name='owner0/repo0'").fetchone()[0] == 777
    conn.close()
    assert not list(sandbox["db"].parent.glob("*.tmp"))


def test_rebuild_fts_rowcount_mismatch_fails(sandbox, monkeypatch):
    write_source_files(sandbox)
    import scripts.db as db_mod
    real = db_mod.reindex_fts
    def bad_fts(conn):
        real(conn)
        conn.execute("DELETE FROM search_fts WHERE full_name='owner0/repo0'")
        conn.commit()
    monkeypatch.setattr(db_mod, "reindex_fts", bad_fts)
    with pytest.raises(RuntimeError, match="FTS"):
        rebuild()


def test_connect_ro_never_creates_db(sandbox):
    from scripts.db import connect_ro
    with pytest.raises(FileNotFoundError):
        connect_ro()
    assert not sandbox["db"].exists()


def test_read_only_connection_rejects_writes(sandbox):
    write_source_files(sandbox)
    from scripts.db import connect_ro
    conn = rebuild()
    conn.close()
    ro = connect_ro()
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("DELETE FROM repos")
    ro.close()
