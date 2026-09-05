"""Review regressions for archived delivery state and derived storage."""
import json
import sqlite3

import pytest

from scripts import db, delivery_log
from scripts.rotate_logs import rotate_file
from scripts.snapshot_store import build_snapshot, save_snapshot
from tests.conftest import write_source_files


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_delivery_rotation_preserves_latest_state_and_snapshot(sandbox):
    path = sandbox["daily"] / "delivery_log.jsonl"
    common = {"kind": "daily_doc", "date": "2026-01-01", "snapshot_id": "v1"}
    write_jsonl(path, [{**common, "status": "created", "document_id": "existing"},
                       {**common, "status": "link_sent", "document_id": "existing"}])
    rotate_file(path, "2026-02-01")
    assert delivery_log.latest_event("daily_doc", "2026-01-01")["status"] == "link_sent"
    delivery_log.append_event(**{**common, "status": "retry"})
    assert delivery_log.latest_event("daily_doc", "2026-01-01")["status"] == "retry"
    assert delivery_log.latest_event("daily_doc", "2026-01-01", snapshot_id="v2") is None
    rotate_file(path, "2026-02-01")
    path.unlink()  # Archived state must survive even without a live file.
    assert delivery_log.latest_event("daily_doc", "2026-01-01")["status"] == "retry"


def test_archived_legacy_push_ignores_delivery_failures(sandbox):
    path = sandbox["daily"] / "push_log.jsonl"
    write_jsonl(path, [{"date": "2026-01-01", "list_type": "total", "full_name": "a/b"}])
    rotate_file(path, "2026-02-01")
    delivery = sandbox["daily"] / "delivery_log.jsonl"
    write_jsonl(delivery, [{"kind": "daily_message", "date": "2026-01-02", "status": "failed"}])
    rotate_file(delivery, "2026-02-01")
    assert delivery_log.legacy_daily_pushed("2026-01-01")
    assert not delivery_log.legacy_daily_pushed("2026-01-02")
    assert not delivery_log.legacy_doc_done("2026-01-01")


def test_rebuild_restores_trending_metadata_without_overwriting_verified(sandbox):
    write_source_files(sandbox, repos=2, profiles=0, real_days=0)
    write_jsonl(sandbox["raw"] / "repo_meta_api.jsonl", [
        {"full_name": "owner0/repo0", "description": "api", "language": "Go"}])
    def rec(day, name, desc, language="Rust", list_type="total"):
        return {"date": day, "list_type": list_type, "entries": [
            {"repo": name, "rank": 1, "description": desc, "language": language}]}

    write_jsonl(sandbox["daily"] / "trends.jsonl", [
        rec("2026-01-03", "legacy/repo", "later"),
        rec("2026-01-01", "legacy/repo", "first"),
        rec("2026-01-02", "canonical/repo", "stale export"),
        rec("2026-01-02", "owner0/repo0", "untrusted"),
    ])
    save_snapshot(build_snapshot("2026-01-02", [
        rec("2026-01-02", "canonical/repo", "language list", list_type="lang:rust"),
        {"list_type": "total", "entries": [
            {"repo": name, "rank": i, "description": "canonical", "language": "Rust"}
            for i, name in enumerate(["canonical/repo", "owner0/repo0", "owner1/repo1"], 1)]},
    ]))
    conn = db.rebuild()
    try:
        rows = {r["full_name"]: dict(r) for r in conn.execute("SELECT * FROM repos")}
        assert rows["legacy/repo"]["description"] == "first"
        assert rows["canonical/repo"]["description"] == "canonical"
        assert rows["canonical/repo"]["language"] == "Rust"
        assert rows["canonical/repo"]["verified"] == 0
        assert rows["canonical/repo"]["source"] == "trending"
        assert rows["owner0/repo0"]["description"] == "api"
        assert rows["owner0/repo0"]["language"] == "Go"
        assert rows["owner1/repo1"]["description"] == "desc 1"
    finally:
        conn.close()


def test_fts_failure_keeps_previous_index(sandbox):
    write_source_files(sandbox)
    conn = db.rebuild()
    before = [tuple(row) for row in conn.execute("SELECT * FROM search_fts")]
    # Deny only the fill operation, after DROP/CREATE have successfully executed.
    conn.set_authorizer(lambda action, name, *_:
                        sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT
                        and name == "search_fts" else sqlite3.SQLITE_OK)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            db.reindex_fts(conn)
        conn.set_authorizer(None)
        assert [tuple(row) for row in conn.execute("SELECT * FROM search_fts")] == before
    finally:
        conn.close()


def test_fts_rebuild_does_not_commit_caller_transaction(sandbox):
    write_source_files(sandbox)
    conn = db.rebuild()
    reader = db.connect(sandbox["db"])
    try:
        conn.execute("UPDATE repos SET description='changed'")
        db.reindex_fts(conn)
        assert reader.execute("SELECT description FROM search_fts LIMIT 1").fetchone()[0] != "changed"
        conn.rollback()
        assert conn.execute("SELECT description FROM search_fts LIMIT 1").fetchone()[0] != "changed"
        conn.execute("UPDATE repos SET description='changed'")
        conn.commit()
        db.reindex_fts(conn)
        assert reader.execute("SELECT description FROM search_fts LIMIT 1").fetchone()[0] == "changed"
    finally:
        reader.close()
        conn.close()
