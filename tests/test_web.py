"""Web 检索边界、XSS、健康检查(T18/T19/T21/T23)。"""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from scripts.db import rebuild
from tests.conftest import write_source_files


@pytest.fixture()
def client(sandbox):
    write_source_files(sandbox, repos=12)
    # 恶意样本:语言与 homepage 携带攻击载荷
    conn = rebuild()
    conn.execute("INSERT OR REPLACE INTO repos (full_name, language, homepage, verified, source)"
                 " VALUES ('evil/xss', '<script>alert(1)</script>', 'javascript:alert(1)', 1, 'api')")
    # 给恶意仓库一条 Top10 趋势记录,使其进入趋势页聚合(第 7 列 star_anomaly=0)
    conn.execute("INSERT OR REPLACE INTO trend_daily VALUES "
                 "('2022-03-01','arch:total',1,'evil/xss',500,'full',0)")
    conn.execute("INSERT OR REPLACE INTO repos (full_name, language, verified, source)"
                 " VALUES ('degraded/only', 'DegradedOnly', 1, 'api')")
    conn.execute("INSERT OR REPLACE INTO trend_daily VALUES "
                 "('2022-04-01','arch:total',1,'degraded/only',500,'degraded',0)")
    conn.commit()
    conn.close()
    import web.app as webapp
    return TestClient(webapp.app)


def test_index_ok(client):
    r = client.get("/")
    assert r.status_code == 200


def test_search_normal(client):
    r = client.get("/search", params={"q": "desc"})
    assert r.status_code == 200
    assert "条结果" in r.text


def test_search_single_char_prefix(client):
    r = client.get("/search", params={"q": "o"})  # owner 前缀
    assert r.status_code == 200


def test_search_rejects_nul_and_control(client):
    r = client.get("/search", params={"q": "abc\x00def"})
    assert r.status_code == 422
    r = client.get("/search", params={"q": "ab\x01cd"})
    assert r.status_code == 422


def test_search_rejects_overlong(client):
    r = client.get("/search", params={"q": "a" * 201})
    assert r.status_code == 422


def test_search_like_escape_no_wildcard_expansion(client):
    # % 作为字面量:任何真实仓库名都不含 %,应返回 0 条而不是全部
    r = client.get("/search", params={"q": "%%%"})
    assert r.status_code == 200
    assert "共 0 条" in r.text


def test_search_many_terms_no_500(client):
    r = client.get("/search", params={"q": " ".join(["aa", "bb", "cc", "dd"] * 50)})
    assert r.status_code in (200, 422)  # 超过 12 词被截断,仍安全
    assert r.status_code != 500


def test_search_reports_total_and_page(client):
    r = client.get("/search", params={"q": "desc", "per_page": 5})
    assert "共" in r.text and "条" in r.text


def test_browse_falls_back_to_latest_valid_date(client):
    r = client.get("/browse", params={"list_type": "total", "d": "1999-01-01"})
    assert r.status_code == 200
    assert "回退" in r.text


def test_browse_month_navigation(client):
    r = client.get("/browse", params={"list_type": "arch:total", "month": "2022-03"})
    assert r.status_code == 200


def test_browse_defaults_to_globally_latest_day(client):
    """默认视图应落在全局最新日期(真实抓取日),而不是 arch:total 的历史末日。"""
    r = client.get("/browse")
    assert r.status_code == 200
    assert "2026-09-01" in r.text
    assert "真实抓取榜" in r.text
    # 默认定位不是回退,不显示回退提示;数据缺口有明确说明
    assert "已回退" not in r.text
    assert "暂无数据" in r.text


def test_browse_explicit_arch_total_still_reaches_history(client):
    # 夹具中 arch:total 的最新日是 2022-04-01(degraded 样本日)
    r = client.get("/browse", params={"list_type": "arch:total"})
    assert r.status_code == 200
    assert "2022-04-01" in r.text
    assert "历史重建榜" in r.text


def test_browse_date_axis_spans_both_sources(client):
    """日期轴合并历史档与真实榜:选历史日期应落到历史重建榜。"""
    r = client.get("/browse", params={"d": "2022-03-01"})
    assert r.status_code == 200
    assert "2022-03-01" in r.text
    assert "arch:total" in r.text


