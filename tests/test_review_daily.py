"""覆盖 CLI、跨阶段和跨日恢复，所有调用均离线。"""
import json

import pytest

from scripts import daily_job as dj
from scripts import delivery_log, feishu
from scripts.db import rebuild
from scripts.profile_queue import process_queue
from scripts.snapshot_store import build_snapshot, load_snapshot, save_snapshot, snapshot_path
from tests.conftest import make_trending_html, write_source_files


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(dj, "today_bj", lambda: "2026-09-05")
    monkeypatch.setattr(dj, "GITHUB_TOKEN", "")
    monkeypatch.setattr(dj, "GLM_API_KEY", "")
    monkeypatch.setattr(dj, "FEISHU_APP_ID", "")
    monkeypatch.setattr(dj, "FEISHU_APP_SECRET", "")
    monkeypatch.setattr(feishu, "FEISHU_WEBHOOK", "mock-configured")
    monkeypatch.setattr(dj, "fetch_one", lambda name: "temporary_error")


def records(n=12):
    from scripts.fetch_trending import parse_trending
    return [{"list_type": "total", "entries": parse_trending(make_trending_html(n))}]


@pytest.mark.parametrize("day,refresh", [("2020-01-01", False), ("2020-01-01", True),
                                        ("2027-01-01", False), ("2026-9-1", False),
                                        ("2026-02-30", False)])
def test_invalid_or_missing_date_never_fetches(sandbox, monkeypatch, day, refresh):
    monkeypatch.setattr(dj, "fetch_all", lambda: pytest.fail("historical/future live fetch"))
    with pytest.raises(ValueError):
        dj.capture_stage(None, day, refresh=refresh, dry_run=False, notify_only=False)
    assert not list(sandbox["daily"].rglob("*.json"))


def test_historical_legacy_replay_without_fetch(sandbox, monkeypatch):
    write_source_files(sandbox)
    monkeypatch.setattr(dj, "fetch_all", lambda: pytest.fail("legacy replay fetched live"))
    loaded, sid = dj.capture_stage(None, "2026-09-01", refresh=False,
                                   dry_run=False, notify_only=False)
    assert sid == "legacy:trends.jsonl" and loaded


def test_main_recovers_corrupt_today_before_rebuild(sandbox, monkeypatch):
    write_source_files(sandbox)
    path = snapshot_path("2026-09-05")
    path.parent.mkdir(parents=True)
    path.write_text("broken", encoding="utf-8")
    monkeypatch.setattr(dj, "fetch_all", records)
    monkeypatch.setattr("sys.argv", ["daily_job.py", "--capture-only"])
    dj.main()
    assert load_snapshot("2026-09-05")
    assert any(p.read_text(encoding="utf-8") == "broken"
               for p in sandbox["daily"].glob("snapshots/history/**/*.json"))


def test_recovery_replaces_tampering_even_when_id_matches_fresh(sandbox, monkeypatch):
    fresh = build_snapshot("2026-09-05", records())
    path = save_snapshot(fresh)
    fresh["lists"][0]["entries"][0]["repo"] = "tampered/repo"
    path.write_text(json.dumps(fresh), encoding="utf-8")
    monkeypatch.setattr(dj, "fetch_all", records)
    dj.capture_stage(None, "2026-09-05", refresh=False, dry_run=False, notify_only=False)
    assert load_snapshot("2026-09-05")["lists"][0]["entries"][0]["repo"] == "owner0/repo0"


