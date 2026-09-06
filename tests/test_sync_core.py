"""本地数据同步核心测试:固定 SHA、白名单、校验、发布、恢复、并发与 CLI。

全部外部交互走 FakeClient/StubSession(离线);runtime 目录在 tmp_path 内。
"""
import json

import pytest
import requests
from conftest import FakeClient, StubResp, StubSession, git_blob_sha, make_remote_files

from scripts.runtime_store import RuntimeStore, SyncLock, SyncLockedError
from scripts.sync_data import (
    GitHubClient,
    _cli,
    build_local_generation,
    classify_path,
    compute_fingerprint,
    filter_whitelist,
    run_sync,
)


@pytest.fixture()
def store(tmp_path):
    return RuntimeStore(runtime_dir=tmp_path / "runtime")


def make_client(files=None, **kwargs) -> FakeClient:
    return FakeClient(files if files is not None else make_remote_files(), **kwargs)


def active_manifest(store: RuntimeStore) -> dict:
    active = store.read_active()
    assert active is not None, "没有激活版本"
    manifest = store.read_manifest(active["generation"])
    assert manifest is not None
    return manifest


def candidate_conn(store: RuntimeStore):
    import sqlite3

    db = store.db_path_for_active()
    assert db is not None
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ---------- 白名单与指纹 ----------

def test_classify_path_whitelist():
    assert classify_path("data/raw/repo_meta_api.jsonl") == "required"
    assert classify_path("data/daily/snapshots/2026/09/2026-09-05.json") == "required"
    assert classify_path("data/raw/star_anomaly_overrides.txt") == "optional"
    assert classify_path("data/daily/push_log.jsonl") == "optional"
    assert classify_path("data/daily/archive/2026-08.jsonl") == "optional"
    # 白名单之外:代码、README 缓存、history、嵌套归档、远端 SQLite
    assert classify_path("scripts/db.py") is None
    assert classify_path("data/readmes/_missing.txt") is None
    assert classify_path("data/daily/snapshots/history/2026/09/x.json") is None
    assert classify_path("data/daily/archive/sub/x.jsonl") is None
    assert classify_path("data/trending.db") is None
    assert classify_path(".env") is None


def test_filter_whitelist_missing_required():
    files = make_remote_files()
    files.pop("data/daily/trends.jsonl")
    entries, missing = filter_whitelist({p: {"sha": git_blob_sha(c), "size": len(c)}
                                         for p, c in files.items()})
    assert "data/daily/trends.jsonl" in missing
    assert not entries.get("data/daily/trends.jsonl")


def test_compute_fingerprint_stable_and_sensitive():
    files = make_remote_files()
    tree = {p: {"sha": git_blob_sha(c), "size": len(c)} for p, c in files.items()}
    assert compute_fingerprint(tree) == compute_fingerprint(dict(reversed(list(tree.items()))))
    tree2 = dict(tree)
    tree2["data/profiles/profiles.jsonl"] = {"sha": "0" * 40, "size": 1}
    assert compute_fingerprint(tree) != compute_fingerprint(tree2)


# ---------- 主流程:下载、校验、发布 ----------

def test_first_sync_publishes_generation(store):
    status = run_sync(store, client=make_client())
    assert status["last_result"] == "updated"
    manifest = active_manifest(store)
    assert manifest["commit"] == "1" * 40
    assert manifest["source"] == "github"
    assert manifest["latest_date"] == "2026-08-30"
    assert store.db_path_for_active() is not None
    conn = candidate_conn(store)
    repos = {r["full_name"] for r in conn.execute("SELECT full_name FROM repos")}
    conn.close()
    assert "owner0/repo0" in repos


def test_second_run_up_to_date_no_download(store):
    files = make_remote_files()
    first = FakeClient(files)
    run_sync(store, client=first)
    second = FakeClient(files, head="2" * 40)  # commit 前进,数据未变
    status = run_sync(store, client=second)
    assert status["last_result"] == "up_to_date"
    assert second.download_calls == 0  # 未下载任何文件
    manifest = active_manifest(store)
    assert manifest["commit"] == "1" * 40      # 数据仍是旧 commit 构建的身份
    assert status["remote_commit"] == "2" * 40  # 远端检查时间与指针如实更新


