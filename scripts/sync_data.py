"""本地数据同步核心:从 GitHub 固定 commit 拉取白名单 source,校验建库,原子发布。

分工边界:同步器只读 GitHub REST/blob,不执行任何 Git 写操作、不抓榜、不调用
模型、不发送通知;所有文件写入 data/runtime 下独立 generation,全部校验通过后
才原子切换 active 指针。任何失败保留当前可查询的版本。

安全约定:
- 一次同步固定在解析出的 HEAD commit 上,所有文件按该 SHA 读取,不混提交;
- 下载只指向 api.github.com;Token 仅用于该域名的请求头,不写 URL、不入日志;
- Git blob 校验按 Git blob 编码(sha1("blob <len>\\0" + content))计算哈希;
- 白名单之外的路径一律忽略;HTTP 错误不得当作"可选文件不存在"。

CLI:
  python scripts/sync_data.py --once                 # 同步一次(成功 0,失败非 0)
  python scripts/sync_data.py --status               # 只读本地状态,不触发网络
  python scripts/sync_data.py --list-generations     # 列出本地数据版本
  python scripts/sync_data.py --activate-generation ID   # 回滚:激活已验证版本
  python scripts/sync_data.py --build-local          # 用工作区 source 构建初始版本
"""
import csv
import hashlib
import json
import re
import shutil
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from scripts.atomic_io import atomic_write_text
from scripts.db import connect_ro
from scripts.runtime_store import (
    DB_NAME,
    DEFAULT_STATUS,
    RuntimeStore,
    SyncLock,
    SyncLockedError,
    utcnow_iso,
)
from scripts.snapshot_store import load_snapshot
from scripts.source_paths import SourcePaths, default as default_source_paths

# ---------- 白名单 ----------
# 必需文件:缺失即同步失败(保留旧版)
REQUIRED_FILES = {
    "data/raw/repo_meta_snapshot.csv",
    "data/raw/repo_meta_api.jsonl",
    "data/raw/trends_gharchive.csv",
    "data/daily/trends.jsonl",
    "data/profiles/profiles.jsonl",
}
# 可选文件:远端不存在 → 候选中也没有(不继承旧版残留)
OPTIONAL_FILES = {
    "data/raw/star_anomaly_overrides.txt",
    "data/raw/identity_flags.json",
    "data/daily/push_log.jsonl",
}
SNAPSHOT_PATH_RE = re.compile(r"^data/daily/snapshots/\d{4}/\d{2}/\d{4}-\d{2}-\d{2}\.json$")
_ARCHIVE_PREFIX = "data/daily/archive/"


def classify_path(path: str) -> str | None:
    """路径 → 'required' / 'optional' / None(白名单之外)。"""
    if path in REQUIRED_FILES:
        return "required"
    if path in OPTIONAL_FILES:
        return "optional"
    if SNAPSHOT_PATH_RE.match(path):
        return "required"  # canonical 快照:至少要有一份有效快照才算必需集合
    if path.startswith(_ARCHIVE_PREFIX) and path.endswith(".jsonl") \
            and "/" not in path[len(_ARCHIVE_PREFIX):]:
        return "optional"
    return None


def filter_whitelist(tree: dict) -> tuple[dict, list[str]]:
    """tree: {path: {sha, size}} → (白名单条目, 缺失的必需文件列表)。"""
    entries = {p: meta for p, meta in tree.items() if classify_path(p)}
    missing = sorted(REQUIRED_FILES - set(entries))
    if not any(SNAPSHOT_PATH_RE.match(p) for p in entries):
        missing.append("data/daily/snapshots/(至少一份有效快照)")
    return entries, missing


def compute_fingerprint(entries: dict) -> str:
    """数据指纹:白名单路径 + Git blob sha + 大小的稳定摘要(与 commit 解耦)。"""
    payload = "\n".join(
        f"{p}\0{entries[p]['sha']}\0{entries[p].get('size', 0)}" for p in sorted(entries))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------- 错误模型 ----------