def test_browse_month_with_explicit_type(client):
    r = client.get("/browse", params={"list_type": "arch:total", "month": "2022-03"})
    assert r.status_code == 200
    assert "2022-03-02" in r.text


def test_xss_language_escaped(client):
    r = client.get("/trends")
    assert r.status_code == 200
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text
    assert "DegradedOnly" not in r.text


def test_homepage_javascript_scheme_dropped(client):
    r = client.get("/repo/evil/xss")
    assert r.status_code == 200
    assert "javascript:" not in r.text


def test_healthz_readyz(client):
    assert client.get("/healthz").status_code == 200
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


def test_openapi_disabled(client):
    assert client.get("/openapi.json").status_code == 404


def test_security_headers_present(client):
    r = client.get("/healthz")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]


def test_generated_links_honor_root_path(client):
    import web.app as webapp
    with TestClient(webapp.app, root_path="/kb") as mounted:
        r = mounted.get("/")
    assert r.status_code == 200
    assert 'href="http://testserver/kb/static/style.css"' in r.text
    assert 'href="http://testserver/kb/search"' in r.text


def test_missing_db_fails_clearly(sandbox, monkeypatch):
    # 无 DB 文件 → connect_ro 抛 FileNotFoundError,不创建空库
    from fastapi.testclient import TestClient as TC

    import web.app as webapp
    c = TC(webapp.app, raise_server_exceptions=False)
    r = c.get("/search", params={"q": "x"})
    assert r.status_code == 503  # 缺库统一映射为不可用,不创建空库
    assert not sandbox["db"].exists()


# ---------- 任务 D:仓库详情页排名走势 ----------

def _add_rows(*trend_rows, repo_full_name=None):
    """在沙箱库中补插仓库与趋势行(测试专用);6 元组补第 7 列 star_anomaly=0。"""
    from scripts.db import connect
    conn = connect()
    if repo_full_name:
        conn.execute("INSERT OR REPLACE INTO repos (full_name, verified, source)"
                     " VALUES (?, 1, 'api')", (repo_full_name,))
    conn.executemany("INSERT OR REPLACE INTO trend_daily VALUES (?,?,?,?,?,?,?)",
                     [row + (0,) for row in trend_rows])
    conn.commit()
    conn.close()


def test_repo_detail_rank_chart_present(client):
    r = client.get("/repo/owner0/repo0")
    assert r.status_code == 200
    assert "<svg" in r.text
    assert "owner0/repo0 历史排名走势" in r.text  # 排名曲线 aria-label


def test_repo_detail_rank_chart_partial_top10_only(client):
    # partial 仅 Top10 可信:rank 9 计入,rank 33 不计入(否则会画出 #25 刻度)
    _add_rows(*[("2022-05-01", "arch:total", 9, "part/ial", 50, "partial"),
                ("2022-05-02", "arch:total", 33, "part/ial", 50, "partial")],
              repo_full_name="part/ial")
    r = client.get("/repo/part/ial")
    assert r.status_code == 200
    assert "part/ial 历史排名走势" in r.text
    assert "#25" not in r.text


def test_repo_detail_rank_chart_fallback_to_real_total(client):
    # 历史重建榜无可信记录时回退真实抓取榜(total)
    _add_rows(("2026-09-01", "total", 7, "real/only", 12, None),
              repo_full_name="real/only")
    r = client.get("/repo/real/only")
    assert r.status_code == 200
    assert "real/only 历史排名走势" in r.text
    assert "真实抓取榜" in r.text  # 回退口径说明


def test_repo_detail_degraded_only_no_chart(client):
    # 仅 degraded 记录不参与 trusted 口径,且无真实榜可回退 → 无排名曲线
    r = client.get("/repo/degraded/only")
    assert r.status_code == 200
    assert "历史排名走势" not in r.text
    assert "暂无排名数据" in r.text


def test_repo_detail_without_trend_data_still_ok(client):
    # 完全没有任何趋势记录的仓库,详情页仍 200
    _add_rows(repo_full_name="lonely/none")
    r = client.get("/repo/lonely/none")
    assert r.status_code == 200
    assert "历史排名走势" not in r.text


# ---------- 任务 E:JSON API ----------

