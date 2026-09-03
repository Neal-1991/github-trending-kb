"""数据质量升级:API 元数据确定性合并(任务 A)+ 刷星 flag 与人工覆盖(任务 B)。

覆盖:
- repo_meta_api.jsonl 按 fetched_at 确定性合并(fetched_at 缺失视为最旧、同分取文件靠后、
  乱序输入同结果、重建端到端);
- star_anomaly 阈值判定(仅 arch:total)与导入写入;
- star_anomaly_overrides.txt 的 include/exclude 双向生效、只影响 arch:total、
  坏指令 fail closed(重建失败且旧库保留);
- Web"现象级爆发"查询尊重 flag;audit 新计数。
"""
import json
from itertools import permutations

import pytest
from fastapi.testclient import TestClient

from config import ARCH_DAILY_STAR_ANOMALY
from scripts.db import (
    apply_star_anomaly_overrides,
    latest_api_meta_by_full_name,
    parse_star_anomaly_overrides,
    rebuild,
    star_anomaly_flag,
)
from tests.conftest import write_source_files


def _write_arch(sandbox, rows: list[str]):
    """覆盖沙箱的 trends_gharchive.csv(表头 + 给定数据行)。"""
    lines = ["date,repo,stars,quality", *rows]
    (sandbox["raw"] / "trends_gharchive.csv").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")


def _write_api_meta(sandbox, records: list[dict]):
    with (sandbox["raw"] / "repo_meta_api.jsonl").open("w", encoding="utf-8") as f:
        for m in records:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")


def _write_overrides(sandbox, text: str):
    (sandbox["raw"] / "star_anomaly_overrides.txt").write_text(text, encoding="utf-8")


def _trend_flags(conn, list_type="arch:total") -> dict:
    return {r["full_name"]: r["star_anomaly"] for r in conn.execute(
        "SELECT full_name, star_anomaly FROM trend_daily WHERE list_type=?", (list_type,))}


# ---------- 任务 A:API 元数据确定性合并 ----------

def test_api_meta_merge_keeps_latest_and_rules():
    """fetched_at 最大者胜;缺失视为最旧;同分按文件顺序靠后。"""
    recs = [
        {"full_name": "a/one", "stars": 1, "fetched_at": "2026-08-30T00:00:00+00:00"},
        {"full_name": "a/one", "stars": 3, "fetched_at": "2026-09-01T00:00:00+00:00"},
        {"full_name": "a/one", "stars": 2, "fetched_at": "2026-08-31T00:00:00+00:00"},
        {"full_name": "b/two", "stars": 10},  # 缺 fetched_at → 最旧
        {"full_name": "b/two", "stars": 11, "fetched_at": "2026-01-01T00:00:00+00:00"},
        {"full_name": "c/tri", "stars": 20, "fetched_at": "2026-05-05T00:00:00+00:00"},
        {"full_name": "c/tri", "stars": 21, "fetched_at": "2026-05-05T00:00:00+00:00"},  # 同分 → 靠后
    ]
    out = {m["full_name"]: m["stars"] for m in latest_api_meta_by_full_name(recs)}
    assert out == {"a/one": 3, "b/two": 11, "c/tri": 21}
    assert len(latest_api_meta_by_full_name(recs)) == 3


def test_api_meta_merge_order_independent():
    """乱序输入同结果(fetched_at 互异时与文件行序完全解耦)。"""
    recs = [
        {"full_name": "a/one", "stars": 1, "fetched_at": "2026-08-30T00:00:00+00:00"},
        {"full_name": "a/one", "stars": 3, "fetched_at": "2026-09-01T00:00:00+00:00"},
        {"full_name": "b/two", "stars": 10},
        {"full_name": "b/two", "stars": 11, "fetched_at": "2026-01-01T00:00:00+00:00"},
        {"full_name": "c/tri", "stars": 20, "fetched_at": "2026-05-05T00:00:00+00:00"},
        {"full_name": "c/tri", "stars": 21, "fetched_at": "2026-05-06T00:00:00+00:00"},
    ]
    base = {m["full_name"]: m["stars"] for m in latest_api_meta_by_full_name(recs)}
    assert base == {"a/one": 3, "b/two": 11, "c/tri": 21}
    for perm in permutations(range(len(recs))):
        shuffled = [recs[i] for i in perm]
        got = {m["full_name"]: m["stars"] for m in latest_api_meta_by_full_name(shuffled)}
        assert got == base


