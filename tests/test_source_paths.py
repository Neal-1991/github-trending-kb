"""SourcePaths 显式建库路径测试:两套 source 不串数据、旧默认行为不变。"""
import json

from conftest import write_source_files

from scripts.db import rebuild
from scripts.source_paths import SourcePaths


def _write_snapshot(daily_dir, day, names):
    from scripts.snapshot_store import build_snapshot

    snap = build_snapshot(day, [{"list_type": "total", "entries": [
        {"rank": i + 1, "repo": name, "stars_today": 7} for i, name in enumerate(names)]}])
    root = daily_dir / "snapshots" / day[:4] / day[5:7]
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{day}.json").write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")


def test_default_rebuild_uses_config_paths(sandbox):
    """sources=None 保持旧行为:读取 config(经 sandbox 替换)的默认布局。"""
    write_source_files(sandbox, repos=2, trend_days=1, profiles=1)
    conn = rebuild()
    names = {r["full_name"] for r in conn.execute("SELECT full_name FROM repos")}
    conn.close()
    assert names == {"owner0/repo0", "owner1/repo1"}


def test_explicit_sources_are_isolated(tmp_path):
    """从两套临时 source 分别建库,互不串数据。"""
    trees = {}
    for tag in ("a", "b"):
        root = tmp_path / tag
        dirs = {"raw": root / "raw", "daily": root / "daily",
                "profiles": root / "profiles", "readmes": root / "readmes"}
        for d in dirs.values():
            d.mkdir(parents=True)
        (dirs["raw"] / "repo_meta_snapshot.csv").write_text(
            "full_name,owner_type,description,fork,created_at,pushed_at,homepage,"
            "stargazers_count,forks_count,subscribers_count,language,archived,"
            "open_issues_count,license_key,topics,default_branch\n"
            f"only/{tag},User,desc,false,2022-01-01T00:00:00Z,2022-06-01T00:00:00Z,,"
            "10,1,0,Python,false,0,MIT,[],main\n", encoding="utf-8")
        _write_snapshot(dirs["daily"], "2026-09-01", [f"only/{tag}"])
        trees[tag] = dirs

    for tag, dirs in trees.items():
        db = tmp_path / f"{tag}.db"
        sources = SourcePaths(raw_dir=dirs["raw"], daily_dir=dirs["daily"],
                              profiles_dir=dirs["profiles"], readme_dir=dirs["readmes"])
        conn = rebuild(db_path=db, sources=sources)
        names = {r["full_name"] for r in conn.execute("SELECT full_name FROM repos")}
        conn.close()
        assert names == {f"only/{tag}"}


def test_missing_readmes_only_from_explicit_source(sandbox, tmp_path):
    """_missing.txt 只来自显式传入的 readme_dir;不借用工作区残留。"""
    write_source_files(sandbox, repos=2, trend_days=0, profiles=0, real_days=0)
    (sandbox["readmes"] / "_missing.txt").write_text("owner1/repo1\n", encoding="utf-8")

    # 同步语义:候选 readme_dir 为空目录 → 不借用工作区 _missing.txt
    empty_readmes = tmp_path / "empty-readmes"
    empty_readmes.mkdir()
    sources = SourcePaths(raw_dir=sandbox["raw"], daily_dir=sandbox["daily"],
                          profiles_dir=sandbox["profiles"], readme_dir=empty_readmes)
    conn = rebuild(db_path=tmp_path / "sync.db", sources=sources)
    statuses = {r["full_name"]: r["profile_status"] for r in
                conn.execute("SELECT full_name, profile_status FROM repos")}
    conn.close()
    assert statuses["owner1/repo1"] == "pending"

    # 工作区默认路径语义:_missing.txt 生效(历史行为不变)
    conn = rebuild(db_path=tmp_path / "workspace.db")
    statuses = {r["full_name"]: r["profile_status"] for r in
                conn.execute("SELECT full_name, profile_status FROM repos")}
    conn.close()
    assert statuses["owner1/repo1"] == "no_readme"