def test_api_search_basic_and_echo(client):
    r = client.get("/api/search", params={"q": "desc"})
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 12
    assert data["page"] == 1 and data["pages"] == 1 and data["per_page"] == 30
    assert len(data["rows"]) == 12
    for key in ("full_name", "description", "language", "stars", "core_days",
                "first_trend_date", "verified", "one_liner", "score"):
        assert key in data["rows"][0]  # fts 模式含 bm25 score
    assert data["query"]["q"] == "desc"
    assert data["query"]["mode"] == "fts"


def test_api_search_pagination(client):
    data = client.get("/api/search",
                      params={"q": "desc", "per_page": 5, "page": 3}).json()
    assert data["total"] == 12
    assert data["pages"] == 3
    assert len(data["rows"]) == 2  # 末页剩余 2 条


def test_api_search_has_profile_filter(client):
    data = client.get("/api/search", params={"has_profile": "1"}).json()
    assert data["total"] == 1
    assert data["rows"][0]["full_name"] == "owner0/repo0"
    assert data["rows"][0]["one_liner"] == "项目0简介"
    assert data["query"]["has_profile"] is True


def test_api_search_control_char_422(client):
    # 控制字符 422 与 HTML 版同口径
    r = client.get("/api/search", params={"q": "ab\x01cd"})
    assert r.status_code == 422


def test_api_repo_known(client):
    r = client.get("/api/repo/owner0/repo0")
    assert r.status_code == 200
    data = r.json()
    repo = data["repo"]
    assert repo["full_name"] == "owner0/repo0"
    assert repo["one_liner"] == "项目0简介"
    for key in ("purpose", "boundaries", "tech_highlights", "maturity"):
        assert key in repo  # 画像五字段
    arch = [t for t in data["trend"] if t["list_type"] == "arch:total"]
    real = [t for t in data["trend"] if t["list_type"] == "total"]
    assert arch and real
    assert set(arch[0]) >= {"date", "rank", "stars", "list_type", "quality"}
    assert arch[0]["quality"] == "full"
    assert real[0]["quality"] is None  # 真实抓取榜 quality 为 NULL


def test_api_repo_homepage_sanitized(client):
    data = client.get("/api/repo/evil/xss").json()
    assert data["repo"]["homepage"] is None  # javascript: 被丢弃


def test_api_repo_404(client):
    r = client.get("/api/repo/no/such")
    assert r.status_code == 404
    assert "detail" in r.json()


def test_api_day_prefers_total(client):
    rows = client.get("/api/day/2026-09-01").json()
    assert len(rows) == 10
    assert set(rows[0]) == {"rank", "full_name", "stars", "quality"}
    assert rows[0]["rank"] == 1
    assert rows[0]["full_name"] == "owner0/repo0"
    assert rows[0]["stars"] == 30  # stars_today


def test_api_day_total_priority_over_arch(client):
    # 同日两类榜单都有 → 默认解析优先 total
    _add_rows(("2022-03-02", "total", 1, "evil/xss", 77, None))
    rows = client.get("/api/day/2022-03-02").json()
    assert rows[0]["full_name"] == "evil/xss"
    assert rows[0]["stars"] == 77


def test_api_day_falls_back_to_arch(client):
    rows = client.get("/api/day/2022-03-02").json()
    assert len(rows) == 12
    assert rows[0]["full_name"] == "owner0/repo0"
    assert rows[0]["quality"] == "full"


def test_api_day_both_missing_404(client):
    r = client.get("/api/day/1999-01-01")
    assert r.status_code == 404
    assert "detail" in r.json()


# ---------- 任务 F:新面孔专页 /new-faces ----------

def _add_new_face(full_name, first_date, *, lang=None, best_rank=None,
                  best_daily_stars=None, one_liner=None):
    """在沙箱库补插带 first_trend_date / best_rank 的新面孔样本(测试专用)。"""
    from scripts.db import connect
    conn = connect()
    conn.execute("INSERT OR REPLACE INTO repos (full_name, language, verified, source,"
                 " first_trend_date, best_rank, best_daily_stars)"
                 " VALUES (?, ?, 1, 'api', ?, ?, ?)",
                 (full_name, lang, first_date, best_rank, best_daily_stars))
    if one_liner is not None:
        conn.execute("INSERT OR REPLACE INTO profiles (full_name, one_liner) VALUES (?, ?)",
                     (full_name, one_liner))
    conn.commit()
    conn.close()


