"""Web 检索边界、XSS、健康检查(T18/T19/T21/T23)。"""

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
    # 给恶意仓库一条 Top10 趋势记录,使其进入趋势页聚合
    conn.execute("INSERT OR REPLACE INTO trend_daily VALUES "
                 "('2022-03-01','arch:total',1,'evil/xss',500,'full')")
    conn.execute("INSERT OR REPLACE INTO repos (full_name, language, verified, source)"
                 " VALUES ('degraded/only', 'DegradedOnly', 1, 'api')")
    conn.execute("INSERT OR REPLACE INTO trend_daily VALUES "
                 "('2022-04-01','arch:total',1,'degraded/only',500,'degraded')")
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
    assert r.status_code == 500  # 未静默创建空库并 200;明确失败
    assert not sandbox["db"].exists()
