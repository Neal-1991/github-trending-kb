"""后台同步协调器:启动后立即检查一次,之后按周期检查;手动触发共用同一入口。

约束:
- 网络/下载/SQLite 建库都在工作线程执行,绝不阻塞异步事件循环;
- 同步运行中再触发 → 合并到现有任务,不另起一轮;
- 服务关闭时停止安排新任务,通过取消点安全中止(退出中的任务不得发布新指针);
- 页面状态轮询只读本地状态(内存 + sync_status.json),绝不请求 GitHub;
- config 的同步开关在调用时读取,测试可整体关闭。
"""
import sys
import threading
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.runtime_store import RuntimeStore


class SyncCoordinator:
    """单线程循环协调器:run_sync 的生命周期与状态展示都收敛在这里。"""

    def __init__(self, *, store: RuntimeStore | None = None, client_factory=None,
                 interval_seconds: float | None = None):
        import config

        self.store = store or RuntimeStore()
        self.client_factory = client_factory  # None → run_sync 内部按 config 构造
        self.interval = interval_seconds or config.DATA_SYNC_INTERVAL_SECONDS
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.Lock()

    # ---- 生命周期 ----
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="data-sync", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0):
        """停止调度并请求中止当前任务;有限等待工作线程退出。"""
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    # ---- 触发 ----
    def trigger(self) -> dict:
        """手动触发一次检查。运行中返回 merged=True(合并到现有任务)。"""
        import config

        if not config.DATA_SYNC_ENABLED:
            return {"triggered": False, "reason": "disabled"}
        with self._lock:
            running = self._running
        if running:
            return {"triggered": False, "reason": "running"}
        if not (self._thread and self._thread.is_alive()):
            self.start()
        else:
            self._wake.set()
        return {"triggered": True, "reason": "scheduled"}

    # ---- 状态(只读本地,不访问网络) ----
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def status(self) -> dict:
        import config

        status = self.store.recover_status() if not self.store.status_path.exists() \
            else self.store.read_status()
        status["enabled"] = bool(config.DATA_SYNC_ENABLED)
        status["running"] = self.is_running() or bool(status.get("running"))
        if status["running"]:
            status["last_result"] = status.get("last_result") or "never_synced"
        if not config.DATA_SYNC_ENABLED:
            status["phase"] = "idle"
            status["running"] = False
            status["message"] = "自动同步已关闭"
            if status.get("last_result") not in ("failed",):
                status["last_result"] = "disabled"
        return status

    # ---- 内部 ----
    def _loop(self):
        import config

        while not self._stop.is_set():
            if config.DATA_SYNC_ENABLED:
                self._run_once()
            else:
                # 关闭状态下不执行任何网络动作,安静等待退出或配置变化
                pass
            # 到点或被手动触发唤醒;立即再检查 stop
            self._wake.wait(timeout=max(5.0, self.interval))
            self._wake.clear()

    def _run_once(self):
        from scripts.sync_data import run_sync

        with self._lock:
            self._running = True
        try:
            kwargs = {}
            if self.client_factory is not None:
                kwargs["client"] = self.client_factory()
            run_sync(self.store, cancel_check=self._stop.is_set, **kwargs)
        except Exception:
            # run_sync 已把失败写入状态;这里兜底保证线程不退出
            pass
        finally:
            with self._lock:
                self._running = False


def status_payload(coordinator: SyncCoordinator | None) -> dict:
    """给 API 用的状态负载:无协调器(测试/未启动)时也返回可读状态。"""
    import config

    if coordinator is not None:
        return coordinator.status()
    store = RuntimeStore()
    status = store.recover_status() if not store.status_path.exists() else store.read_status()
    status["enabled"] = bool(config.DATA_SYNC_ENABLED)
    if not config.DATA_SYNC_ENABLED:
        status["message"] = "自动同步已关闭"
        if status.get("last_result") not in ("failed",):
            status["last_result"] = "disabled"
    return status