def _week_label(iso_day: str) -> str:
    """按被测页同口径独立复算自然周组标题,如 '2026-W36(09-01 ~ 09-07)'。"""
    d = date.fromisoformat(iso_day)
    iso = d.isocalendar()
    monday = date.fromisocalendar(iso[0], iso[1], 1)
    sunday = date.fromisocalendar(iso[0], iso[1], 7)
    return f"{iso[0]}-W{iso[1]:02d}（{monday:%m-%d} ~ {sunday:%m-%d}）"


def test_new_faces_page_ok_and_grouped(client):
    recent = (date.today() - timedelta(days=1)).isoformat()
    _add_new_face("fresh/one", recent, lang="Rust", best_rank=2,
                  best_daily_stars=321, one_liner="全新爆发项目")
    r = client.get("/new-faces")
    assert r.status_code == 200
    assert "新面孔 · 首次上榜" in r.text
    assert _week_label(recent) in r.text          # 自然周分组标题
    assert "fresh/one" in r.text
    assert "/repo/fresh/one" in r.text            # 仓库名链接指向详情页
    assert "全新爆发项目" in r.text                # 一句话画像
    assert "上榜即 Top3" in r.text                 # best_rank <= 3 徽标
    assert "本期共 1 个新面孔" in r.text           # 页脚总数
    # 导航含新面孔入口,且当前页高亮
    assert ">新面孔</a>" in r.text
    assert 'aria-current="page"' in r.text


def test_new_faces_uses_full_lang_list(client):
    # 语言下拉 = 全量 DISTINCT language(含样本里只有 1 个的冷门语言)
    _add_new_face("cold/lang", (date.today() - timedelta(days=1)).isoformat(),
                  lang="Coldlang")
    r = client.get("/new-faces")
    assert r.status_code == 200
    assert "<option value=\"Coldlang\"" in r.text


def test_new_faces_lang_filter(client):
    d = (date.today() - timedelta(days=2)).isoformat()
    _add_new_face("rust/only", d, lang="Rust", best_daily_stars=10)
    _add_new_face("zig/only", d, lang="Zig", best_daily_stars=20)
    r = client.get("/new-faces", params={"lang": "Zig"})
    assert r.status_code == 200
    assert "zig/only" in r.text
    assert "rust/only" not in r.text
    # 下拉里不存在的语言值 → 结果为空并给出友好空态
    r = client.get("/new-faces", params={"lang": "Cobol"})
    assert r.status_code == 200
    assert "rust/only" not in r.text and "zig/only" not in r.text
    assert "暂无新面孔" in r.text
    assert "本期共 0 个新面孔" in r.text


def test_new_faces_top3_badge_only_for_best_rank_le_3(client):
    d = (date.today() - timedelta(days=2)).isoformat()
    _add_new_face("top/three", d, lang="Rust", best_rank=2, best_daily_stars=500)
    _add_new_face("low/rank", d, lang="Go", best_rank=25, best_daily_stars=90)
    _add_new_face("null/rank", d, lang="Zig")  # best_rank 为 NULL 不得报错
    r = client.get("/new-faces")
    assert r.status_code == 200
    assert r.text.count("上榜即 Top3") == 1  # 仅 top/three 有徽标
    assert "low/rank" in r.text and "null/rank" in r.text


def test_new_faces_weeks_window(client):
    recent = (date.today() - timedelta(days=3)).isoformat()
    oldish = (date.today() - timedelta(days=40)).isoformat()
    _add_new_face("recent/face", recent, lang="Rust")
    _add_new_face("older/face", oldish, lang="Rust")
    r = client.get("/new-faces", params={"weeks": "4"})
    assert r.status_code == 200
    assert "recent/face" in r.text
    assert "older/face" not in r.text
    r = client.get("/new-faces", params={"weeks": "8"})
    assert r.status_code == 200
    assert "older/face" in r.text


def test_new_faces_invalid_weeks_falls_back_to_default(client):
    # 40 天前的样本不在 4 周窗口内,但在默认 8 周窗口内:非法 weeks 回退即可见
    d = (date.today() - timedelta(days=40)).isoformat()
    _add_new_face("older/face", d, lang="Rust")
    for bad in ("7", "0", "-4", "999", "abc", "1e9", "<script>"):
        r = client.get("/new-faces", params={"weeks": bad})
        assert r.status_code == 200, f"weeks={bad!r} 不应 500"
        assert "older/face" in r.text
        assert "最近 8 周" in r.text  # 回退默认 8 周


