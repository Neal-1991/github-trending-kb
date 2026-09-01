"""原子文件写入:同目录临时文件 → 完整写出 → os.replace。

失败时旧文件保持不变(fail closed)。所有 source of truth 文件
(历史 CSV、元数据 CSV、canonical 快照)与 SQLite 重建都必须走这里。
"""
import json
import os
import tempfile
import time
from pathlib import Path


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, obj, *, ensure_ascii: bool = False, indent: int | None = None) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=ensure_ascii, indent=indent))


def atomic_append_jsonl(path: Path, obj: dict) -> None:
    """JSONL 追加不是原子替换,但保证目录存在且单行完整写出。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def replace_file_with_retry(src: Path, dst: Path, *, attempts: int = 60, delay: float = 0.5) -> None:
    """os.replace,容忍目标被占用(Windows 上有进程持着旧 DB 句柄)。

    重试期间旧文件始终可服务;超过次数才抛错,旧文件依旧不变。
    """
    src, dst = Path(src), Path(dst)
    for attempt in range(1, attempts + 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts:
                raise
            time.sleep(delay)
