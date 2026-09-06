"""仓库身份/元数据冲突标注:flags 判定纯函数与 rebuild 摄取(全离线)。"""
import json

import pytest

from scripts.atomic_io import atomic_write_json
from scripts.db import parse_identity_flags, rebuild
from scripts.identity_flags import (
    build_identity_flags,
    detect_created_after_trend,
    detect_meta_conflicts,
    generate,
)
from tests.conftest import write_source_files


def _snap_row(name, **over):
    row = {"full_name": name, "description": "d", "language": "Python",
           "stargazers_count": "100", "created_at": "2020-01-01 00:00:00",
           "license_key": "MIT", "fork": "false", "archived": "false", "homepage": ""}
    row.update(over)
    return row


# ---------- (a) meta_conflict 纯函数判定 ----------

def test_meta_conflict_from_two_conflicting_snapshot_rows():
    flags = detect_meta_conflicts(
        [_snap_row("a/b"), _snap_row("a/b", stargazers_count="200", fork="true")], [])
    assert set(flags) == {"a/b"}
    entry = flags["a/b"]
    assert entry["flags"] == ["meta_conflict"]
    evidence = entry["evidence"]["meta_conflict"]
    assert set(evidence) == {"stargazers_count", "fork"}
    assert [o["value"] for o in evidence["stargazers_count"]] == ["100", "200"]
    assert all(o["source"] == "snapshot" and "fetched_at" not in o
               for o in evidence["stargazers_count"])
    assert "stargazers_count" in entry["note"] and "fork" in entry["note"]


def test_no_conflict_for_identical_duplicate_or_single_row():
    assert detect_meta_conflicts([_snap_row("a/b"), _snap_row("a/b")], []) == {}
    assert detect_meta_conflicts([_snap_row("a/b")], []) == {}


def test_api_evidence_tags_fetched_at_and_api_internal_conflict():
    api = [
        {"full_name": "a/b", "description": "v1", "stars": 150,
         "fetched_at": "2026-08-30T00:00:00+00:00"},
        {"full_name": "a/b", "description": "v2", "stars": 180,
         "fetched_at": "2026-09-06T00:00:00+00:00"},
    ]
    flags = detect_meta_conflicts([_snap_row("a/b")], api)
    evidence = flags["a/b"]["evidence"]["meta_conflict"]
    # api 历史观测之间 description 不一致 → 触发;snapshot 单行 vs api 的
    # stargazers_count 漂移属预期覆盖行为,不进入冲突字段
    assert set(evidence) == {"description"}
    # evidence 收录该字段全部来源的观测取值(snapshot 在前,api 按 fetched_at 升序)
    assert [(o["source"], o["value"], o.get("fetched_at")) for o in evidence["description"]] == [
        ("snapshot", "d", None),
        ("api", "v1", "2026-08-30T00:00:00+00:00"),
        ("api", "v2", "2026-09-06T00:00:00+00:00"),
    ]
    assert "API 历史观测" in flags["a/b"]["note"]


def test_cross_source_drift_alone_does_not_trigger():
    api = [{"full_name": "a/b", "description": "d", "stars": 999,
            "fetched_at": "2026-09-06T00:00:00+00:00"}]
    assert detect_meta_conflicts([_snap_row("a/b")], api) == {}


# ---------- (b) created_after_trend 判定 ----------

def test_created_after_trend_detection():
    rows = [
        {"full_name": "ok/one", "created_at": "2020-01-01T00:00:00Z", "first_trend_date": "2021-01-01"},
        {"full_name": "bad/two", "created_at": "2022-06-12T09:06:26Z", "first_trend_date": "2022-06-01"},
        {"full_name": "same/three", "created_at": "2022-06-01T00:00:00Z", "first_trend_date": "2022-06-01"},
        {"full_name": "skip/no-created", "created_at": None, "first_trend_date": "2022-06-01"},
        {"full_name": "skip/no-trend", "created_at": "2022-06-12T09:06:26Z", "first_trend_date": None},
    ]
    flags = detect_created_after_trend(rows)
    assert set(flags) == {"bad/two"}
    note = flags["bad/two"]["note"]
    # 与详情页 identity_risk 提示同口径
    assert "2022-06-12" in note and "2022-06-01" in note and "同名的其他仓库" in note
    evidence = flags["bad/two"]["evidence"]["created_after_trend"]
    assert evidence["created_at"].startswith("2022-06-12")
    assert evidence["first_trend_date"] == "2022-06-01"


def test_build_identity_flags_merges_both_flag_types():
    snapshot = [_snap_row("x/y"), _snap_row("x/y", stargazers_count="101")]
    rows = [{"full_name": "x/y", "created_at": "2022-06-12T00:00:00Z",
             "first_trend_date": "2022-06-01"}]
    flags = build_identity_flags(snapshot, [], rows, generated_at="2026-09-06T00:00:00+00:00")
    assert flags["generated_at"] == "2026-09-06T00:00:00+00:00"
    entry = flags["repos"]["x/y"]
    assert entry["flags"] == ["meta_conflict", "created_after_trend"]
    assert set(entry["evidence"]) == {"meta_conflict", "created_after_trend"}
    assert "stargazers_count" in entry["note"] and "同名的其他仓库" in entry["note"]