def test_new_faces_groups_desc_and_sorted_by_stars(client):
    d_new = (date.today() - timedelta(days=1)).isoformat()
    d_old = (date.today() - timedelta(days=40)).isoformat()
    _add_new_face("fresh/one", d_new, lang="Rust", best_daily_stars=50)
    _add_new_face("older/face", d_old, lang="Rust", best_daily_stars=99)
    # 同一周内按单日峰值降序
    _add_new_face("hot/later", d_new, lang="Go", best_daily_stars=100)
    _add_new_face("hot/earlier", d_new, lang="Go", best_daily_stars=900)
    r = client.get("/new-faces")
    assert r.status_code == 200
    assert _week_label(d_new) in r.text and _week_label(d_old) in r.text
    # 组倒序:新周在前
    assert r.text.index(_week_label(d_new)) < r.text.index(_week_label(d_old))
    # 组内按峰值降序
    assert r.text.index("hot/earlier") < r.text.index("hot/later")


def test_new_faces_empty_db_friendly(client):
    # 夹具里的 owner 仓库首次上榜日为 2022-03-01,不在 8 周窗口 → 空态
    r = client.get("/new-faces")
    assert r.status_code == 200
    assert "暂无新面孔" in r.text
    assert "本期共 0 个新面孔" in r.text


@pytest.mark.parametrize("series, expected", [
    ({"A": [1], "B": [1]}, [(50, 50), (0, 50)]),
    ({"A": [1, 3], "B": [3, 1]}, [(75, 25), (25, 75), (0, 75), (0, 25)]),
])
def test_stacked_bars_geometry(series, expected):
    import xml.etree.ElementTree as ET

    from web.app import stacked_bars
    quarters = [f"2026Q{i + 1}" for i in range(len(series["A"]))]
    svg = stacked_bars(quarters, series, h=100).split("</svg>")[0] + "</svg>"
    rects = ET.fromstring(svg).findall("rect")
    assert [(float(r.attrib["y"]), float(r.attrib["height"])) for r in rects] == expected


def test_readyz_missing_db_is_503(sandbox):
    import web.app as webapp
    c = TestClient(webapp.app)
    assert c.get("/healthz").status_code == 200
    assert c.get("/readyz").status_code == 503
    assert not sandbox["db"].exists()


def test_readyz_unreadable_db_is_503(sandbox, monkeypatch):
    import sqlite3

    import web.app as webapp

    def fail():
        raise sqlite3.OperationalError("unable to open database")

    monkeypatch.setattr(webapp, "connect_ro", fail)
    assert TestClient(webapp.app).get("/readyz").status_code == 503


def test_detail_and_home_gap_uses_database_span(client):
    for path in ("/", "/repo/owner0/repo0"):
        page = client.get(path).text
        assert "2022-04-02 ~ 2026-08-31" in page
        assert "2026-02 ~ 2026-08" not in page
    _add_rows(("2026-08-31", "arch:total", 1, "owner0/repo0", 10, "full"))
    for path in ("/", "/repo/owner0/repo0"):
        assert "暂无数据" not in client.get(path).text


def test_home_freshness_and_empty_capture_instruction(client):
    page = client.get("/").text
    assert "最新采集日期 2026-09-01" in page
    assert "画像覆盖率" in page
    from scripts.db import connect
    conn = connect()
    conn.execute("DELETE FROM trend_daily WHERE list_type='total'")
    conn.commit()
    conn.close()
    page = client.get("/").text
    assert "--capture-only" in page
    assert "--dry-run" not in page


def test_detail_warns_only_for_creation_after_first_trend(client):
    from scripts.db import connect
    conn = connect()
    conn.execute("UPDATE repos SET created_at='2026-09-01T00:00:00Z' WHERE full_name='owner0/repo0'")
    conn.commit()
    assert "身份信息待核验" in client.get("/repo/owner0/repo0").text
    conn.execute("UPDATE repos SET created_at='2022-03-01T12:00:00Z' WHERE full_name='owner0/repo0'")
    conn.commit()
    conn.close()
    assert "身份信息待核验" not in client.get("/repo/owner0/repo0").text