def test_refresh_removes_old_ranks_and_removed_lists(sandbox, monkeypatch):
    original = records(25)
    original.append({"list_type": "lang:python", "entries": original[0]["entries"]})
    save_snapshot(build_snapshot("2026-09-05", original))
    conn = rebuild()
    monkeypatch.setattr(dj, "fetch_all", lambda: records(20))
    fresh, _ = dj.capture_stage(conn, "2026-09-05", refresh=True,
                                dry_run=False, notify_only=False)
    dj.profile_stage(conn, fresh, "2026-09-05", dry_run=False)
    assert conn.execute("SELECT count(*) FROM trend_daily").fetchone()[0] == 20
    assert conn.execute("SELECT MAX(rank) FROM trend_daily").fetchone()[0] == 20
    assert conn.execute("SELECT trend_days FROM repos WHERE full_name='owner24/repo24'").fetchone()[0] == 0
    conn.close()


def test_failed_message_records_failure_and_raises(sandbox, monkeypatch):
    conn = rebuild()
    monkeypatch.setattr(feishu, "send", lambda card: (False, "rejected", None))
    with pytest.raises(dj.NotificationError):
        dj.push_daily(conn, "2026-09-05", records(), {}, "s")
    assert delivery_log.latest_event("daily_message", "2026-09-05")["status"] == "failed"
    conn.close()


def test_daily_failure_does_not_block_weekly(sandbox, monkeypatch):
    seen = []
    def daily(*args):
        raise dj.NotificationError("daily rejected")
    monkeypatch.setattr(dj, "push_daily", daily)
    monkeypatch.setattr(dj, "push_weekly", lambda *args: seen.append("weekly"))
    with pytest.raises(dj.NotificationError):
        dj.notify_stage(None, "2026-08-30", [], {}, "s")
    assert seen == ["weekly"]


def test_cli_notify_failure_not_success(sandbox, monkeypatch):
    write_source_files(sandbox)
    monkeypatch.setattr(feishu, "send", lambda card: (False, "rejected", None))
    monkeypatch.setattr("sys.argv", ["daily_job.py", "--notify-only", "--date", "2026-09-01"])
    with pytest.raises(dj.NotificationError):
        dj.main()


def test_sent_event_survives_compat_log_failure(sandbox, monkeypatch):
    conn = rebuild()
    seen = []
    monkeypatch.setattr(feishu, "send", lambda card: (seen.append(card) or True, "ok", "id"))
    def broken_log(*args):
        raise OSError("disk failure")
    monkeypatch.setattr(dj, "_record_push_log", broken_log)
    with pytest.raises(dj.NotificationError):
        dj.notify_stage(conn, "2026-09-05", records(), {}, "s")
    assert delivery_log.latest_event("daily_message", "2026-09-05")["status"] == "sent"
    dj.notify_stage(conn, "2026-09-05", records(), {}, "s")
    assert len(seen) == 1
    conn.close()


def test_archived_daily_does_not_resend(sandbox, monkeypatch):
    from scripts.rotate_logs import rotate_all
    conn = rebuild()
    seen = []
    monkeypatch.setattr(feishu, "send", lambda card: (seen.append(card) or True, "ok", "id"))
    dj.push_daily(conn, "2025-01-01", records(), {}, "s")
    rotate_all(sandbox["daily"], days=90)
    dj.push_daily(conn, "2025-01-01", records(), {}, "s")
    assert len(seen) == 1
    conn.close()


def test_notify_only_uses_stored_profile(sandbox, monkeypatch):
    write_source_files(sandbox)
    sent = []
    monkeypatch.setattr(feishu, "send", lambda card: (sent.append(card) or True, "ok", "id"))
    monkeypatch.setattr("sys.argv", ["daily_job.py", "--notify-only", "--date", "2026-09-01"])
    dj.main()
    assert "项目0简介" in json.dumps(sent, ensure_ascii=False)