def test_data_change_triggers_rebuild(store):
    run_sync(store, client=make_client(make_remote_files()))
    files2 = make_remote_files(day="2026-08-31")
    status = run_sync(store, client=make_client(files2))
    assert status["last_result"] == "updated"
    assert status["active_latest_date"] == "2026-08-31"
    conn = candidate_conn(store)
    days = {r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM trend_daily WHERE list_type='total'")}
    conn.close()
    assert "2026-08-31" in days and "2026-08-30" not in days


def test_profile_change_same_day_rebuilds(store):
    """最新采集日期相同 ≠ 无需同步:画像更新也要重建。"""
    run_sync(store, client=make_client(make_remote_files()))
    files2 = make_remote_files()  # 同一天
    files2["data/profiles/profiles.jsonl"] = (
        json.dumps({"full_name": "owner0/repo0", "one_liner": "更新后的画像",
                    "source": "glm-api"}, ensure_ascii=False) + "\n").encode("utf-8")
    status = run_sync(store, client=make_client(files2))
    assert status["last_result"] == "updated"
    conn = candidate_conn(store)
    one_liner = conn.execute(
        "SELECT one_liner FROM profiles WHERE full_name='owner0/repo0'").fetchone()[0]
    conn.close()
    assert one_liner == "更新后的画像"


def test_build_version_change_rebuilds(store):
    run_sync(store, client=make_client(), build_version="1")
    status = run_sync(store, client=make_client(), build_version="2")
    assert status["last_result"] == "updated"
    assert "-bv2-" in status["active_generation"]


def test_fixed_sha_used_for_all_downloads(store):
    """同步开始后远端再变化:本次所有文件仍来自最初看到的清单。"""
    class FixedShaClient(FakeClient):
        """get_tree 返回后冻结文件表;模拟 main 在下载中途前进。"""

        def __init__(self, files):
            super().__init__(files)
            self._frozen = None

        def get_tree(self, sha):
            self._frozen = dict(self.files)
            tree = super().get_tree(sha)
            self.files = make_remote_files(day="2026-08-31")  # 远端已前进
            return tree

        def download_blob(self, sha, size, dest, *, max_file_bytes):
            current, self.files = self.files, self._frozen
            try:
                return super().download_blob(sha, size, dest,
                                             max_file_bytes=max_file_bytes)
            finally:
                self.files = current

    status = run_sync(store, client=FixedShaClient(make_remote_files()))
    assert status["last_result"] == "updated"
    assert status["active_latest_date"] == "2026-08-30"  # 整批仍是固定 SHA 的数据


def test_required_file_missing_keeps_old_version(store):
    run_sync(store, client=make_client())
    old_manifest = active_manifest(store)
    broken = make_remote_files()
    broken.pop("data/profiles/profiles.jsonl")
    status = run_sync(store, client=make_client(broken))
    assert status["last_result"] == "failed"
    assert status["last_error_code"] == "REQUIRED_FILE_MISSING"
    assert store.read_active()["generation"] == old_manifest["generation_id"]


def test_optional_file_removed_not_inherited(store):
    overrides = b"exclude 2022-03-01 owner0/repo0\n"
    files1 = make_remote_files(extra_files={"data/raw/star_anomaly_overrides.txt": overrides})
    run_sync(store, client=make_client(files1))
    conn = candidate_conn(store)
    flagged = conn.execute(
        "SELECT star_anomaly FROM trend_daily WHERE date='2022-03-01' "
        "AND full_name='owner0/repo0'").fetchone()[0]
    conn.close()
    assert flagged == 1
    status = run_sync(store, client=make_client())  # 远端移除覆盖规则
    assert status["last_result"] == "updated"
    conn = candidate_conn(store)
    flagged = conn.execute(
        "SELECT star_anomaly FROM trend_daily WHERE date='2022-03-01' "
        "AND full_name='owner0/repo0'").fetchone()[0]
    conn.close()
    assert flagged == 0  # 候选中同步移除,不残留旧覆盖


def test_corrupted_cache_re_downloaded(store):
    files = make_remote_files()
    run_sync(store, client=make_client(files))
    active = store.read_active()
    cached = (store.generation_dir(active["generation"]) / "source"
              / "daily" / "trends.jsonl")
    cached.write_text("corrupted", encoding="utf-8")  # 内容破坏但 blob 标识不变
    files2 = make_remote_files()
    files2["data/profiles/profiles.jsonl"] = (
        json.dumps({"full_name": "owner0/repo0", "one_liner": "v2",
                    "source": "glm-api"}, ensure_ascii=False) + "\n").encode("utf-8")
    client2 = make_client(files2)
    status = run_sync(store, client=client2)
    assert status["last_result"] == "updated"
    assert client2.download_calls >= 1  # 缓存校验失败 → 重新下载,不盲信


def test_size_limit_rejected(store):
    files = make_remote_files()
    status = run_sync(store, client=make_client(files), max_file_bytes=10)
    assert status["last_result"] == "failed"
    assert status["last_error_code"] == "SIZE_LIMIT"
    assert store.read_active() is None


# ---------- 失败语义:网络、限流、哈希、日期倒退 ----------

def _stub_github(responses) -> GitHubClient:
    client = GitHubClient("Neal-1991/github-trending-kb", max_retries=0)
    client.session = StubSession(responses)
    return client


def test_rate_limit_failure_records_retry_after(store):
    client = _stub_github([
        StubResp(status_code=403, headers={"X-RateLimit-Remaining": "0",
                                           "Retry-After": "120"}),
    ])
    status = run_sync(store, client=client)
    assert status["last_result"] == "failed"
    assert status["last_error_code"] == "RATE_LIMITED"
    assert status["next_check_at"] is not None  # 尊重服务端限流重试信息


def test_network_failure_keeps_old_version(store):
    run_sync(store, client=make_client())
    old = active_manifest(store)
    client = _stub_github([requests.ConnectionError("boom")])
    status = run_sync(store, client=client)
    assert status["last_result"] == "failed"
    assert status["last_error_code"] == "NETWORK_ERROR"
    assert store.read_active()["generation"] == old["generation_id"]


def test_blob_hash_mismatch_rejected(store):
    files = make_remote_files()
    tree = [{"path": p, "type": "blob", "sha": git_blob_sha(c), "size": len(c)}
            for p, c in sorted(files.items())]
    client = GitHubClient("o/r", max_retries=0)
    client.session = StubSession([
        StubResp(json_data={"sha": "a" * 40}),                    # get_head
        StubResp(json_data={"tree": tree, "truncated": False}),   # get_tree
        StubResp(content=b"tampered content"),                    # 第一个下载被篡改
    ])
    status = run_sync(store, client=client)
    assert status["last_result"] == "failed"
    assert status["last_error_code"] == "HASH_MISMATCH"


def test_tree_truncated_fails(store):
    client = GitHubClient("o/r", max_retries=0)
    client.session = StubSession([
        StubResp(json_data={"sha": "a" * 40}),
        StubResp(json_data={"truncated": True, "tree": []}),   # 递归拉取截断
        StubResp(json_data={"truncated": True, "tree": []}),   # 补齐遍历仍截断
    ])
    status = run_sync(store, client=client)
    assert status["last_result"] == "failed"
    assert status["last_error_code"] == "TREE_TRUNCATED"


def test_regression_rejected(store):
    run_sync(store, client=make_client(make_remote_files(day="2026-08-31")))
    old = active_manifest(store)
    status = run_sync(store, client=make_client(make_remote_files(day="2026-08-30")))
    assert status["last_result"] == "failed"
    assert status["last_error_code"] == "REGRESSION"
    assert store.read_active()["generation"] == old["generation_id"]


# ---------- 并发、取消、恢复、清理 ----------

def test_cross_process_lock_prevents_second_sync(store):
    run_sync(store, client=make_client())  # 状态文件已存在
    lock = SyncLock(store.lock_path).acquire()
    try:
        with pytest.raises(SyncLockedError):
            run_sync(store, client=make_client())
    finally:
        lock.release()
    # 锁被占用时不得写状态(避免与持锁者竞争)
    assert store.read_status()["last_result"] == "updated"


def test_cancel_before_publish_keeps_pointer(store):
    def cancel_when_publishing():
        return store.read_status()["phase"] == "publishing"

    status = run_sync(store, client=make_client(), cancel_check=cancel_when_publishing)
    assert status["last_result"] == "failed"
    assert status["last_error_code"] == "CANCELLED"
    assert store.read_active() is None           # 未发布新指针
    assert not any(store.staging_root.iterdir())  # 候选已清理


def test_status_recovery_from_active_and_manifest(store):
    run_sync(store, client=make_client())
    gen = store.read_active()["generation"]
    # 模拟指针发布后状态写失败/进程重启:状态文件损坏成"运行中"
    store.status_path.write_text(json.dumps({"running": True, "phase": "downloading",
                                             "last_result": "failed"}),
                                 encoding="utf-8")
    status = store.recover_status()
    assert status["running"] is False
    assert status["phase"] == "idle"
    assert status["active_generation"] == gen
    assert status["active_latest_date"] == "2026-08-30"  # 以 manifest 为准


def test_status_recovery_after_status_file_lost(store):
    run_sync(store, client=make_client())
    gen = store.read_active()["generation"]
    store.status_path.unlink()
    status = store.recover_status()
    assert status["active_generation"] == gen
    assert status["active_commit"] == "1" * 40


def test_old_generations_cleaned_keep_two(store):
    run_sync(store, client=make_client(make_remote_files(day="2026-08-28")))
    run_sync(store, client=make_client(make_remote_files(day="2026-08-29")))
    run_sync(store, client=make_client(make_remote_files(day="2026-08-30")))
    gens = sorted(p.name for p in store.generations_root.iterdir())
    assert len(gens) == 2  # active + 上一版,更老的已清理
    active = store.read_active()["generation"]
    assert active in gens


# ---------- 本地初始构建 ----------

def test_build_local_generation_from_workspace(sandbox, store):
    from conftest import write_source_files

    from scripts.snapshot_store import build_snapshot
    from scripts.source_paths import SourcePaths

    write_source_files(sandbox, repos=2, trend_days=1, profiles=1)
    snap = build_snapshot("2026-09-02", [{"list_type": "total", "entries": [
        {"rank": i + 1, "repo": f"owner{i}/repo{i}", "stars_today": 5}
        for i in range(2)]}])
    snap_dir = sandbox["daily"] / "snapshots" / "2026" / "09"
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "2026-09-02.json").write_text(
        json.dumps(snap, ensure_ascii=False), encoding="utf-8")

    sources = SourcePaths(raw_dir=sandbox["raw"], daily_dir=sandbox["daily"],
                          profiles_dir=sandbox["profiles"], readme_dir=sandbox["readmes"])
    status = build_local_generation(store, sources=sources)
    assert status["last_result"] == "updated"
    manifest = active_manifest(store)
    assert manifest["source"] == "local"
    assert manifest["commit"] is None  # 本地版本不伪装成 GitHub SHA
    assert status["active_latest_date"] == "2026-09-02"
    # 工作区 source 未被修改
    assert (sandbox["raw"] / "trends_gharchive.csv").stat().st_size > 0


# ---------- CLI ----------

def test_cli_once_and_status(sandbox, store, capsys, monkeypatch):
    import scripts.sync_data as sd

    monkeypatch.setattr(sd, "default_client", lambda: make_client())
    assert _cli(["--once"]) == 0
    assert _cli(["--once"]) == 0  # 第二次 up_to_date
    out = capsys.readouterr().out
    assert "已同步" in out or "up_to_date" in out
    assert _cli(["--status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["active_generation"]


def test_cli_once_failure_exit_code(sandbox, store, monkeypatch):
    import scripts.sync_data as sd

    files = make_remote_files()
    files.pop("data/daily/trends.jsonl")
    monkeypatch.setattr(sd, "default_client", lambda: make_client(files))
    assert _cli(["--once"]) == 1


def test_cli_activate_generation_rollback(sandbox, store, capsys):
    run_sync(store, client=make_client(make_remote_files(day="2026-08-28")))
    first = store.read_active()["generation"]
    run_sync(store, client=make_client(make_remote_files(day="2026-08-29")))
    assert store.read_active()["generation"] != first
    assert _cli(["--activate-generation", first]) == 0
    assert store.read_active()["generation"] == first
    assert store.db_path_for_active() is not None
