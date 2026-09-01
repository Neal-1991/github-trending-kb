"""daily_job 幂等/回放/dry-run 零副作用(T02/T03)与投递状态机(T09/T10/T11)。"""

import pytest

from scripts import daily_job, delivery_log, feishu
from scripts.db import rebuild
from scripts.snapshot_store import load_snapshot
from tests.conftest import make_trending_html, write_source_files


def _records(n=12):
    from scripts.fetch_trending import parse_trending
    return [{"list_type": "total", "entries": parse_trending(make_trending_html(n))}]


@pytest.fixture()
def no_network(monkeypatch):
    """屏蔽一切外部调用:抓取返回固定样本,GLM/GitHub API/README/飞书均可断言。"""
    calls = {"fetch": 0, "send": 0, "doc": 0}
    records = _records()

    def fake_fetch_all():
        calls["fetch"] += 1
        return records

    def fake_send(card):
        calls["send"] += 1
        return True, "ok", "msg-id-1"

    monkeypatch.setattr(daily_job, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(feishu, "send", fake_send)
    monkeypatch.setattr(daily_job.feishu, "send", fake_send)
    monkeypatch.setattr(daily_job, "feishu_doc", type("F", (), {
        "generate_doc": staticmethod(lambda *a, **k: (_ for _ in ()).throw(
            __import__("scripts.feishu_doc", fromlist=["DocScopeError"]).DocScopeError("no perm"))),
        "build_daily_blocks": staticmethod(lambda *a, **k: []),
        "build_weekly_blocks": staticmethod(lambda *a, **k: []),
        "DocScopeError": __import__("scripts.feishu_doc", fromlist=["DocScopeError"]).DocScopeError,
    }))
    monkeypatch.setattr(daily_job, "GLM_API_KEY", "")
    monkeypatch.setattr(daily_job, "GITHUB_TOKEN", "")
    monkeypatch.setattr(daily_job, "FEISHU_APP_ID", "")
    monkeypatch.setattr(daily_job, "FEISHU_APP_SECRET", "")
    daily_job._calls = calls
    return calls


def _source_hash(dirs) -> dict:
    files = {}
    for p in list(dirs["raw"].iterdir()) + list(dirs["daily"].iterdir()) + list(dirs["profiles"].iterdir()):
        if p.is_file():
            files[p.name] = p.read_bytes()
    return files


def test_first_run_captures_and_second_replays(sandbox, no_network, capsys):
    write_source_files(sandbox)
    conn = rebuild()
    date = "2026-09-01"
    r1, sid1 = daily_job.capture_stage(conn, date, refresh=False, dry_run=False, notify_only=False)
    assert no_network["fetch"] == 1
    assert load_snapshot(date) is not None
    # 第二次:抓取结果即使变化也回放 canonical
    r2, sid2 = daily_job.capture_stage(conn, date, refresh=False, dry_run=False, notify_only=False)
    assert no_network["fetch"] == 1
    assert sid1 == sid2


def test_dry_run_has_zero_source_side_effects(sandbox, no_network):
    write_source_files(sandbox)
    before = _source_hash(sandbox)
    conn = rebuild()
    daily_job.capture_stage(conn, "2026-09-01", refresh=False, dry_run=True, notify_only=False)
    assert _source_hash(sandbox) == before
    assert load_snapshot("2026-09-01") is None


def test_main_dry_run_does_not_replace_project_db(sandbox, no_network, monkeypatch):
    write_source_files(sandbox)
    conn = rebuild()
    conn.close()
    before_db = sandbox["db"].read_bytes()
    before_sources = _source_hash(sandbox)
    monkeypatch.setattr("sys.argv", ["daily_job.py", "--dry-run", "--date", "2026-09-02"])
    daily_job.main()
    assert sandbox["db"].read_bytes() == before_db
    assert _source_hash(sandbox) == before_sources


def test_refresh_archives_old_snapshot(sandbox, no_network, monkeypatch):
    write_source_files(sandbox)
    conn = rebuild()
    date = "2026-09-01"
    daily_job.capture_stage(conn, date, refresh=False, dry_run=False, notify_only=False)
    old_id = load_snapshot(date)["snapshot_id"]
    # 第二次抓取返回不同内容 → refresh 产生新版本并归档旧版
    from scripts.fetch_trending import parse_trending
    variant = [{"list_type": "total",
                "entries": parse_trending(make_trending_html(15))}]
    monkeypatch.setattr(daily_job, "fetch_all", lambda: variant)
    daily_job.capture_stage(conn, date, refresh=True, dry_run=False, notify_only=False)
    new = load_snapshot(date)
    assert new["snapshot_id"] != old_id


def test_delivery_state_and_reuse_document(sandbox, monkeypatch):
    date = "2026-09-01"
    assert not delivery_log.latest_event("daily_doc", date)
    delivery_log.append_event(kind="daily_doc", date=date, status="created",
                              document_id="doc-1", url="https://feishu.cn/docx/doc-1",
                              snapshot_id="sha256:test")
    assert delivery_log.latest_event("daily_doc", date)["status"] == "created"
    # 重试复用同一 document_id,不重复创建
    created = []
    import scripts.feishu_doc as fd
    def fake_generate_doc(title, blocks, open_id=""):
        created.append(title)
        return {"document_id": "doc-1", "url": "https://feishu.cn/docx/doc-1"}
    monkeypatch.setattr(fd, "generate_doc", fake_generate_doc)
    # 已有 created 事件 → push_daily 不应再次 create
    sent_cards = []
    monkeypatch.setattr(feishu, "send",
                        lambda card: (sent_cards.append(card), (True, "ok", "mid"))[1])
    # 构造最小 conn(仅 load_profiles_map 用不到,doc blocks 为空)
    class FakeConn:
        def execute(self, *a, **k):
            return []
    # 模拟 doc 模式
    monkeypatch.setattr(daily_job, "FEISHU_APP_ID", "app")
    monkeypatch.setattr(daily_job, "FEISHU_APP_SECRET", "sec")
    records = _records()
    for rec in records:
        for e in rec["entries"]:
            e["is_new"] = False
    daily_job.push_daily(FakeConn(), date, records, {}, "sha256:test")
    assert created == []               # 未再次创建文档
    assert len(sent_cards) == 1        # 只发了链接卡片
    assert delivery_log.latest_event("daily_doc", date)["status"] == "link_sent"
    # 再次运行:整日跳过
    sent_cards.clear()
    daily_job.push_daily(FakeConn(), date, records, {}, "sha256:test")
    assert sent_cards == []


def test_webhook_mode_single_summary_card(sandbox, no_network, monkeypatch):
    date = "2026-09-01"
    sends = []
    monkeypatch.setattr(feishu, "send", lambda card: (sends.append(card), (True, "ok", None))[1])
    monkeypatch.setattr(feishu, "FEISHU_WEBHOOK", "https://hook")
    monkeypatch.setattr(daily_job, "FEISHU_APP_ID", "")
    monkeypatch.setattr(daily_job, "FEISHU_APP_SECRET", "")

    class FakeConn:
        def execute(self, *a, **k):
            return []
    records = _records()
    for rec in records:
        for e in rec["entries"]:
            e["is_new"] = False
    daily_job.push_daily(FakeConn(), date, records, {}, "sha256:test")
    assert len(sends) == 1  # 只一条消息,无第二条文档卡片
    daily_job.push_daily(FakeConn(), date, records, {}, "sha256:test")
    assert len(sends) == 1  # 幂等


def test_refreshed_snapshot_has_independent_delivery_state(sandbox, no_network, monkeypatch):
    date = "2026-09-01"
    sends = []
    monkeypatch.setattr(feishu, "send", lambda card: (sends.append(card), (True, "ok", None))[1])
    monkeypatch.setattr(daily_job, "FEISHU_APP_ID", "")
    monkeypatch.setattr(daily_job, "FEISHU_APP_SECRET", "")

    class FakeConn:
        def execute(self, *a, **k):
            return []

    records = _records()
    daily_job.push_daily(FakeConn(), date, records, {}, "sha256:v1")
    daily_job.push_daily(FakeConn(), date, records, {}, "sha256:v2")
    daily_job.push_daily(FakeConn(), date, records, {}, "sha256:v2")
    assert len(sends) == 2
    assert delivery_log.latest_event(
        "daily_message", date, snapshot_id="sha256:v1")["status"] == "sent"
    assert delivery_log.latest_event(
        "daily_message", date, snapshot_id="sha256:v2")["status"] == "sent"


def test_new_face_flag_is_consistent_across_lists(sandbox, no_network):
    write_source_files(sandbox, repos=1)
    conn = rebuild()
    entry = {"rank": 1, "repo": "new/repo", "description": "new",
             "language": "Python", "stars_total": 10, "stars_today": 5, "forks": 1}
    records = [
        {"list_type": "total", "entries": [dict(entry)]},
        {"list_type": "python", "entries": [dict(entry)]},
    ]
    daily_job.profile_stage(conn, records, "2026-09-02", dry_run=True)
    assert all(e["is_new"] for rec in records for e in rec["entries"])
    conn.close()