def test_api_meta_merge_missing_fetched_at_and_tie():
    recs = [
        {"full_name": "a/one", "stars": 99, "fetched_at": "2020-01-01T00:00:00+00:00"},
        {"full_name": "a/one", "stars": 1},  # 缺 fetched_at,虽有更新意图仍视为最旧
    ]
    assert latest_api_meta_by_full_name(recs)[0]["stars"] == 99
    # 均缺失(或同分):文件顺序靠后者胜(与历史"后者覆盖"一致)
    tie = [{"full_name": "b/two", "stars": 1}, {"full_name": "b/two", "stars": 2}]
    assert latest_api_meta_by_full_name(tie)[0]["stars"] == 2
    assert latest_api_meta_by_full_name(list(reversed(tie)))[0]["stars"] == 1


def test_rebuild_merges_api_meta_by_fetched_at(sandbox):
    """端到端:JSONL 行序颠倒,重建结果仍取 fetched_at 最新的观测,且覆盖快照。"""
    write_source_files(sandbox, repos=2, trend_days=1, profiles=0, real_days=0)
    recs = [
        {"full_name": "owner0/repo0", "description": "old", "stars": 111,
         "fetched_at": "2026-08-30T00:00:00+00:00"},
        {"full_name": "owner0/repo0", "description": "new", "stars": 222,
         "fetched_at": "2026-09-01T00:00:00+00:00"},
    ]
    _write_api_meta(sandbox, list(reversed(recs)))
    conn = rebuild()
    row = conn.execute("SELECT stars, description, verified, source FROM repos"
                       " WHERE full_name='owner0/repo0'").fetchone()
    assert (row["stars"], row["description"], row["verified"], row["source"]) == \
        (222, "new", 1, "api")
    conn.close()


# ---------- 任务 B:阈值判定与导入写入 ----------

def test_star_anomaly_flag_threshold():
    assert star_anomaly_flag("arch:total", ARCH_DAILY_STAR_ANOMALY) == 1
    assert star_anomaly_flag("arch:total", ARCH_DAILY_STAR_ANOMALY - 1) == 0
    assert star_anomaly_flag("arch:total", None) == 0
    assert star_anomaly_flag("total", ARCH_DAILY_STAR_ANOMALY * 10) == 0      # 只对 arch:total
    assert star_anomaly_flag("lang:python", ARCH_DAILY_STAR_ANOMALY * 10) == 0


def test_arch_import_sets_flag_and_trusted_stats(sandbox):
    """arch CSV 导入即按阈值写 flag;flag=1 不参与 best_daily_stars。"""
    write_source_files(sandbox, repos=3, trend_days=0, profiles=0, real_days=0)
    _write_arch(sandbox, [
        f"2022-03-01,owner0/repo0,{ARCH_DAILY_STAR_ANOMALY - 1},full",
        f"2022-03-01,owner1/repo1,{ARCH_DAILY_STAR_ANOMALY},full",
        "2022-03-01,owner2/repo2,100,full",
    ])
    conn = rebuild()
    assert _trend_flags(conn) == {
        "owner0/repo0": 0, "owner1/repo1": 1, "owner2/repo2": 0}
    best = {r["full_name"]: r["best_daily_stars"] for r in conn.execute(
        "SELECT full_name, best_daily_stars FROM repos")}
    assert best["owner0/repo0"] == ARCH_DAILY_STAR_ANOMALY - 1
    assert best["owner1/repo1"] is None   # 疑似刷星不作为可信峰值
    assert best["owner2/repo2"] == 100
    conn.close()


def test_real_and_snapshot_imports_write_zero_flag(sandbox):
    """快照/真实榜路径无 arch:total,star_anomaly 显式写 0(非 NULL)。"""
    write_source_files(sandbox, repos=3, trend_days=1, profiles=0, real_days=1)
    conn = rebuild()
    n_rows = conn.execute("SELECT count(*) FROM trend_daily").fetchone()[0]
    assert n_rows > 0
    assert conn.execute(
        "SELECT count(*) FROM trend_daily WHERE star_anomaly != 0").fetchone()[0] == 0
    conn.close()


# ---------- 任务 B:人工覆盖文件 ----------

def test_parse_overrides_valid_with_comments_and_blanks():
    text = "\n".join([
        "# 注释行",
        "",
        "include 2022-03-01 owner0/repo0",
        "exclude 2022-03-02 owner1/repo1   ",
    ])
    assert parse_star_anomaly_overrides(text) == {
        ("2022-03-01", "owner0/repo0"): 0,
        ("2022-03-02", "owner1/repo1"): 1,
    }


