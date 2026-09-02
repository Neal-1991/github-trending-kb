"""fetch_trending HTML 解析与批次校验回归测试(T24:GitHub 改版的第一道防线)。

只测解析与校验逻辑,不访问网络:HTML 用本地构造器生成,requests 会话用假对象替换。
"""

import re
from types import SimpleNamespace

import pytest
import requests

from config import STARS_TODAY_COVERAGE, TRENDING_MAX_ENTRIES, TRENDING_MIN_ENTRIES
from scripts import fetch_trending as ft

# ---------- HTML 构造器(贴合 parse_trending 的选择器) ----------

def _article(repo, *, desc="一个示例项目", lang="Python",
             stars_total=1000, forks=10, stars_today=5) -> str:
    parts = ['<article class="Box-row">']
    parts.append(f'<h2><a href="/{repo}">{repo}</a></h2>')
    if desc is not None:
        parts.append(f"<p>{desc}</p>")
    if lang is not None:
        parts.append(f'<span itemprop="programmingLanguage">{lang}</span>')
    parts.append(f'<a class="Link--muted" href="/{repo}/stargazers">{stars_total:,}</a>')
    parts.append(f'<a class="Link--muted" href="/{repo}/forks">{forks}</a>')
    if stars_today is not None:
        parts.append(f'<span class="d-inline-block float-sm-right">{stars_today:,} stars today</span>')
    parts.append("</article>")
    return "".join(parts)


def make_html(articles: list[str]) -> str:
    return "<html><body>" + "".join(articles) + "</body></html>"


def valid_articles(n: int, *, stars_today=5) -> list[str]:
    return [_article(f"owner{i}/repo{i}", stars_today=stars_today) for i in range(n)]


def make_entries(n: int, *, stars_today=5) -> list[dict]:
    return [{"rank": i + 1, "repo": f"owner{i}/repo{i}", "description": "d",
             "language": "Python", "stars_total": 100, "stars_today": stars_today,
             "forks": 1} for i in range(n)]


# ---------- parse_trending ----------

def test_parse_trending_extracts_all_fields():
    html = make_html(valid_articles(3))
    entries = ft.parse_trending(html)
    assert [e["rank"] for e in entries] == [1, 2, 3]
    first = entries[0]
    assert first["repo"] == "owner0/repo0"
    assert first["description"] == "一个示例项目"
    assert first["language"] == "Python"
    assert first["stars_total"] == 1000
    assert first["stars_today"] == 5
    assert first["forks"] == 10


def test_parse_trending_href_variants():
    html = make_html([
        _article("a/b").replace('href="/a/b"', 'href="/a/b/stargazers"'),
        _article("c/d").replace('href="/c/d"', 'href="c/d"'),
    ])
    entries = ft.parse_trending(html)
    assert [e["repo"] for e in entries] == ["a/b", "c/d"]


def test_parse_trending_missing_optional_fields():
    html = make_html([_article("owner/repo", desc=None, lang=None,
                               stars_today=None)])
    entries = ft.parse_trending(html)
    assert len(entries) == 1
    assert entries[0]["description"] is None
    assert entries[0]["language"] is None
    assert entries[0]["stars_today"] == 0


def test_parse_trending_skips_articles_without_repo_link():
    broken = '<article class="Box-row"><h2>没有链接</h2></article>'
    html = make_html([broken] + valid_articles(2))
    entries = ft.parse_trending(html)
    assert [e["rank"] for e in entries] == [1, 2]  # 跳过坏条目,rank 仍连续
    assert all(e["repo"] for e in entries)


def test_parse_trending_empty_html():
    assert ft.parse_trending("<html><body></body></html>") == []


def test_num_strips_non_digits():
    assert ft._num("1,234 stars today") == 1234
    assert ft._num("stars today") == 0
    assert ft._num("") == 0


# ---------- validate_entries ----------

def test_validate_valid_list_passes():
    assert ft.validate_entries("total", make_entries(25)) == []


def test_validate_min_boundary():
    assert ft.validate_entries("total", make_entries(TRENDING_MIN_ENTRIES)) == []
    problems = ft.validate_entries("total", make_entries(TRENDING_MIN_ENTRIES - 1))
    assert any("条数" in p for p in problems)


def test_validate_max_boundary():
    assert ft.validate_entries("total", make_entries(TRENDING_MAX_ENTRIES)) == []
    problems = ft.validate_entries("total", make_entries(TRENDING_MAX_ENTRIES + 1))
    assert any("条数" in p for p in problems)


def test_validate_rank_discontinuity():
    entries = make_entries(3)
    entries[1]["rank"] = 3  # 1,3,5 跳号
    entries[2]["rank"] = 5
    problems = ft.validate_entries("total", entries)
    assert any("rank 不连续" in p for p in problems)


def test_validate_invalid_repo_names():
    entries = make_entries(2)
    entries[0]["repo"] = "no-slash"
    entries[1]["repo"] = "a/b/c"
    problems = ft.validate_entries("total", entries)
    assert sum(1 for p in problems if "仓库名非法" in p) == 2


def test_validate_duplicate_repo():
    entries = make_entries(3)
    entries[1]["repo"] = entries[0]["repo"]
    problems = ft.validate_entries("total", entries)
    assert any("仓库名重复" in p for p in problems)


def test_validate_coverage_below_threshold():
    # 一半条目 stars_today=0:50% < 60%,应判定选择器可能失效
    half = TRENDING_MIN_ENTRIES // 2
    entries = make_entries(TRENDING_MIN_ENTRIES)
    for i, e in enumerate(entries):
        if i < half:
            e["stars_today"] = 0
    problems = ft.validate_entries("total", entries)
    assert any("覆盖率" in p for p in problems)


