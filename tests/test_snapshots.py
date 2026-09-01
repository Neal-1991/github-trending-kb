"""快照存储与抓取校验(T01/T02/T04/T05)。"""
import json

import pytest

from scripts import snapshot_store as snap
from scripts.fetch_trending import FetchValidationError, parse_trending, validate_entries
from tests.conftest import make_trending_html


def _records(n=12, stars=True):
    return [{"list_type": "total",
             "entries": parse_trending(make_trending_html(n, stars_today=stars))}]


def test_snapshot_id_content_addressed():
    a = snap.build_snapshot("2026-09-01", _records())
    b = snap.build_snapshot("2026-09-01", _records())
    assert a["snapshot_id"] == b["snapshot_id"]  # captured_at 不同但内容相同


def test_save_and_load_roundtrip(sandbox):
    snapshot = snap.build_snapshot("2026-09-01", _records())
    path = snap.save_snapshot(snapshot)
    assert path.exists()
    loaded = snap.load_snapshot("2026-09-01")
    assert loaded["snapshot_id"] == snapshot["snapshot_id"]
    # 幂等:相同内容再存不报错
    assert snap.save_snapshot(snap.build_snapshot("2026-09-01", _records())) == path


def test_save_existing_different_content_requires_overwrite(sandbox):
    snap.save_snapshot(snap.build_snapshot("2026-09-01", _records(12)))
    with pytest.raises(snap.SnapshotExistsError):
        snap.save_snapshot(snap.build_snapshot("2026-09-01", _records(13)))
    snap.save_snapshot(snap.build_snapshot("2026-09-01", _records(13)), overwrite=True)
    history = list(sandbox["daily"].rglob("history/**/*.json"))
    assert len(history) == 1  # 旧版本归档保留


def test_validation_rejects_bad_batch():
    problems = validate_entries("total", [])
    assert problems  # 空榜单必须产生问题项
    entries = parse_trending(make_trending_html(5))  # 少于下限 10
    assert any("条数" in p for p in validate_entries("total", entries))


def test_validation_rejects_discontinuous_rank_and_dup():
    entries = parse_trending(make_trending_html(12))
    entries[5]["rank"] = 99
    assert any("rank" in p for p in validate_entries("total", entries))
    entries2 = parse_trending(make_trending_html(12))
    entries2[7]["repo"] = entries2[6]["repo"]
    assert any("重复" in p for p in validate_entries("total", entries2))


def test_validation_rejects_zero_star_coverage():
    entries = parse_trending(make_trending_html(12, stars_today=False))
    assert any("覆盖率" in p for p in validate_entries("total", entries))


def test_parse_trending_basic():
    entries = parse_trending(make_trending_html(3, repos=["a/b", "c/d", "e/f"]))
    assert [e["repo"] for e in entries] == ["a/b", "c/d", "e/f"]
    assert entries[0]["rank"] == 1
    assert entries[0]["stars_today"] > 0
    assert entries[0]["stars_total"] == 1000