def test_parse_overrides_last_wins_for_same_key():
    text = "include 2022-03-01 a/b\nexclude 2022-03-01 a/b\n"
    assert parse_star_anomaly_overrides(text)[("2022-03-01", "a/b")] == 1


@pytest.mark.parametrize("bad", [
    "banish 2022-03-01 owner0/repo0",        # 未知指令
    "include 2022-03-01",                     # 缺 full_name
    "include 2022-03-01 owner0/repo0 extra",  # 多余 token
    "include 2022-3-1 owner0/repo0",          # 日期非零填充
    "include 2022-02-30 owner0/repo0",        # 日期不存在
    "include",                                 # token 不足
])
def test_parse_overrides_fail_closed(bad):
    with pytest.raises(ValueError):
        parse_star_anomaly_overrides(bad)


def test_overrides_apply_both_directions_and_only_arch(sandbox):
    """include 纠正真实爆发 / exclude 标记疑似刷星;只作用于 arch:total 行。"""
    write_source_files(sandbox, repos=3, trend_days=0, profiles=0, real_days=0)
    _write_arch(sandbox, [
        f"2022-03-01,owner0/repo0,{ARCH_DAILY_STAR_ANOMALY + 100},full",
        "2022-03-01,owner1/repo1,300,full",
        "2022-03-01,owner2/repo2,100,full",
    ])
    _write_overrides(sandbox, "# 真实爆发\ninclude 2022-03-01 owner0/repo0\n"
                              "exclude 2022-03-01 owner1/repo1\n")
    conn = rebuild()
    assert _trend_flags(conn) == {
        "owner0/repo0": 0, "owner1/repo1": 1, "owner2/repo2": 0}
    # include 的真实爆发进入 best_daily_stars(阈值判定被人工纠正)
    assert conn.execute("SELECT best_daily_stars FROM repos"
                        " WHERE full_name='owner0/repo0'").fetchone()[0] \
        == ARCH_DAILY_STAR_ANOMALY + 100

    # 只影响 arch:total:同键 total 行不被 exclude 波及
    conn.execute(
        "INSERT OR REPLACE INTO trend_daily"
        " (date, list_type, rank, full_name, stars, quality, star_anomaly)"
        " VALUES ('2022-03-01','total',1,'owner1/repo1',300,NULL,0)")
    conn.commit()
    applied = apply_star_anomaly_overrides(conn)
    assert conn.execute("SELECT star_anomaly FROM trend_daily"
                        " WHERE list_type='total'").fetchone()[0] == 0
    assert applied == 2   # 仅 arch:total 的两行命中
    conn.close()


def test_bad_overrides_fail_rebuild_and_keep_old_db(sandbox):
    """覆盖文件不可解析 → rebuild 抛错(fail closed),原子语义保证旧库不变。"""
    write_source_files(sandbox, repos=3, trend_days=1, profiles=0)
    rebuild().close()
    before = sandbox["db"].read_bytes()
    _write_overrides(sandbox, "banish 2022-03-01 owner0/repo0\n")
    with pytest.raises(ValueError, match="star_anomaly_overrides"):
        rebuild()
    assert sandbox["db"].read_bytes() == before
    assert not list(sandbox["db"].parent.glob("*.tmp"))


# ---------- 任务 B:Web 爆发查询尊重 flag ----------

@pytest.fixture()
def anomaly_client(sandbox):
    """owner0=阈值命中但被 include 纠正;owner1=人工 exclude;owner2=正常。"""
    write_source_files(sandbox, repos=3, trend_days=0, profiles=0, real_days=0)
    _write_arch(sandbox, [
        f"2022-03-01,owner0/repo0,{ARCH_DAILY_STAR_ANOMALY + 100},full",
        "2022-03-01,owner1/repo1,300,full",
        "2022-03-01,owner2/repo2,100,full",
    ])
    _write_overrides(sandbox, "include 2022-03-01 owner0/repo0\n"
                              "exclude 2022-03-01 owner1/repo1\n")
    rebuild().close()
    import web.app as webapp
    return TestClient(webapp.app)


def test_web_spikes_respect_star_anomaly_flag(anomaly_client):
    r = anomaly_client.get("/trends")
    assert r.status_code == 200
    assert "owner0/repo0" in r.text     # include:真实爆发照常展示
    assert "owner2/repo2" in r.text
    assert "owner1/repo1" not in r.text  # exclude:疑似刷星不展示
    # 展示按单日星标降序:15100(include) 在 100 之前
    assert r.text.index("owner0/repo0") < r.text.index("owner2/repo2")