def test_weekly_window_and_timezone(sandbox, monkeypatch):
    conn = rebuild()
    for name, day in [("inside/repo", "2026-08-30"), ("future/repo", "2026-09-05")]:
        conn.execute("INSERT INTO repos(full_name,first_trend_date) VALUES(?,?)", (name, day))
        conn.execute("INSERT INTO trend_daily VALUES(?,'total',1,?,100,NULL,0)", (day, name))
    for name, stamp in [("in", "2026-08-23T16:00:00Z"), ("out", "2026-08-30T16:00:00Z")]:
        conn.execute("INSERT INTO profiles(full_name,generated_at) VALUES(?,?)", (name, stamp))
    summary = {}
    original = feishu.build_weekly_card
    def card(day, data):
        summary.update(data)
        return original(day, data)
    monkeypatch.setattr(feishu, "build_weekly_card", card)
    monkeypatch.setattr(feishu, "send", lambda card: (True, "ok", "id"))
    dj.push_weekly(conn, "2026-08-30", "s")
    assert summary["top_new"] == [("inside/repo", 100)]
    assert summary["new_repos"] == summary["profiled"] == 1
    conn.close()


def test_queue_survives_rebuild_and_drains_off_board_repo(sandbox):
    save_snapshot(build_snapshot("2026-09-01", records(2)))
    conn = rebuild()
    path = sandbox["profiles"] / "pending_queue.json"
    seen = []
    def profile(names, dry_run, conn):
        for name in names:
            seen.append(name)
            conn.execute("INSERT INTO profiles(full_name,one_liner) VALUES(?, 'done')", (name,))
            # 持久 source，下一次 rebuild 恢复已完成项。
            with (sandbox["profiles"] / "profiles.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"full_name": name, "one_liner": "done"}) + "\n")
        conn.commit()
        return {}
    process_queue(conn, ["owner0/repo0", "owner1/repo1"], path, "2026-09-01", 1, False, profile)
    conn.close()
    conn = rebuild()
    process_queue(conn, [], path, "2026-09-02", 1, False, profile)
    assert seen == ["owner0/repo0", "owner1/repo1"]
    assert json.loads(path.read_text()) == {}
    conn.close()


def test_queue_retries_missing_readme_after_cooldown(sandbox):
    conn = rebuild()
    conn.execute("INSERT INTO repos(full_name) VALUES('owner/repo')")
    path = sandbox["profiles"] / "pending_queue.json"
    seen = []
    def profile(names, dry_run, conn):
        seen.extend(names)
        conn.execute("UPDATE repos SET profile_status='no_readme'")
        conn.commit()
        return {}
    process_queue(conn, ["owner/repo"], path, "2026-09-01", 1, False, profile)
    process_queue(conn, [], path, "2026-09-02", 1, False, profile)
    assert len(seen) == 1
    process_queue(conn, [], path, "2026-10-01", 1, False, profile)
    assert len(seen) == 2
    conn.close()


@pytest.mark.parametrize("status", [403, 500, 503, 200])
def test_readme_non_404_is_retryable(sandbox, monkeypatch, status):
    from types import SimpleNamespace

    from scripts import fetch_readmes
    monkeypatch.setattr(fetch_readmes, "_session", lambda: SimpleNamespace(
        get=lambda *args, **kwargs: SimpleNamespace(status_code=status, text="")))
    assert fetch_readmes.fetch_one("owner/repo") == "temporary_error"
    assert not (sandbox["readmes"] / "owner__repo.md").exists()


def test_due_no_readme_temporary_failure_retries_next_day(sandbox, monkeypatch):
    conn = rebuild()
    conn.execute("INSERT INTO repos(full_name,profile_status) VALUES('owner/repo','no_readme')")
    path = sandbox["profiles"] / "pending_queue.json"
    path.write_text(json.dumps({"owner/repo": {"status": "no_readme", "attempts": 1,
                                              "queued_at": "2026-08-01",
                                              "retry_at": "2026-09-05"}}))
    monkeypatch.setattr(dj, "fetch_one", lambda name: "temporary_error")
    process_queue(conn, [], path, "2026-09-05", 1, False, dj.profile_new_repos)
    assert json.loads(path.read_text())["owner/repo"]["retry_at"] == "2026-09-06"
    conn.close()
