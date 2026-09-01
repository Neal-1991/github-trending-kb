"""画像管线与数据质量(T15/T16/T17/T24)。"""
import json

import pytest

from scripts import glm_client
from scripts.db import rebuild
from tests.conftest import write_source_files


def test_glm_parse_valid_json():
    text = '{"one_liner":"一个项目","purpose":"用途","boundaries":"边界","tech_highlights":"技术","maturity":"成熟"}'
    out = glm_client._parse_json(text)
    assert out and out["one_liner"] == "一个项目"


def test_glm_parse_wrapped_json():
    text = '好的,以下是结果:\n{"one_liner":"x","purpose":"y","boundaries":"z","tech_highlights":"w","maturity":"v"}\n完毕'
    assert glm_client._parse_json(text)["purpose"] == "y"


def test_glm_invalid_output_rejected():
    assert glm_client._parse_json("not json") is None
    missing = '{"one_liner":"x"}'
    assert glm_client._parse_json(missing) is None          # 缺字段 → None(触发重试)
    wrong_type = '{"one_liner":1,"purpose":"y","boundaries":"z","tech_highlights":"w","maturity":"v"}'
    assert glm_client._parse_json(wrong_type) is None       # 类型错误 → None


def test_glm_overlong_field_truncated():
    text = json.dumps({"one_liner": "长" * 500, "purpose": "y", "boundaries": "z",
                       "tech_highlights": "w", "maturity": "v"}, ensure_ascii=False)
    out = glm_client._parse_json(text)
    assert len(out["one_liner"]) == 120


def test_prompt_wraps_readme_as_untrusted():
    import inspect
    src = inspect.getsource(glm_client.profile_repo)
    assert "不可信" in src and "README 节选结束" in src


def test_profile_batch_writes_profiles_table(sandbox, monkeypatch, capsys):
    write_source_files(sandbox, repos=4, profiles=0)
    # 给 2 个仓库放 README
    for i in (0, 1):
        (sandbox["readmes"] / f"owner{i}__repo{i}.md").write_text("# readme", encoding="utf-8")
    conn = rebuild()
    conn.close()
    import scripts.profile_batch as pb
    monkeypatch.setattr(pb, "README_DIR", sandbox["readmes"])
    monkeypatch.setattr(pb, "PROFILE_DIR", sandbox["profiles"])
    monkeypatch.setattr(pb, "GLM_API_KEY", "k")
    monkeypatch.setattr(pb.glm_client, "GLM_API_KEY", "k")

    calls = []
    def fake_profile(name, meta, readme):
        calls.append(name)
        return {"one_liner": "简介", "purpose": "用途", "boundaries": "边界",
                "tech_highlights": "技术", "maturity": "成熟"}
    monkeypatch.setattr(pb.glm_client, "profile_repo", fake_profile)

    import sys as _sys
    _sys.argv = ["profile_batch.py", "--limit", "2", "--min-core-days", "0"]
    pb.main()
    conn = rebuild()
    assert conn.execute("SELECT count(*) FROM profiles").fetchone()[0] == 2
    assert conn.execute(
        "SELECT count(*) FROM profiles WHERE input_hash IS NOT NULL"
    ).fetchone()[0] == 2
    # 重跑:profiles 表已有 → 不再调用 GLM(T17)
    calls.clear()
    pb.main()
    assert calls == []
    conn.close()


def test_profile_batch_does_not_starve_on_no_readme(sandbox, monkeypatch, capsys):
    """候选窗口前几个全无 README 时,后面的有效候选仍被选中(T24)。"""
    write_source_files(sandbox, repos=8, profiles=0)
    # 只有 core_days 最低的 repo7 有 README(排序后位于最末)
    (sandbox["readmes"] / "owner7__repo7.md").write_text("# r", encoding="utf-8")
    conn = rebuild()
    conn.close()
    import scripts.profile_batch as pb
    monkeypatch.setattr(pb, "README_DIR", sandbox["readmes"])
    monkeypatch.setattr(pb, "PROFILE_DIR", sandbox["profiles"])
    monkeypatch.setattr(pb, "GLM_API_KEY", "k")
    monkeypatch.setattr(pb.glm_client, "GLM_API_KEY", "k")
    monkeypatch.setattr(pb.glm_client, "profile_repo",
                        lambda *a, **k: {"one_liner": "x", "purpose": "y", "boundaries": "z",
                                         "tech_highlights": "w", "maturity": "v"})
    import sys as _sys
    _sys.argv = ["profile_batch.py", "--limit", "1", "--min-core-days", "0"]
    pb.main()
    out = capsys.readouterr().out
    assert "owner7/repo7 ✓" in out


def test_trusted_metrics_exclude_partial_gt10(sandbox):
    """partial 且 rank>10 不进入 best_rank/trend_days 的 trusted 口径(T15)。"""
    write_source_files(sandbox, repos=3)
    conn = rebuild()
    # partial 月份的 rank 30 观测
    conn.execute("INSERT OR REPLACE INTO trend_daily VALUES ('2025-12-01','arch:total',30,'owner0/repo0',5,'partial')")
    conn.execute("INSERT OR REPLACE INTO trend_daily VALUES ('2025-12-02','arch:total',3,'owner0/repo0',50,'partial')")
    conn.commit()
    from scripts.db import refresh_repo_stats
    refresh_repo_stats(conn)
    row = conn.execute("SELECT best_rank, trend_days FROM repos WHERE full_name='owner0/repo0'").fetchone()
    assert row["best_rank"] == 1          # partial rank30 未生效;rank1 来自 full 天
    conn.close()


def test_core_days_excludes_degraded_observations(sandbox):
    write_source_files(sandbox, repos=3, trend_days=1)
    conn = rebuild()
    before = conn.execute(
        "SELECT core_days FROM repos WHERE full_name='owner0/repo0'"
    ).fetchone()["core_days"]
    conn.execute(
        "INSERT OR REPLACE INTO trend_daily VALUES "
        "('2025-01-01','arch:total',1,'owner0/repo0',500,'degraded')")
    conn.commit()
    from scripts.db import refresh_repo_stats
    refresh_repo_stats(conn)
    after = conn.execute(
        "SELECT core_days FROM repos WHERE full_name='owner0/repo0'"
    ).fetchone()["core_days"]
    assert after == before
    conn.close()


def test_anomaly_excluded_from_best_daily_stars(sandbox):
    write_source_files(sandbox, repos=3)
    conn = rebuild()
    conn.execute("INSERT OR REPLACE INTO trend_daily VALUES ('2024-09-29','arch:total',1,'owner0/repo0',27891,'full')")
    conn.commit()
    assert conn.execute(
        "SELECT best_daily_stars FROM repos WHERE full_name='owner0/repo0'"
    ).fetchone()["best_daily_stars"] == 100  # 原始 full 天峰值(100),而非 27891
    conn.close()


def test_extract_history_fail_closed(sandbox, monkeypatch, capsys):
    """历史抓取连续异常 → 正式 CSV 不变,无 UnboundLocalError(T06)。"""
    import scripts.extract_history as eh
    monkeypatch.setattr(eh, "RAW_DIR", sandbox["raw"])
    old = sandbox["raw"] / "trends_gharchive.csv"
    old.write_text("date,repo,stars,quality\n2022-03-01,keep/old,1,full\n", encoding="utf-8")
    monkeypatch.setattr(eh, "run_query", lambda sql: " garbage not csv")
    monkeypatch.setattr(eh.time, "sleep", lambda s: None)
    with pytest.raises(eh.QueryError):
        eh.main()
    assert "keep/old" in old.read_text(encoding="utf-8")  # 旧文件未被动过