# ---------- audit 新计数 ----------

def test_audit_api_meta_merge_counts(sandbox, monkeypatch):
    import scripts.audit_data as audit_data
    monkeypatch.setattr(audit_data, "RAW_DIR", sandbox["raw"])
    _write_api_meta(sandbox, [
        {"full_name": "a/one", "stars": 1, "fetched_at": "2026-08-30T00:00:00+00:00"},
        {"full_name": "a/one", "stars": 2, "fetched_at": "2026-09-01T00:00:00+00:00"},
        {"full_name": "b/two", "stars": 10},
    ])
    assert audit_data.audit_api_meta_merge() == {
        "rows_before": 3, "rows_after": 2, "merged_away": 1}
    (sandbox["raw"] / "repo_meta_api.jsonl").unlink()
    assert audit_data.audit_api_meta_merge() == {}


def test_audit_overrides_counts_and_fail_closed(sandbox, monkeypatch):
    import scripts.audit_data as audit_data
    monkeypatch.setattr(audit_data, "RAW_DIR", sandbox["raw"])
    _write_overrides(sandbox, "# c\ninclude 2022-03-01 a/b\nexclude 2022-03-02 x/y\n")
    errors: list = []
    assert audit_data.audit_star_anomaly_overrides(errors) == {
        "lines": 2, "includes": 1, "excludes": 1}
    assert errors == []
    _write_overrides(sandbox, "foo bar\n")
    errors = []
    assert audit_data.audit_star_anomaly_overrides(errors) == {}
    assert errors and "star_anomaly_overrides" in errors[0]
    (sandbox["raw"] / "star_anomaly_overrides.txt").unlink()
    assert audit_data.audit_star_anomaly_overrides([]) == {}


def test_audit_reports_flagged_rows(sandbox, monkeypatch):
    """库内被 flag 的行数进入 audit 报告;旧 schema(缺列)给出待重建提示。"""
    import scripts.audit_data as audit_data
    write_source_files(sandbox, repos=3, trend_days=0, profiles=0, real_days=0)
    _write_arch(sandbox, [f"2022-03-01,owner1/repo1,{ARCH_DAILY_STAR_ANOMALY},full"])
    rebuild().close()
    monkeypatch.setattr(audit_data, "DB_PATH", sandbox["db"])
    errors: list = []
    findings: list = []
    out = audit_data.audit_db(errors, findings)
    assert out["star_anomaly_rows"] == 1
    assert any("star_anomaly=1" in f for f in findings)
    assert errors == []

    # 旧 schema 派生库:缺 star_anomaly 列 → 提示待重建,不崩溃
    from scripts.db import connect
    conn = connect()
    conn.execute("ALTER TABLE trend_daily DROP COLUMN star_anomaly")
    conn.commit()
    conn.close()
    errors, findings = [], []
    out = audit_data.audit_db(errors, findings)
    assert "star_anomaly_rows" not in out
    assert any("缺少 star_anomaly 列" in f for f in findings)


def test_audit_snapshot_csv_finding_updated(sandbox, monkeypatch):
    """文案:CSV first-wins 仅为内部历史行为,API 数据已按 fetched_at 确定性合并。"""
    import scripts.audit_data as audit_data
    monkeypatch.setattr(audit_data, "RAW_DIR", sandbox["raw"])
    (sandbox["raw"] / "repo_meta_snapshot.csv").write_text(
        "full_name,owner_type,description,fork,created_at,pushed_at,homepage,"
        "stargazers_count,forks_count,subscribers_count,language,archived,"
        "open_issues_count,license_key,topics,default_branch\n"
        "a/b,User,desc1,false,2022-01-01T00:00:00Z,2022-06-01T00:00:00Z,,1,1,1,"
        "Python,false,0,MIT,,main\n"
        "a/b,User,desc2,false,2022-01-01T00:00:00Z,2022-06-01T00:00:00Z,,2,1,1,"
        "Python,false,0,MIT,,main\n", encoding="utf-8")
    errors: list = []
    findings: list = []
    out = audit_data.audit_snapshot_csv(errors, findings)
    assert out["dups"] == 1 and out["conflicts"] == 1
    assert not errors
    assert "first-wins" in findings[0] and "fetched_at" in findings[0]