def test_validate_coverage_at_threshold_passes():
    entries = make_entries(10)
    for i, e in enumerate(entries):
        if i < 4:  # 6/10 = 60%,恰好达到阈值
            e["stars_today"] = 0
    assert ft.validate_entries("total", entries) == []


def test_validate_coverage_threshold_value():
    assert STARS_TODAY_COVERAGE == 0.6  # 测试边界假设依赖该配置值


# ---------- fetch_list(HTTP + 重试 + 诊断) ----------

class FakeResp:
    def __init__(self, status_code=200, url="https://github.com/trending?since=daily",
                 text=""):
        self.status_code, self.url, self.text = status_code, url, text


class FakeSession:
    def __init__(self, resp):
        self.resp, self.calls, self.urls = resp, 0, []

    def get(self, url, timeout=30):
        self.calls += 1
        self.urls.append(url)
        return self.resp


@pytest.fixture()
def no_net(monkeypatch, tmp_path):
    """替换会话、屏蔽重试休眠、诊断文件写入 tmp_path。"""
    monkeypatch.setattr(ft, "DIAG_DIR", tmp_path / "diag")
    monkeypatch.setattr(ft, "time", SimpleNamespace(sleep=lambda s: None))
    return tmp_path


def test_fetch_list_success_parses_entries(no_net, monkeypatch):
    sess = FakeSession(FakeResp(text=make_html(valid_articles(12))))
    monkeypatch.setattr(ft, "session", sess)
    rec = ft.fetch_list("total")
    assert rec["list_type"] == "total"
    assert len(rec["entries"]) == 12
    assert sess.calls == 1


def test_fetch_list_url_per_list_type(no_net, monkeypatch):
    sess = FakeSession(FakeResp(text=make_html(valid_articles(12))))
    monkeypatch.setattr(ft, "session", sess)
    ft.fetch_list("lang:python")
    assert sess.urls[0] == "https://github.com/trending/python?since=daily"


def test_fetch_list_http_error_retries_then_empty(no_net, monkeypatch):
    sess = FakeSession(FakeResp(status_code=503, text="oops"))
    monkeypatch.setattr(ft, "session", sess)
    rec = ft.fetch_list("total")
    assert rec["entries"] == []
    assert sess.calls == 3  # 重试 3 次后放弃
    diag = list((no_net / "diag").glob("*.html"))
    assert len(diag) == 1  # 最终失败落诊断文件


def test_fetch_list_validation_failure_retries_then_empty(no_net, monkeypatch):
    # 25 条但 70% 无 stars_today:解析正常、覆盖率校验失败
    articles = valid_articles(25, stars_today=0)
    for i in range(8):
        articles[i] = _article(f"owner{i}/repo{i}", stars_today=5)
    sess = FakeSession(FakeResp(text=make_html(articles)))
    monkeypatch.setattr(ft, "session", sess)
    rec = ft.fetch_list("total")
    assert rec["entries"] == []
    assert sess.calls == 3


def test_fetch_list_redirect_guard(no_net, monkeypatch):
    # 200 但最终 URL 不再是 trending(如跳登录/验证页):视为失败
    sess = FakeSession(FakeResp(url="https://github.com/login", text="<html></html>"))
    monkeypatch.setattr(ft, "session", sess)
    rec = ft.fetch_list("total")
    assert rec["entries"] == []
    assert sess.calls == 3


def test_fetch_list_network_exception(no_net, monkeypatch):
    class ExplodingSession:
        def __init__(self):
            self.calls = 0

        def get(self, url, timeout=30):
            self.calls += 1
            raise requests.ConnectionError("boom")

    sess = ExplodingSession()
    monkeypatch.setattr(ft, "session", sess)
    rec = ft.fetch_list("total")
    assert rec["entries"] == []
    assert sess.calls == 3


# ---------- fetch_all(批次校验:任一榜单失败整批失败) ----------

def _fake_fetch_list(mapping: dict):
    def fake(list_type: str, retries: int = 3):
        return {"list_type": list_type, "entries": mapping.get(list_type, [])}
    return fake


def test_fetch_all_all_valid(monkeypatch):
    entries = make_entries(12)
    monkeypatch.setattr(ft, "fetch_list", _fake_fetch_list(
        {"total": entries, "lang:python": entries, "lang:typescript": entries,
         "lang:javascript": entries, "lang:rust": entries}))
    results = ft.fetch_all()
    assert [r["list_type"] for r in results] == [
        "total", "lang:python", "lang:typescript", "lang:javascript", "lang:rust"]


def test_fetch_all_one_empty_fails_whole_batch(monkeypatch):
    entries = make_entries(12)
    monkeypatch.setattr(ft, "fetch_list", _fake_fetch_list(
        {"total": entries, "lang:python": [], "lang:typescript": entries,
         "lang:javascript": entries, "lang:rust": entries}))
    with pytest.raises(ft.FetchValidationError) as exc:
        ft.fetch_all()
    assert "lang:python" in str(exc.value)


def test_fetch_all_invalid_entries_fail_batch(monkeypatch):
    bad = make_entries(5)  # 低于条数下限
    entries = make_entries(12)
    monkeypatch.setattr(ft, "fetch_list", _fake_fetch_list(
        {"total": entries, "lang:python": bad, "lang:typescript": entries,
         "lang:javascript": entries, "lang:rust": entries}))
    with pytest.raises(ft.FetchValidationError) as exc:
        ft.fetch_all()
    assert "lang:python" in str(exc.value)
    assert "条数" in str(exc.value)


def test_fetch_validation_error_truncates_long_problems():
    problems = [f"问题{i}" for i in range(10)]
    err = ft.FetchValidationError(problems)
    assert len(err.problems) == 10
    assert re.search(r"共 10 项", str(err))
