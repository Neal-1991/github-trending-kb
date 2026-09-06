"""不可变的建库 source 路径对象。

scripts/db.py、snapshot_store 历史上直接使用 config 的模块全局路径;后台同步
线程若改写全局变量会影响 Web 请求与测试,因此派生库的构建接口支持显式传入
SourcePaths。`default()` 在调用时读取 config 当前值,保持旧默认布局不变。
"""
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@dataclass(frozen=True)
class SourcePaths:
    """一次建库所需的全部 source 目录(其余派生文件一律不读取)。"""

    raw_dir: Path
    daily_dir: Path
    profiles_dir: Path
    readme_dir: Path

    def __post_init__(self):
        for name in ("raw_dir", "daily_dir", "profiles_dir", "readme_dir"):
            object.__setattr__(self, name, Path(getattr(self, name)))

    # ---- 派生路径(全部基于以上四个根,不允许外部注入) ----
    @property
    def snapshot_root(self) -> Path:
        """canonical 快照根目录(data/daily/snapshots)。"""
        return self.daily_dir / "snapshots"

    @property
    def trends_jsonl(self) -> Path:
        return self.daily_dir / "trends.jsonl"

    @property
    def push_log(self) -> Path:
        return self.daily_dir / "push_log.jsonl"

    @property
    def push_archive_dir(self) -> Path:
        return self.daily_dir / "archive"

    @property
    def overrides_path(self) -> Path:
        """人工刷星纠正规则(可选文件)。"""
        return self.raw_dir / "star_anomaly_overrides.txt"

    @property
    def missing_readmes(self) -> Path:
        """README 永久缺失清单(可选;同步版本通常没有)。"""
        return self.readme_dir / "_missing.txt"

    @property
    def meta_snapshot_csv(self) -> Path:
        return self.raw_dir / "repo_meta_snapshot.csv"

    @property
    def meta_api_jsonl(self) -> Path:
        return self.raw_dir / "repo_meta_api.jsonl"

    @property
    def trends_gharchive_csv(self) -> Path:
        return self.raw_dir / "trends_gharchive.csv"

    @property
    def profiles_jsonl(self) -> Path:
        return self.profiles_dir / "profiles.jsonl"

    def has_any_source(self) -> bool:
        """rebuild 的 had_sources 判定:任一主 source 存在即视为有数据。"""
        from scripts.snapshot_store import iter_snapshots

        return any(p.exists() for p in (
            self.meta_snapshot_csv, self.meta_api_jsonl, self.trends_gharchive_csv,
            self.trends_jsonl, self.profiles_jsonl)) or any(iter_snapshots(self.snapshot_root))


def default() -> SourcePaths:
    """从 config 模块当前值构造(调用时读取,兼容测试对路径的 monkeypatch)。"""
    import config

    return SourcePaths(
        raw_dir=config.RAW_DIR,
        daily_dir=config.DAILY_DIR,
        profiles_dir=config.PROFILE_DIR,
        readme_dir=config.README_DIR,
    )
