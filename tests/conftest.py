"""测试公共设施:临时目录、隔离的 source 布局、最小样本数据。

所有测试不访问真实网络、不读取 .env 中的秘密、不写仓库内 data/ 目录。
同步测试的假远端/假客户端设施也在本文件(git_blob_sha/make_remote_files/FakeClient)。
"""
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_WATCHED = ["daily/push_log.jsonl", "daily/trends.jsonl", "daily/doc_log.jsonl",
            "daily/delivery_log.jsonl", "profiles/profiles.jsonl",
            "profiles/pending_queue.json",
            "raw/repo_meta_api.jsonl", "raw/repo_gone.jsonl"]


@pytest.fixture(autouse=True)
def block_real_requests(monkeypatch):
    """所有测试默认禁止 requests 出网；外部服务必须在调用边界显式 mock。"""
    import requests

    def blocked(*args, **kwargs):
        raise AssertionError("测试不得调用真实网络，请 mock 外部服务")

    monkeypatch.setattr(requests.sessions.Session, "request", blocked)


@pytest.fixture(autouse=True)
def protect_real_data():
    """任何测试若写入仓库真实 data/,立即失败。"""
    real = ROOT / "data"
    def snap():
        return {w: (real / w).read_bytes() if (real / w).exists() else None
                for w in _WATCHED}
    before = snap()
    yield
    after = snap()
    for w in _WATCHED:
        if before[w] != after[w]:
            tail = (after[w] or b"")[-300:].decode("utf-8", "replace")
            raise AssertionError(
                f"测试污染了真实数据文件 data/{w}\n新增内容尾部: {tail!r}")


@pytest.fixture(autouse=True)
def disable_auto_sync(monkeypatch):
    """测试默认关闭本地自动同步:任何 TestClient/lifespan 都不得偷跑真实同步。

    需要验证后台生命周期的用例,在用例内显式 monkeypatch 打开并注入假客户端。
    """
    import config

    monkeypatch.setattr(config, "DATA_SYNC_ENABLED", False)


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """把 config 与各模块的路径全部切到 tmp_path,返回目录句柄。"""
    import config
    import scripts.db as db_mod
    import scripts.delivery_log as delivery
    import scripts.snapshot_store as snap

    dirs = {
        "root": tmp_path,
        "raw": tmp_path / "raw",
        "daily": tmp_path / "daily",
        "profiles": tmp_path / "profiles",
        "readmes": tmp_path / "readmes",
        "runtime": tmp_path / "runtime",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "trending.db"

    monkeypatch.setattr(config, "RAW_DIR", dirs["raw"])
    monkeypatch.setattr(config, "DAILY_DIR", dirs["daily"])
    monkeypatch.setattr(config, "PROFILE_DIR", dirs["profiles"])
    monkeypatch.setattr(config, "README_DIR", dirs["readmes"])
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "DATA_RUNTIME_DIR", dirs["runtime"])
    # 各模块 from-import 的副本也要替换;模块可能随提交顺序尚未存在,容错处理
    monkeypatch.setattr(db_mod, "RAW_DIR", dirs["raw"])
    monkeypatch.setattr(db_mod, "DAILY_DIR", dirs["daily"])
    monkeypatch.setattr(db_mod, "PROFILE_DIR", dirs["profiles"])
    monkeypatch.setattr(db_mod, "README_DIR", dirs["readmes"])
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    try:
        import scripts.snapshot_store as snap
    except ImportError:
        snap = None
    if snap:
        monkeypatch.setattr(snap, "DAILY_DIR", dirs["daily"])
        monkeypatch.setattr(snap, "SNAPSHOT_DIR", dirs["daily"] / "snapshots")
        monkeypatch.setattr(snap, "SNAPSHOT_HISTORY_DIR", dirs["daily"] / "snapshots" / "history")
    try:
        import scripts.delivery_log as delivery
    except ImportError:
        delivery = None
    if delivery:
        monkeypatch.setattr(delivery, "DELIVERY_LOG", dirs["daily"] / "delivery_log.jsonl")
        monkeypatch.setattr(delivery, "LEGACY_DOC_LOG", dirs["daily"] / "doc_log.jsonl")
        monkeypatch.setattr(delivery, "LEGACY_PUSH_LOG", dirs["daily"] / "push_log.jsonl")
    # daily_job 模块内的路径副本同样要切走,防止测试写入仓库真实 data/
    try:
        import scripts.daily_job as dj
    except ImportError:
        dj = None
    if dj:
        for attr, val in [("DAILY_DIR", dirs["daily"]), ("RAW_DIR", dirs["raw"]),
                          ("PROFILE_DIR", dirs["profiles"]), ("README_DIR", dirs["readmes"]),
                          ("RAW_META", dirs["raw"] / "repo_meta_api.jsonl"),
                          ("COMPAT_TRENDS", dirs["daily"] / "trends.jsonl")]:
            monkeypatch.setattr(dj, attr, val, raising=False)
    try:
        import scripts.fetch_readmes as readmes
    except ImportError:
        readmes = None
    if readmes:
        monkeypatch.setattr(readmes, "README_DIR", dirs["readmes"])
        monkeypatch.setattr(readmes, "MISSING_LOG", dirs["readmes"] / "_missing.txt")
    dirs["db"] = db_path
    return dirs