# ---------- parse_identity_flags:fail closed 语义 ----------

def test_parse_identity_flags_ok_and_errors():
    ok = {"generated_at": "t", "repos": {"a/b": {"flags": ["meta_conflict"], "note": "n"}}}
    assert parse_identity_flags(json.dumps(ok)) == {"a/b": "n"}
    assert parse_identity_flags(json.dumps({"repos": {}})) == {}
    with pytest.raises(ValueError, match="identity_flags.json"):
        parse_identity_flags("{broken")
    with pytest.raises(ValueError, match="repos"):
        parse_identity_flags(json.dumps({"generated_at": "t"}))
    with pytest.raises(ValueError, match="note"):
        parse_identity_flags(json.dumps({"repos": {"a/b": {"note": 123}}}))


# ---------- (c) rebuild 摄取(sandbox,离线) ----------

def _write_flags(dirs, obj):
    (dirs["raw"] / "identity_flags.json").write_text(
        json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def test_rebuild_ingests_identity_notes(sandbox):
    write_source_files(sandbox)
    _write_flags(sandbox, {"generated_at": "2026-09-06T00:00:00+00:00", "repos": {
        "owner0/repo0": {"flags": ["meta_conflict"],
                         "note": "元数据多行冲突:stargazers_count 取值不一致", "evidence": {}},
        "ghost/missing": {"flags": ["meta_conflict"], "note": "库中不存在", "evidence": {}},
    }})
    conn = rebuild()
    notes = {r["full_name"]: r["identity_note"]
             for r in conn.execute("SELECT full_name, identity_note FROM repos")}
    assert notes["owner0/repo0"] == "元数据多行冲突:stargazers_count 取值不一致"
    assert notes["owner1/repo1"] == ""
    assert "ghost/missing" not in notes
    conn.close()


def test_rebuild_without_flags_file_identity_notes_empty(sandbox):
    write_source_files(sandbox)
    conn = rebuild()
    total = conn.execute("SELECT count(*) FROM repos").fetchone()[0]
    annotated = conn.execute(
        "SELECT count(*) FROM repos WHERE identity_note IS NULL OR identity_note != ''"
    ).fetchone()[0]
    assert total > 0 and annotated == 0
    conn.close()


def test_rebuild_bad_flags_json_fails_closed(sandbox):
    write_source_files(sandbox)
    (sandbox["raw"] / "identity_flags.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="identity_flags.json"):
        rebuild()


def test_generate_end_to_end_then_reingest(sandbox):
    """snapshot 双行冲突 → generate 产出 meta_conflict → 原子落盘 → rebuild 摄取。"""
    write_source_files(sandbox)
    conn = rebuild()
    conn.close()
    with (sandbox["raw"] / "repo_meta_snapshot.csv").open("a", encoding="utf-8") as f:
        f.write("owner0/repo0,User,desc 0,false,2022-01-01T00:00:00Z,"
                "2022-06-01T00:00:00Z,,999,10,5,Python,false,2,MIT,agent,main\n")

    flags = generate(sandbox["raw"] / "repo_meta_snapshot.csv",
                     sandbox["raw"] / "repo_meta_api.jsonl", sandbox["db"])
    assert set(flags["repos"]) == {"owner0/repo0"}
    evidence = flags["repos"]["owner0/repo0"]["evidence"]["meta_conflict"]
    assert [o["value"] for o in evidence["stargazers_count"]] == ["100", "999"]

    out = sandbox["raw"] / "identity_flags.json"
    atomic_write_json(out, flags, ensure_ascii=False)
    conn = rebuild()
    note = conn.execute(
        "SELECT identity_note FROM repos WHERE full_name='owner0/repo0'").fetchone()[0]
    assert "stargazers_count" in note
    conn.close()


def test_main_cli_default_out_and_json_mode(sandbox, monkeypatch, capsys):
    import scripts.identity_flags as idf

    write_source_files(sandbox)
    conn = rebuild()
    conn.close()
    out = sandbox["raw"] / "identity_flags.json"
    monkeypatch.setattr(idf, "_default_paths", lambda: (
        sandbox["raw"] / "repo_meta_snapshot.csv", sandbox["raw"] / "repo_meta_api.jsonl",
        sandbox["db"], out))

    assert idf.main([]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "generated_at" in data and isinstance(data["repos"], dict)
    assert "标注 0 个仓库" in capsys.readouterr().out

    out.unlink()
    assert idf.main(["--json"]) == 0
    assert "generated_at" in capsys.readouterr().out
    assert not out.exists()
