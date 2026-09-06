"""运行版本存储:data/runtime 下的 generation 目录、active 指针、跨进程锁与状态。

布局(整个目录 gitignore,不覆盖工作区 source):
  data/runtime/
    sync.lock            跨进程同步互斥(文件句柄锁,进程退出自动释放)
    sync_status.json     同步状态(原子写入)
    active.json          唯一激活指针(原子替换,只引用 runtime 内 generation)
    staging/<run-id>/    尚未通过校验的候选
    generations/<gen-id>/source/... + trending.db + manifest.json

关键约束:
- active.json 只引用 runtime 管理目录内的 generation,拒绝路径逃逸;
- 发布 = staging 整体移入 generations/ 后原子替换 active.json,不覆盖正在
  使用的 SQLite 文件(Windows 上旧读句柄会阻止替换);
- 指针发布后状态写失败/进程重启:状态从 active + manifest 恢复,新版仍生效;
- 清理只删 active 与上一版之外的老 generation,删除失败跳过留待下次。
"""
import json
import os
import re
import shutil
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.atomic_io import atomic_write_json

ACTIVE_NAME = "active.json"
STATUS_NAME = "sync_status.json"
LOCK_NAME = "sync.lock"
MANIFEST_NAME = "manifest.json"
DB_NAME = "trending.db"

_GEN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# 状态字段与缺省值(last_result: never_synced/updated/up_to_date/failed/disabled)
DEFAULT_STATUS = {
    "phase": "idle",              # idle/checking/downloading/validating/building/publishing
    "running": False,
    "last_result": "never_synced",
    "last_checked_at": None,
    "last_success_at": None,
    "last_updated_at": None,
    "remote_commit": None,
    "active_commit": None,
    "active_generation": None,
    "active_source": None,
    "active_latest_date": None,
    "last_error_code": None,
    "last_error_message": None,
    "next_check_at": None,
    "message": None,
}


class SyncLockedError(RuntimeError):
    """跨进程同步锁被占用(另一个 CLI/服务正在同步)。"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SyncLock:
    """跨进程互斥锁:独占文件句柄 + 平台字节范围锁。

    Windows 用 msvcrt.locking,POSIX 用 fcntl.flock;进程退出(包括崩溃)时
    由操作系统释放,不会留下"文件存在即锁住"的死锁。同进程内第二个句柄同样
    抢不到锁,因此对线程并发也安全。
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> "SyncLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        fh.seek(0)  # 锁定/解锁都必须在字节 0 上,与 release 的位置一致
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise SyncLockedError("已有同步任务在运行(跨进程锁被占用)") from exc
        self._fh = fh
        return self

    def release(self):
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass  # 解锁失败不影响释放句柄(关闭句柄即释放)
        finally:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


@contextmanager
def hold_sync_lock(lock_path: Path):
    lock = SyncLock(lock_path).acquire()
    try:
        yield lock
    finally:
        lock.release()