def write_source_files(dirs: dict, *, repos=3, trend_days=2, profiles=1, real_days=1):
    """写入最小 source of truth 样本,供 rebuild 使用。"""
    meta_rows = ["full_name,owner_type,description,fork,created_at,pushed_at,homepage,"
                 "stargazers_count,forks_count,subscribers_count,language,archived,"
                 "open_issues_count,license_key,topics,default_branch"]
    names = []
    for i in range(repos):
        name = f"owner{i}/repo{i}"
        names.append(name)
        meta_rows.append(f"{name},User,desc {i},false,2022-01-0{i + 1}T00:00:00Z,"
                         f"2022-06-01T00:00:00Z,,{100 + i},10,5,Python,false,2,MIT,agent,main")
    (dirs["raw"] / "repo_meta_snapshot.csv").write_text("\n".join(meta_rows) + "\n", encoding="utf-8")

    arch = ["date,repo,stars,quality"]
    for d in range(trend_days):
        day = f"2022-03-0{d + 1}"
        for i, name in enumerate(names):
            arch.append(f"{day},{name},{100 - i * 10},full")
    (dirs["raw"] / "trends_gharchive.csv").write_text("\n".join(arch) + "\n", encoding="utf-8")

    real = []
    for d in range(real_days):
        entries = [{"rank": i + 1, "repo": names[i], "description": None, "language": "Python",
                    "stars_total": 100, "stars_today": 30 - i, "forks": 5}
                   for i in range(min(repos, 10))]
        real.append({"date": f"2026-09-0{d + 1}", "list_type": "total", "entries": entries})
    with (dirs["daily"] / "trends.jsonl").open("a", encoding="utf-8") as f:
        for rec in real:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    profs = []
    for i in range(profiles):
        profs.append({"full_name": f"owner{i}/repo{i}", "one_liner": f"项目{i}简介",
                      "purpose": "用途", "boundaries": "边界", "tech_highlights": "技术",
                      "maturity": "成熟", "model": "glm-test", "source": "glm-api",
                      "generated_at": "2026-09-01T08:00:00+08:00"})
    with (dirs["profiles"] / "profiles.jsonl").open("a", encoding="utf-8") as f:
        for p in profs:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    return names


def make_trending_html(n: int, *, stars_today: bool = True, repos: list[str] | None = None) -> str:
    """生成 n 个 article.Box-row 的 trending 页面样本。"""
    repos = repos or [f"owner{i}/repo{i}" for i in range(n)]
    articles = []
    for i, repo in enumerate(repos):
        star_html = ('<span class="d-inline-block float-sm-right">'
                     f"{2250 - i * 10} stars today</span>") if stars_today else ""
        articles.append(f"""
        <article class="Box-row">
          <h2><a href="/{repo}">{repo}</a></h2>
          <p>描述 {i}</p>
          <span itemprop="programmingLanguage">Python</span>
          <a class="Link--muted" href="/{repo}/stargazers">{1000 - i} stars</a>
          <a class="Link--muted" href="/{repo}/forks">50 forks</a>
          {star_html}
        </article>""")
    return f"<html><body>{''.join(articles)}</body></html>"


# ---------- 本地数据同步测试设施(假远端 + 假客户端,全部离线) ----------

def git_blob_sha(content: bytes) -> str:
    """Git blob 对象哈希:sha1("blob <len>\0" + content)。"""
    return hashlib.sha1(f"blob {len(content)}\0".encode("ascii") + content).hexdigest()


