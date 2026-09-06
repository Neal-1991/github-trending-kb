"""Web 同步接口与后台生命周期测试:CSRF/同源保护、准备页、运行库优先级、协调器。"""
import json
import time

import pytest
from conftest import FakeClient, make_remote_files
from fastapi.testclient import TestClient

from scripts.runtime_store import RuntimeStore


@pytest.fixture()
def webapp(sandbox):
    import web.app as webapp

    return webapp


def _csrf_headers(webapp, extra=None):
    headers = {"X-CSRF-Token": webapp.SYNC_CSRF_TOKEN}
    headers.update(extra or {})
    return headers


# ---------- 状态接口(无数据库依赖,不触网) ----------

def test_sync_status_endpoint_no_db(webapp):
    c = TestClient(webapp.app)
    r = c.get("/api/sync/status")
    assert r.status_code == 200
    data = r.json()
    assert data["enabled"] is False            # 测试默认关闭自动同步
    assert data["last_result"] == "disabled"
    assert data["running"] is False
    assert data["csrf_token"] == webapp.SYNC_CSRF_TOKEN


# ---------- 写接口保护:Host / Origin / CSRF ----------

def test_post_sync_requires_csrf_token(webapp):
    c = TestClient(webapp.app)
    assert c.post("/api/sync").status_code == 403
    assert c.post("/api/sync", headers={"X-CSRF-Token": "wrong"}).status_code == 403


def test_post_sync_rejects_cross_origin(webapp):
    c = TestClient(webapp.app)
    r = c.post("/api/sync", headers=_csrf_headers(webapp, {"Origin": "http://evil.example"}))
    assert r.status_code == 403


def test_post_sync_rejects_foreign_host(webapp):
    c = TestClient(webapp.app)
    r = c.post("/api/sync", headers=_csrf_headers(webapp, {"Host": "intranet.example"}))
    assert r.status_code == 403


def test_post_sync_same_origin_token_ok_202(webapp):
    c = TestClient(webapp.app)
    r = c.post("/api/sync", headers=_csrf_headers(
        webapp, {"Host": "127.0.0.1:8000", "Origin": "http://127.0.0.1:8000"}))
    assert r.status_code == 202
    data = r.json()
    assert data["trigger"] == {"triggered": False, "reason": "disabled"}


def test_post_sync_accepts_loopback_hosts(webapp):
    c = TestClient(webapp.app)
    for host in ("127.0.0.1:8000", "localhost:8000"):
        r = c.post("/api/sync", headers=_csrf_headers(webapp, {"Host": host}))
        assert r.status_code == 202, host


# ---------- 缺库准备页与查询回退 ----------

def test_preparing_page_when_no_db(webapp):
    c = TestClient(webapp.app)
    r = c.get("/", headers={"accept": "text/html"})
    assert r.status_code == 503
    assert "正在准备数据" in r.text
    # JSON API 与健康检查保持结构化响应
    assert c.get("/api/search", params={"q": "x"}).status_code == 503
    assert c.get("/readyz").status_code == 503


def test_runtime_generation_preferred_over_legacy_db(sandbox, webapp):
    from scripts.db import rebuild
    from scripts.source_paths import SourcePaths

    # 工作区旧库:含 legacy/only
    conn = rebuild(sandbox["db"])
    conn.execute("INSERT OR REPLACE INTO repos (full_name) VALUES ('legacy/only')")
    conn.commit()
    conn.close()
    # runtime 版本:含 runtime/only
    store = RuntimeStore()
    staging = store.new_staging()
    write_source_files(staging / "source", sandbox)  # 见下方辅助
    sources = SourcePaths(raw_dir=staging / "source" / "raw",
                          daily_dir=staging / "source" / "daily",
                          profiles_dir=staging / "source" / "profiles",
                          readme_dir=staging / "source" / "readmes")
    db = rebuild(staging / "trending.db", sources=sources)
    db.close()
    manifest = {"generation_id": "testgen", "commit": "f" * 40, "source": "local",
                "build_version": "1", "fingerprint": "x", "files": {},
                "latest_date": "2026-09-02", "built_at": "2026-09-05T00:00:00+00:00",
                "counts": {}}
    (staging / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    store.publish(staging, "testgen")
    store.activate("testgen", manifest)

    c = TestClient(webapp.app)
    assert c.get("/search", params={"q": "runtime"}).status_code == 200
    assert "runtime/only" in c.get("/search", params={"q": "runtime"}).text
    assert "legacy/only" not in c.get("/search", params={"q": "runtime"}).text
    # 状态条也能看到激活版本
    data = c.get("/api/sync/status").json()
    assert data["active_generation"] == "testgen"


def write_source_files(target_root, sandbox):
    """把 sandbox source 布局复制到目标根(测试辅助,只读 sandbox)。"""
    import shutil

    for sub in ("raw", "daily", "profiles", "readmes"):
        src = sandbox[sub]
        if src.exists():
            shutil.copytree(src, target_root / sub)
    # 确保有 canonical 快照(运行库校验与展示需要)
    snap_root = target_root / "daily" / "snapshots" / "2026" / "09"
    snap_root.mkdir(parents=True, exist_ok=True)
    from scripts.snapshot_store import build_snapshot

    snap = build_snapshot("2026-09-02", [{"list_type": "total", "entries": [
        {"rank": 1, "repo": "runtime/only", "stars_today": 9}]}])
    (snap_root / "2026-09-02.json").write_text(
        json.dumps(snap, ensure_ascii=False), encoding="utf-8")


# ---------- 后台协调器生命周期 ----------

def test_coordinator_runs_immediately_and_stops(sandbox, webapp, monkeypatch):
    import web.app as webapp_mod

    monkeypatch.setattr(webapp_mod.config, "DATA_SYNC_ENABLED", True)
    monkeypatch.setattr("scripts.sync_data.default_client",
                        lambda: FakeClient(make_remote_files()))
    with TestClient(webapp_mod.app) as c:
        deadline = time.time() + 10
        data = {}
        while time.time() < deadline:
            data = c.get("/api/sync/status").json()
            if data.get("last_result") == "updated":
                break
            time.sleep(0.1)
        assert data.get("last_result") == "updated"
        assert data.get("active_latest_date") == "2026-08-30"
        assert data.get("enabled") is True
    assert webapp_mod.sync_coordinator is None  # 关闭时已停止并清理


def test_coordinator_trigger_merges_while_running(sandbox, monkeypatch):
    import threading

    import web.app as webapp_mod
    from web.sync_service import SyncCoordinator

    monkeypatch.setattr(webapp_mod.config, "DATA_SYNC_ENABLED", True)
    store = RuntimeStore()
    gate = threading.Event()
    released = threading.Event()

    class BlockingClient(FakeClient):
        def get_head(self):
            released.set()
            gate.wait(timeout=5)
            return super().get_head()

    coordinator = SyncCoordinator(store=store, client_factory=lambda: BlockingClient(
        make_remote_files()), interval_seconds=3600)
    coordinator.start()
    try:
        assert released.wait(timeout=5), "后台同步未启动"
        deadline = time.time() + 5
        while time.time() < deadline and not coordinator.is_running():
            time.sleep(0.05)
        result = coordinator.trigger()
        assert result == {"triggered": False, "reason": "running"}  # 合并,不另起一轮
    finally:
        gate.set()
        # 先等本次同步完成(避免 stop 的取消标志把任务打成 CANCELLED),再停止
        deadline = time.time() + 5
        while time.time() < deadline and coordinator.is_running():
            time.sleep(0.05)
        coordinator.stop(timeout=5)
    status = store.read_status()
    assert status["last_result"] == "updated"