class RuntimeStore:
    """runtime 目录的读取、发布、恢复与清理(全部路径锁定在管理目录内)。"""

    def __init__(self, runtime_dir: Path | None = None):
        # 调用时读取 config(而非 import 时的副本),兼容测试对目录的替换
        if runtime_dir is not None:
            self.root = Path(runtime_dir)
        else:
            import config

            self.root = Path(config.DATA_RUNTIME_DIR)

    # ---- 基础路径 ----
    @property
    def active_path(self) -> Path:
        return self.root / ACTIVE_NAME

    @property
    def status_path(self) -> Path:
        return self.root / STATUS_NAME

    @property
    def lock_path(self) -> Path:
        return self.root / LOCK_NAME

    @property
    def staging_root(self) -> Path:
        return self.root / "staging"

    @property
    def generations_root(self) -> Path:
        return self.root / "generations"

    def generation_dir(self, generation_id: str) -> Path:
        if not _GEN_ID_RE.match(str(generation_id)):
            raise ValueError(f"非法 generation id: {generation_id!r}")
        return self.generations_root / generation_id

    # ---- manifest ----
    def read_manifest(self, generation_id: str) -> dict | None:
        path = self.generation_dir(generation_id) / MANIFEST_NAME
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    # ---- active 指针 ----
    def read_active(self) -> dict | None:
        try:
            data = json.loads(self.active_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("generation"), str):
            return None
        try:
            self.generation_dir(data["generation"])  # 路径逃逸检查
        except ValueError:
            return None
        return data

    def db_path_for_active(self) -> Path | None:
        active = self.read_active()
        if not active:
            return None
        db = self.generation_dir(active["generation"]) / DB_NAME
        return db if db.exists() else None

    def activate(self, generation_id: str, manifest: dict):
        """原子替换激活指针;调用方必须已持有同步锁。"""
        self.generation_dir(generation_id)  # 非法 id 直接拒绝
        atomic_write_json(self.active_path, {
            "generation": generation_id,
            "commit": manifest.get("commit"),
            "source": manifest.get("source"),
            "latest_date": manifest.get("latest_date"),
            "activated_at": utcnow_iso(),
        })

    # ---- staging 与发布 ----
    def new_staging(self) -> Path:
        path = self.staging_root / f"run-{uuid.uuid4().hex}"
        (path / "source").mkdir(parents=True, exist_ok=True)
        return path

    def discard_staging(self, staging: Path):
        if _is_within(staging, self.staging_root) and staging != self.staging_root:
            shutil.rmtree(staging, ignore_errors=True)

    def publish(self, staging: Path, generation_id: str) -> Path:
        """staging 整体移入 generations/<id>;目标已存在则视为重复发布,丢弃候选。

        Windows 上新写出的文件可能被杀软/索引器短暂占用导致目录改名被拒,
        有限重试;重试期间旧版本始终可服务。
        """
        target = self.generation_dir(generation_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            self.discard_staging(staging)
            return target
        last_exc: OSError | None = None
        for attempt in range(20):
            try:
                os.replace(staging, target)  # 同卷目录改名,原子;目标不存在时可用
                return target
            except PermissionError as exc:
                last_exc = exc
                time.sleep(0.25)
        raise last_exc  # type: ignore[misc]

    def clean_stale_staging(self, *, max_age_seconds: float = 24 * 3600):
        """删除过期 staging(进程崩溃残留)。当前正在写入的目录由调用方持有,
        且其修改时间总是新的,不会被清理。"""
        if not self.staging_root.exists():
            return
        now = time.time()
        for child in self.staging_root.iterdir():
            try:
                if child.is_dir() and now - child.stat().st_mtime > max_age_seconds:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                continue

    # ---- 状态 ----
    def read_status(self) -> dict:
        status = dict(DEFAULT_STATUS)
        try:
            data = json.loads(self.status_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                status.update({k: v for k, v in data.items() if k in DEFAULT_STATUS})
        except (OSError, ValueError):
            pass
        return status

    def write_status(self, patch: dict) -> dict:
        status = self.read_status()
        status.update({k: v for k, v in patch.items() if k in DEFAULT_STATUS})
        last_exc: OSError | None = None
        for attempt in range(10):
            try:
                atomic_write_json(self.status_path, status)
                return status
            except OSError as exc:  # Windows:目标正被读线程占用,短暂重试
                last_exc = exc
                time.sleep(0.05 * (attempt + 1))
        raise last_exc  # type: ignore[misc]

    def recover_status(self) -> dict:
        """从 active + manifest 恢复真实状态(指针发布后状态写失败/进程重启)。

        active 是唯一事实:running 复位为 False,激活信息以 manifest 为准。
        状态文件写失败不影响恢复结果的返回(下次再写)。
        """
        status = self.read_status()
        status["running"] = False
        if status["phase"] not in ("idle",):
            status["phase"] = "idle"
        active = self.read_active()
        if active:
            manifest = self.read_manifest(active["generation"]) or {}
            status["active_generation"] = active["generation"]
            status["active_commit"] = manifest.get("commit", active.get("commit"))
            status["active_source"] = manifest.get("source", active.get("source"))
            status["active_latest_date"] = manifest.get("latest_date", active.get("latest_date"))
        try:
            self.write_status(status)
        except OSError:
            pass
        return status

    # ---- 清理 ----
    def cleanup_generations(self, active_generation: str | None, *, keep: int = 2):
        """保留 active 与最近的(keep-1)个其他 generation,删除更老的。

        删除失败(Windows 上旧读句柄占用)只跳过,不影响同步。
        """
        if not self.generations_root.exists():
            return
        entries = []
        for child in self.generations_root.iterdir():
            if not child.is_dir() or not _GEN_ID_RE.match(child.name):
                continue
            manifest = self.read_manifest(child.name) or {}
            entries.append((manifest.get("built_at") or "", child.name))
        entries.sort(reverse=True)
        protected = {active_generation} if active_generation else set()
        # active 永不删除;再保留 keep-1 个最新的非 active generation
        candidates = [name for _, name in entries if name not in protected]
        doomed = candidates[keep - 1:]
        for name in doomed:
            if not _is_within(self.generation_dir(name), self.generations_root):
                continue
            shutil.rmtree(self.generation_dir(name), ignore_errors=True)


def _is_within(path: Path, parent: Path) -> bool:
    """解析后的绝对路径必须仍在管理目录内,禁止递归清理用户工作区。"""
    try:
        resolved = Path(path).resolve()
        root = Path(parent).resolve()
        return resolved == root or root in resolved.parents
    except OSError:
        return False