def make_remote_files(*, day="2026-08-30", repos=3, arch_stars=100,
                      extra_files: dict | None = None) -> dict[str, bytes]:
    """构造一份"远端仓库白名单文件"内容映射,模拟 GitHub 上已提交的 source。

    默认包含全部必需文件 + 一份 canonical 快照(day);快照日期晚于 legacy
    trends.jsonl(2026-08-01)与历史重建榜(2022-03-01),保证候选库最新日期
    等于快照日期(与 validate_source_files 的期望一致)。
    """
    from scripts.snapshot_store import build_snapshot

    names = [f"owner{i}/repo{i}" for i in range(repos)]
    meta_header = ("full_name,owner_type,description,fork,created_at,pushed_at,homepage,"
                   "stargazers_count,forks_count,subscribers_count,language,archived,"
                   "open_issues_count,license_key,topics,default_branch")
    lines = [meta_header]
    api_lines = []
    for i, name in enumerate(names):
        lines.append(f"{name},User,remote desc {i},false,2022-01-0{i + 1}T00:00:00Z,"
                     f"2022-06-01T00:00:00Z,,{100 + i},10,5,Python,false,2,MIT,remote,main")
        api_lines.append(json.dumps({
            "full_name": name, "description": f"api desc {i}", "language": "Python",
            "stars": 100 + i, "forks": 10, "open_issues": 2, "topics": ["sync"],
            "created_at": "2022-01-01T00:00:00Z", "pushed_at": "2022-06-01T00:00:00Z",
            "fetched_at": f"{day}T08:00:00+08:00"}, ensure_ascii=False))
    profiles = [json.dumps({
        "full_name": names[0], "one_liner": "远程画像", "purpose": "用途",
        "boundaries": "边界", "tech_highlights": "技术", "maturity": "成熟",
        "model": "glm-test", "source": "glm-api",
        "generated_at": f"{day}T08:00:00+08:00"}, ensure_ascii=False)]
    legacy = {"date": "2026-08-01", "list_type": "total", "entries": [
        {"rank": i + 1, "repo": names[i], "description": None, "language": "Python",
         "stars_total": 100 + i, "stars_today": 10 - i, "forks": 5}
        for i in range(min(repos, 2))]}
    snapshot = build_snapshot(day, [{"list_type": "total", "entries": [
        {"rank": i + 1, "repo": names[i], "description": f"快照描述 {i}",
         "language": "Python", "stars_total": 100 + i, "stars_today": 30 - i, "forks": 5}
        for i in range(repos)]}])
    files = {
        "data/raw/repo_meta_snapshot.csv": ("\n".join(lines) + "\n").encode("utf-8"),
        "data/raw/repo_meta_api.jsonl": ("\n".join(api_lines) + "\n").encode("utf-8"),
        "data/raw/trends_gharchive.csv":
            (f"date,repo,stars,quality\n2022-03-01,{names[0]},{arch_stars},full\n").encode("utf-8"),
        "data/daily/trends.jsonl": (json.dumps(legacy, ensure_ascii=False) + "\n").encode("utf-8"),
        "data/profiles/profiles.jsonl": ("\n".join(profiles) + "\n").encode("utf-8"),
        f"data/daily/snapshots/{day[:4]}/{day[5:7]}/{day}.json":
            (json.dumps(snapshot, ensure_ascii=False) + "\n").encode("utf-8"),
    }
    if extra_files:
        files.update(extra_files)
    return files


class FakeClient:
    """run_sync 的假 GitHub 客户端:内存文件表 + Git blob sha 语义。

    fail_downloads: {第 N 次下载(1 起): SyncError} 用于注入下载中途失败。
    """

    def __init__(self, files: dict[str, bytes], head: str = "1" * 40,
                 fail_downloads: dict[int, Exception] | None = None):
        self.files = files
        self.head = head
        self.download_calls = 0
        self.fail_downloads = fail_downloads or {}

    def get_head(self) -> str:
        return self.head

    def get_tree(self, commit_sha: str) -> dict:
        return {p: {"sha": git_blob_sha(c), "size": len(c)}
                for p, c in self.files.items()}

    def download_blob(self, blob_sha: str, size_hint: int, dest: Path,
                      *, max_file_bytes: int) -> str:
        self.download_calls += 1
        if self.download_calls in self.fail_downloads:
            raise self.fail_downloads[self.download_calls]
        for content in self.files.values():
            if git_blob_sha(content) == blob_sha:
                assert len(content) == size_hint
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(content)
                return hashlib.sha256(content).hexdigest()
        raise AssertionError(f"未知 blob: {blob_sha[:8]}")


class StubResp:
    def __init__(self, status_code=200, json_data=None, content=b"", headers=None):
        self.status_code = status_code
        self._json = json_data
        self.content = content
        self.headers = headers or {}

    def json(self):
        return self._json


class StubSession:
    """按序弹出的 HTTP 应答栈;元素为 StubResp 或待抛异常。"""

    def __init__(self, responses):
        self.headers: dict = {}
        self.responses = list(responses)

    def get(self, url, headers=None, timeout=None):
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