class SyncError(RuntimeError):
    """带错误码的同步失败。error_code 用于状态展示与测试断言。"""

    def __init__(self, error_code: str, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.retry_after = retry_after


# ---------- GitHub 客户端 ----------
class GitHubClient:
    """GitHub REST 最小封装:HEAD 提交、git tree、blob 下载。只访问 api.github.com。"""

    API_ROOT = "https://api.github.com"

    def __init__(self, repo: str, branch: str = "main", token: str = "",
                 timeout: float = 30.0, max_retries: int = 3):
        self.repo = repo.strip("/")
        self.branch = branch
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "github-trending-kb-sync",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self.download_calls = 0  # 供测试观察

    def _get(self, url: str, *, accept: str | None = None) -> requests.Response:
        headers = {"Accept": accept} if accept else None
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.get(url, headers=headers, timeout=self.timeout)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_exc = exc
                if attempt == self.max_retries:
                    raise SyncError("NETWORK_ERROR",
                                    f"网络不可用: {type(exc).__name__}") from exc
                time.sleep(min(2 ** attempt, 8))
                continue
            if resp.status_code in (403, 429) \
                    and resp.headers.get("X-RateLimit-Remaining") == "0":
                raise SyncError("RATE_LIMITED", "GitHub 限流,稍后自动重试",
                                retry_after=_rate_limit_retry_after(resp))
            if resp.status_code >= 500:
                last_exc = SyncError("HTTP_ERROR",
                                     f"GitHub 服务端错误: HTTP {resp.status_code}")
                if attempt == self.max_retries:
                    raise last_exc
                time.sleep(min(2 ** attempt, 8))
                continue
            if resp.status_code >= 400:
                raise SyncError("HTTP_ERROR",
                                f"GitHub 请求失败: HTTP {resp.status_code} ({url.split('?')[0]})")
            return resp
        raise last_exc or SyncError("NETWORK_ERROR", "网络不可用")

    def get_head(self) -> str:
        """解析分支 HEAD 的完整 commit SHA(整个同步固定使用它)。"""
        resp = self._get(f"{self.API_ROOT}/repos/{self.repo}/commits/{self.branch}")
        sha = resp.json().get("sha")
        if not sha:
            raise SyncError("HTTP_ERROR", "无法解析远端 HEAD commit")
        return sha

    def get_tree(self, commit_sha: str) -> dict:
        """返回 {path: {sha, size}}。root 递归拉取;截断时补齐遍历,仍截断则失败。"""
        resp = self._get(
            f"{self.API_ROOT}/repos/{self.repo}/git/trees/{commit_sha}?recursive=1")
        data = resp.json()
        if data.get("truncated"):
            return self._walk_tree(commit_sha)
        return _tree_to_map(data)

    def _walk_tree(self, root_sha: str) -> dict:
        """非递归 BFS 遍历(截断兜底);子树仍截断则明确失败。"""
        result: dict = {}
        queue = [root_sha]
        while queue:
            sha = queue.pop()
            resp = self._get(f"{self.API_ROOT}/repos/{self.repo}/git/trees/{sha}")
            data = resp.json()
            if data.get("truncated"):
                raise SyncError("TREE_TRUNCATED", "远端文件清单截断且无法补齐,拒绝不完整数据")
            for item in data.get("tree", []):
                if item.get("type") == "tree":
                    queue.append(item["sha"])
                elif item.get("type") == "blob":
                    result[item["path"]] = {"sha": item["sha"], "size": item.get("size", 0)}
        return result

    def download_blob(self, blob_sha: str, size_hint: int, dest: Path,
                      *, max_file_bytes: int) -> str:
        """下载 blob 到 dest(原子写入),按 Git blob 编码校验 sha1。返回内容 sha256。"""
        self.download_calls += 1
        resp = self._get(f"{self.API_ROOT}/repos/{self.repo}/git/blobs/{blob_sha}",
                         accept="application/vnd.github.raw")
        content = resp.content
        if len(content) > max_file_bytes:
            raise SyncError("SIZE_LIMIT",
                            f"文件超过单文件大小上限: {blob_sha[:8]} ({len(content)} bytes)")
        if size_hint and len(content) != size_hint:
            raise SyncError("HASH_MISMATCH", f"文件大小与远端清单不符: {blob_sha[:8]}")
        expected = hashlib.sha1(f"blob {len(content)}\0".encode("ascii") + content).hexdigest()
        if expected != blob_sha:
            raise SyncError("HASH_MISMATCH", f"Git blob 校验失败: {blob_sha[:8]}")
        sha256 = hashlib.sha256(content).hexdigest()
        atomic_write_text(dest, content.decode("utf-8"))
        return sha256


def _rate_limit_retry_after(resp: requests.Response) -> float | None:
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            pass
    reset = resp.headers.get("X-RateLimit-Reset")
    if reset:
        try:
            return max(0.0, float(reset) - time.time())
        except ValueError:
            pass
    return None


def _tree_to_map(data: dict) -> dict:
    return {item["path"]: {"sha": item["sha"], "size": item.get("size", 0)}
            for item in data.get("tree", []) if item.get("type") == "blob"}


def default_client() -> GitHubClient:
    """按当前 config 构造客户端(调用时读取,便于测试替换)。"""
    import config

    return GitHubClient(config.DATA_SYNC_REPO, branch=config.DATA_SYNC_BRANCH,
                        token=config.DATA_SYNC_TOKEN,
                        timeout=max(10.0, config.DATA_SYNC_TASK_TIMEOUT_SECONDS / 60))


# ---------- source 文件解析校验 ----------
_GHARCHIVE_COLS = {"date", "repo", "stars", "quality"}
_SNAPSHOT_CSV_COLS = {"full_name"}


def validate_source_files(source_root: Path) -> str:
    """候选 source 完整性:白名单文件可完整解析、canonical 快照内容哈希可信。

    返回期望的最新数据日期(canonical 快照最大日期)。
    """
    raw = source_root / "raw"
    daily = source_root / "daily"
    profiles = source_root / "profiles"

    def _fail(msg):
        raise SyncError("INVALID_SOURCE", msg)

    with (raw / "repo_meta_snapshot.csv").open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or not _SNAPSHOT_CSV_COLS.issubset(set(reader.fieldnames)):
            _fail("repo_meta_snapshot.csv 缺少 full_name 列")
        for _ in reader:
            pass
    with (raw / "trends_gharchive.csv").open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or not _GHARCHIVE_COLS.issubset(set(reader.fieldnames)):
            _fail("trends_gharchive.csv 缺少必需列")
        for _ in reader:
            pass
    for jsonl in (raw / "repo_meta_api.jsonl", daily / "trends.jsonl",
                  profiles / "profiles.jsonl", daily / "push_log.jsonl"):
        if not jsonl.exists():
            continue  # push_log 可选
        with jsonl.open(encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                if line.strip():
                    try:
                        json.loads(line)
                    except ValueError as exc:
                        _fail(f"{jsonl.name} 第 {lineno} 行 JSON 损坏: {exc}")
    overrides = raw / "star_anomaly_overrides.txt"
    if overrides.exists():
        from scripts.db import parse_star_anomaly_overrides

        try:
            parse_star_anomaly_overrides(overrides.read_text(encoding="utf-8"))
        except ValueError as exc:
            _fail(f"人工覆盖规则不可解析: {exc}")

    snapshot_root = daily / "snapshots"
    dates = []
    if snapshot_root.exists():
        pattern = "[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].json"
        for path in sorted(snapshot_root.glob(pattern)):
            try:
                snap = load_snapshot(path.stem, snapshot_root)
            except Exception as exc:  # SnapshotValidationError/JSONDecodeError → fail closed
                _fail(f"canonical 快照校验失败: {path.name}: {exc}")
            if snap:
                dates.append(snap["date"])
    if not dates:
        _fail("候选 source 中没有任何有效 canonical 快照")
    return max(dates)


# ---------- 候选数据库校验 ----------
def _real_latest_date(conn) -> str | None:
    row = conn.execute(
        "SELECT MAX(date) m FROM trend_daily WHERE list_type='total'").fetchone()
    if row and row["m"]:
        return row["m"]
    return conn.execute("SELECT MAX(date) m FROM trend_daily").fetchone()["m"]


def _beijing_today() -> date:
    return datetime.now(timezone(timedelta(hours=8))).date()


def check_candidate_db(db_path: Path, expected_latest: str | None,
                       current_db: Path | None):
    """候选库必须:完整、schema/FTS 一致、无未来日期、日期与 source 一致、不倒退。"""
    conn = connect_ro(db_path)
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SyncError("VALIDATION_FAILED", "候选数据库 integrity_check 不通过")
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        missing = {"repos", "trend_daily", "profiles", "push_log", "search_fts"} - tables
        if missing:
            raise SyncError("VALIDATION_FAILED", f"候选数据库缺少表: {sorted(missing)}")
        n_repos = conn.execute("SELECT count(*) FROM repos").fetchone()[0]
        n_fts = conn.execute("SELECT count(*) FROM search_fts").fetchone()[0]
        if n_repos == 0 or n_fts != n_repos:
            raise SyncError("VALIDATION_FAILED", "候选数据库 repos/FTS 行数不一致")

        max_date = conn.execute("SELECT MAX(date) m FROM trend_daily").fetchone()["m"]
        if max_date and max_date > _beijing_today().isoformat():
            raise SyncError("VALIDATION_FAILED", f"候选数据库包含未来日期: {max_date}")
        real_latest = _real_latest_date(conn)
        if expected_latest and max_date != expected_latest:
            raise SyncError("VALIDATION_FAILED",
                            f"候选库最新日期 {max_date} 与 source 快照日期 {expected_latest} 不一致")

        # 关键查询抽样:首页统计、前缀检索、最新日榜单必须可执行
        conn.execute("""
          SELECT (SELECT count(*) FROM repos), (SELECT count(*) FROM profiles),
                 (SELECT count(DISTINCT date) FROM trend_daily)""").fetchone()
        conn.execute("SELECT full_name FROM repos WHERE full_name LIKE ? LIMIT 1",
                     ("a%",)).fetchone()
        if real_latest:
            conn.execute(
                "SELECT rank, full_name FROM trend_daily WHERE date=? AND list_type='total'"
                " ORDER BY rank LIMIT 5", (real_latest,)).fetchone()

        if current_db is not None and current_db.exists():
            cur = None
            try:
                cur = connect_ro(current_db)
                prev_latest = _real_latest_date(cur)
            except Exception:
                prev_latest = None
            finally:
                if cur is not None:
                    cur.close()
            if prev_latest and real_latest and real_latest < prev_latest:
                raise SyncError(
                    "REGRESSION",
                    f"远端候选最新真实榜({real_latest})早于当前版本({prev_latest}),已保留旧版")
    finally:
        conn.close()


# ---------- 下载与缓存复用 ----------
def _rel_target(path: str) -> str:
    """'data/raw/x.csv' → 'raw/x.csv'(staging/source 下的相对路径)。"""
    prefix = "data/"
    return path[len(prefix):] if path.startswith(prefix) else path


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def materialize_entries(client, entries: dict, staging: Path,
                        prev_dir: Path | None, prev_files: dict | None, *,
                        max_file_bytes: int, max_total_bytes: int, preempt) -> dict:
    """按固定 SHA 把白名单文件写入 staging/source;未变化文件复用旧版本并复验。"""
    source_root = staging / "source"
    files: dict = {}
    total = 0
    for path in sorted(entries):
        preempt()
        meta = entries[path]
        size = int(meta.get("size") or 0)
        if size > max_file_bytes:
            raise SyncError("SIZE_LIMIT", f"文件超过单文件大小上限: {path}")
        total += size
        if total > max_total_bytes:
            raise SyncError("SIZE_LIMIT", "整批下载超过总大小上限,已中止")
        dest = source_root / _rel_target(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        prev_meta = (prev_files or {}).get(path)
        if prev_meta and prev_meta.get("blob_sha") == meta["sha"] and prev_dir is not None:
            prev_file = prev_dir / "source" / _rel_target(path)
            if prev_file.exists():
                expected_sha = prev_meta.get("sha256")
                if expected_sha and _file_sha256(prev_file) == expected_sha:
                    shutil.copyfile(prev_file, dest)
                    files[path] = {"size": prev_meta["size"], "sha256": expected_sha,
                                   "blob_sha": meta["sha"]}
                    continue
                # 缓存内容与指纹不符:不盲信,重新下载
        sha256 = client.download_blob(meta["sha"], size, dest, max_file_bytes=max_file_bytes)
        files[path] = {"size": size, "sha256": sha256, "blob_sha": meta["sha"]}
    return files


def _build_candidate(staging: Path) -> Path:
    from scripts.db import rebuild

    source_root = staging / "source"
    sources = SourcePaths(
        raw_dir=source_root / "raw",
        daily_dir=source_root / "daily",
        profiles_dir=source_root / "profiles",
        readme_dir=source_root / "readmes",  # 同步版本无 README 缓存,缺失清单按未知处理
    )
    db_path = staging / DB_NAME
    conn = rebuild(db_path=db_path, sources=sources)
    conn.close()
    return db_path


def _make_manifest(*, generation_id: str, commit: str | None, source: str,
                   build_version: str, fingerprint: str, files: dict,
                   latest_date: str | None, db_path: Path) -> dict:
    counts = {}
    try:
        conn = connect_ro(db_path)
        counts = {
            "repos": conn.execute("SELECT count(*) FROM repos").fetchone()[0],
            "profiles": conn.execute("SELECT count(*) FROM profiles").fetchone()[0],
            "trend_days": conn.execute(
                "SELECT count(DISTINCT date) FROM trend_daily").fetchone()[0],
        }
        conn.close()
    except Exception:
        pass
    return {
        "generation_id": generation_id,
        "commit": commit,
        "source": source,
        "build_version": build_version,
        "fingerprint": fingerprint,
        "files": files,
        "latest_date": latest_date,
        "built_at": utcnow_iso(),
        "counts": counts,
    }


_LOCAL_WHITELIST = [
    "raw/repo_meta_snapshot.csv", "raw/repo_meta_api.jsonl",
    "raw/trends_gharchive.csv", "raw/star_anomaly_overrides.txt",
    "raw/identity_flags.json",
    "daily/trends.jsonl", "daily/push_log.jsonl", "profiles/profiles.jsonl",
]


def local_fingerprint(sources: SourcePaths) -> tuple[str, dict]:
    """工作区 source 的数据指纹与文件清单(按内容 sha256,用于本地初始构建)。

    覆盖与远端白名单一致的集合:固定文件 + canonical 快照(不含 history)+
    push 归档,保证本地版本与远端版本可比较、可接力。
    """
    root = Path(sources.raw_dir).parent
    files: dict = {}

    def add(rel_path: str):
        p = root / rel_path
        if p.exists():
            files[rel_path] = {"size": p.stat().st_size, "sha256": _file_sha256(p)}

    for rel in _LOCAL_WHITELIST:
        add(rel)
    snap_root = root / "daily" / "snapshots"
    if snap_root.exists():
        pattern = "[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].json"
        for p in sorted(snap_root.glob(pattern)):
            add(p.relative_to(root).as_posix())
    archive = root / "daily" / "archive"
    if archive.exists():
        for p in sorted(archive.glob("*.jsonl")):
            add(p.relative_to(root).as_posix())
    payload = "\n".join(f"{k}\0{v['sha256']}\0{v['size']}" for k, v in sorted(files.items()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), files


def _next_check_at(retry_after: float | None) -> str | None:
    if retry_after is None:
        return None
    eta = datetime.now(timezone.utc) + timedelta(seconds=min(retry_after, 6 * 3600))
    return eta.isoformat(timespec="seconds")


# ---------- 主流程 ----------
def run_sync(store: RuntimeStore | None = None, *, client=None,
             build_version: str | None = None, max_file_bytes: int | None = None,
             max_total_bytes: int | None = None, task_timeout: float | None = None,
             cancel_check=None) -> dict:
    """执行一次完整同步。成功返回最终状态;失败写入状态并返回(不抛 SyncError);
    跨进程锁被占用时抛 SyncLockedError(不写状态,锁持有者负责状态)。"""
    import config

    store = store or RuntimeStore()
    build_version = build_version or config.DB_BUILD_VERSION
    max_file_bytes = max_file_bytes or config.DATA_SYNC_MAX_FILE_BYTES
    max_total_bytes = max_total_bytes or config.DATA_SYNC_MAX_TOTAL_BYTES
    task_timeout = task_timeout or config.DATA_SYNC_TASK_TIMEOUT_SECONDS
    started = time.monotonic()

    def preempt():
        if cancel_check is not None and cancel_check():
            raise SyncError("CANCELLED", "同步任务被取消(服务关闭)")
        if time.monotonic() - started > task_timeout:
            raise SyncError("TIMEOUT", f"同步任务超过时限({int(task_timeout)} 秒)")

    def _running(**patch):
        patch.setdefault("running", True)
        return store.write_status(patch)

    def _fail(exc: SyncError) -> dict:
        # 失败状态在仍持锁时写入,避免与下一个同步进程竞争状态文件
        return _running(running=False, phase="idle", last_result="failed",
                        last_checked_at=utcnow_iso(),
                        last_error_code=exc.error_code, last_error_message=exc.message,
                        next_check_at=_next_check_at(exc.retry_after),
                        message=f"同步失败:{exc.message}。当前仍可查询已有数据。")

    lock = SyncLock(store.lock_path).acquire()
    try:
        store.recover_status()
        store.clean_stale_staging()
        staging = None
        try:
            _running(phase="checking", last_error_code=None, last_error_message=None)
            gh = client or default_client()
            head = gh.get_head()
            tree = gh.get_tree(head)
            entries, missing = filter_whitelist(tree)
            if missing:
                raise SyncError("REQUIRED_FILE_MISSING",
                                f"固定 commit 缺少必需文件: {', '.join(missing)}")
            fingerprint = compute_fingerprint(entries)

            active = store.read_active()
            prev_manifest = store.read_manifest(active["generation"]) if active else None
            prev_dir = store.generation_dir(active["generation"]) if active else None
            active_db = store.db_path_for_active()

            if (prev_manifest is not None
                    and prev_manifest.get("fingerprint") == fingerprint
                    and prev_manifest.get("build_version") == build_version
                    and active_db is not None):
                now = utcnow_iso()
                return _running(running=False, phase="idle", last_result="up_to_date",
                                last_checked_at=now, last_success_at=now, remote_commit=head,
                                last_error_code=None, last_error_message=None,
                                message=f"与 GitHub 已同步,数据截至 "
                                        f"{prev_manifest.get('latest_date') or '—'}")

            _running(phase="downloading", remote_commit=head)
            staging = store.new_staging()
            files = materialize_entries(
                gh, entries, staging, prev_dir,
                prev_manifest.get("files") if prev_manifest else None,
                max_file_bytes=max_file_bytes, max_total_bytes=max_total_bytes,
                preempt=preempt)

            _running(phase="validating")
            expected_latest = validate_source_files(staging / "source")

            _running(phase="building")
            db_path = _build_candidate(staging)

            _running(phase="validating")
            check_candidate_db(db_path, expected_latest, active_db)

            _running(phase="publishing")
            preempt()  # 退出中的任务不得继续发布新指针
            gen_id = f"{head[:8]}-bv{build_version}-{fingerprint[:8]}"
            manifest = _make_manifest(
                generation_id=gen_id, commit=head, source="github",
                build_version=build_version, fingerprint=fingerprint, files=files,
                latest_date=expected_latest, db_path=db_path)
            atomic_write_text(staging / "manifest.json",
                              json.dumps(manifest, ensure_ascii=False, indent=1))
            store.publish(staging, gen_id)
            staging = None
            store.activate(gen_id, manifest)
            now = utcnow_iso()
            _running(running=False, phase="idle", last_result="updated",
                     last_checked_at=now, last_success_at=now, last_updated_at=now,
                     remote_commit=head, active_commit=head,
                     active_generation=gen_id, active_source="github",
                     active_latest_date=expected_latest,
                     last_error_code=None, last_error_message=None,
                     message=f"新数据已就绪,更新至 {expected_latest}")
            store.cleanup_generations(gen_id)
            return store.read_status()
        except SyncError as exc:
            if staging is not None and staging.exists():
                store.discard_staging(staging)
            return _fail(exc)
        except Exception as exc:  # 未预期异常也要落状态,不得静默
            if staging is not None and staging.exists():
                store.discard_staging(staging)
            return _fail(SyncError("INTERNAL", f"{type(exc).__name__}: {exc}"))
    finally:
        lock.release()


def build_local_generation(store: RuntimeStore | None = None, *,
                           build_version: str | None = None,
                           sources: SourcePaths | None = None) -> dict:
    """无远端数据时,用工作区 source 在运行目录构建初始版本(commit 为空,source=local)。

    不修改工作区 source;与远端版本共用校验与发布路径。
    """
    import config

    store = store or RuntimeStore()
    build_version = build_version or config.DB_BUILD_VERSION
    src = sources or default_source_paths()

    def _running(**patch):
        patch.setdefault("running", True)
        return store.write_status(patch)

    lock = SyncLock(store.lock_path).acquire()
    try:
        store.recover_status()
        staging = None
        try:
            _running(phase="validating")
            fingerprint, files = local_fingerprint(src)
            active = store.read_active()
            prev_manifest = store.read_manifest(active["generation"]) if active else None
            if (prev_manifest is not None
                    and prev_manifest.get("fingerprint") == fingerprint
                    and prev_manifest.get("build_version") == build_version
                    and store.db_path_for_active() is not None):
                return _running(running=False, phase="idle", last_result="up_to_date",
                                last_success_at=utcnow_iso(),
                                message="本地初始版本已是最新构建")
            staging = store.new_staging()
            _running(phase="downloading")
            source_root = staging / "source"
            data_root = Path(src.raw_dir).parent
            for rel in files:  # 复制工作区 source(只读取,不修改工作区)
                dest = source_root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(data_root / rel, dest)
            expected_latest = validate_source_files(source_root)
            _running(phase="building")
            db_path = _build_candidate(staging)
            _running(phase="validating")
            check_candidate_db(db_path, expected_latest, store.db_path_for_active())
            _running(phase="publishing")
            gen_id = f"local-bv{build_version}-{fingerprint[:8]}"
            manifest = _make_manifest(
                generation_id=gen_id, commit=None, source="local",
                build_version=build_version, fingerprint=fingerprint, files=files,
                latest_date=expected_latest, db_path=db_path)
            atomic_write_text(staging / "manifest.json",
                              json.dumps(manifest, ensure_ascii=False, indent=1))
            store.publish(staging, gen_id)
            staging = None
            store.activate(gen_id, manifest)
            _running(running=False, phase="idle", last_result="updated",
                     last_success_at=utcnow_iso(), last_updated_at=utcnow_iso(),
                     active_generation=gen_id, active_source="local", active_commit=None,
                     active_latest_date=expected_latest,
                     message=f"本地数据已就绪,更新至 {expected_latest}")
            store.cleanup_generations(gen_id)
            return store.read_status()
        except SyncError as exc:
            if staging is not None and staging.exists():
                store.discard_staging(staging)
            return _running(running=False, phase="idle", last_result="failed",
                            last_error_code=exc.error_code,
                            last_error_message=exc.message,
                            message=f"本地建库失败:{exc.message}")
        except Exception as exc:
            if staging is not None and staging.exists():
                store.discard_staging(staging)
            return _running(running=False, phase="idle", last_result="failed",
                            last_error_code="INTERNAL",
                            last_error_message=f"{type(exc).__name__}: {exc}",
                            message=f"本地建库失败:内部错误({type(exc).__name__})")
    finally:
        lock.release()


# ---------- CLI ----------
def _cli(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="本地数据同步(GitHub → runtime 版本库)")
    parser.add_argument("--once", action="store_true", help="同步一次")
    parser.add_argument("--build-local", action="store_true",
                        help="用工作区 source 构建本地初始版本")
    parser.add_argument("--status", action="store_true", help="只读本地状态,不触发网络")
    parser.add_argument("--list-generations", action="store_true", help="列出本地数据版本")
    parser.add_argument("--activate-generation", metavar="ID",
                        help="回滚:激活指定已验证版本(受锁保护)")
    args = parser.parse_args(argv)

    store = RuntimeStore()
    if args.status:
        print(json.dumps(dict(DEFAULT_STATUS) | store.read_status(),
                         ensure_ascii=False, indent=2))
        return 0
    if args.list_generations:
        gens_root = store.generations_root
        if not gens_root.exists():
            print("暂无本地数据版本")
            return 0
        active = store.read_active() or {}
        for child in sorted(gens_root.iterdir()):
            manifest = store.read_manifest(child.name) or {}
            mark = " <- active" if child.name == active.get("generation") else ""
            print(f"{child.name}  commit={manifest.get('commit') or '-'}"
                  f"  date={manifest.get('latest_date') or '-'}"
                  f"  built={manifest.get('built_at') or '-'}{mark}")
        return 0
    if args.activate_generation:
        try:
            lock = SyncLock(store.lock_path).acquire()
        except SyncLockedError as exc:
            print(f"无法激活: {exc}", file=sys.stderr)
            return 3
        try:
            manifest = store.read_manifest(args.activate_generation)
            if not manifest:
                print(f"未找到 generation: {args.activate_generation}", file=sys.stderr)
                return 1
            if not (store.generation_dir(args.activate_generation) / DB_NAME).exists():
                print("该 generation 缺少数据库文件,拒绝激活", file=sys.stderr)
                return 1
            store.activate(args.activate_generation, manifest)
            store.write_status({"running": False, "phase": "idle",
                                "last_result": "updated",
                                "last_success_at": utcnow_iso(),
                                "last_updated_at": utcnow_iso(),
                                "active_generation": args.activate_generation,
                                "active_commit": manifest.get("commit"),
                                "active_source": manifest.get("source"),
                                "active_latest_date": manifest.get("latest_date"),
                                "message": f"已切换到数据版本 {args.activate_generation}"})
        finally:
            lock.release()
        print(f"已激活 {args.activate_generation}")
        return 0
    if args.build_local:
        status = build_local_generation(store)
        print(status.get("message") or status.get("last_result"))
        return 0 if status.get("last_result") in ("updated", "up_to_date") else 1
    if args.once:
        try:
            status = run_sync(store)
        except SyncLockedError as exc:
            print(f"同步被跳过: {exc}", file=sys.stderr)
            return 3
        print(status.get("message") or status.get("last_result"))
        if status.get("last_error_code"):
            print(f"错误码: {status['last_error_code']}", file=sys.stderr)
        return 0 if status.get("last_result") in ("updated", "up_to_date") else 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
