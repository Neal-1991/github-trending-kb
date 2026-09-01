"""抓取 github.com/trending 真实榜单(总榜 + 语言分榜)。

无官方 API,解析 HTML;所有榜单作为一个批次提交,任一校验失败则整批失败
(FetchValidationError),不产生 canonical 快照。校验项:
  HTTP 200、条数在配置区间、rank 从 1 连续、owner/repo 格式合法且榜内唯一、
  stars_today 覆盖率达到阈值。失败 HTML 存入 data/diagnostics/(已 gitignore)。
返回结构: {"list_type": "...", "entries": [{"rank","repo","description","language","stars_total","stars_today"}]}
"""
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import (
    DAILY_DIR,
    LANG_LISTS,
    STARS_TODAY_COVERAGE,
    TRENDING_MAX_ENTRIES,
    TRENDING_MIN_ENTRIES,
)

DIAG_DIR = DAILY_DIR / "diagnostics"
REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
})


class FetchValidationError(RuntimeError):
    """批次校验失败:不产生 canonical 快照,正式数据不变。"""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems[:6]) + (f" (共 {len(problems)} 项)" if len(problems) > 6 else ""))


def _num(text: str) -> int:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else 0


def parse_trending(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    entries = []
    for art in soup.select("article.Box-row"):
        a = art.select_one("h2 a")
        if not a or not a.get("href"):
            continue
        repo = a["href"].strip("/").removesuffix("/stargazers")
        desc_el = art.select_one("p")
        lang_el = art.select_one('[itemprop="programmingLanguage"]')
        stars_total = forks = 0
        for link in art.select('a.Link--muted'):
            href = link.get("href", "")
            count = _num(link.get_text())
            if "/stargazers" in href:
                stars_total = count
            elif "/forks" in href:
                forks = count
        today_el = art.select_one("span.d-inline-block.float-sm-right")
        entries.append({
            "rank": len(entries) + 1,
            "repo": repo,
            "description": desc_el.get_text(strip=True) if desc_el else None,
            "language": lang_el.get_text(strip=True) if lang_el else None,
            "stars_total": stars_total,
            "stars_today": _num(today_el.get_text()) if today_el else 0,
            "forks": forks,
        })
    return entries


def validate_entries(list_type: str, entries: list[dict]) -> list[str]:
    """返回问题列表;为空表示该榜通过。"""
    problems = []
    n = len(entries)
    if not (TRENDING_MIN_ENTRIES <= n <= TRENDING_MAX_ENTRIES):
        problems.append(f"[{list_type}] 条数 {n} 超出合理区间 [{TRENDING_MIN_ENTRIES}, {TRENDING_MAX_ENTRIES}]")
    for i, e in enumerate(entries, 1):
        if e["rank"] != i:
            problems.append(f"[{list_type}] rank 不连续: 第 {i} 条 rank={e['rank']}")
            break
    seen = set()
    for e in entries:
        if not REPO_RE.match(e["repo"]):
            problems.append(f"[{list_type}] 仓库名非法: {e['repo']!r}")
        if e["repo"] in seen:
            problems.append(f"[{list_type}] 仓库名重复: {e['repo']}")
        seen.add(e["repo"])
    covered = sum(1 for e in entries if (e.get("stars_today") or 0) > 0)
    coverage = covered / n if n else 0.0
    if coverage < STARS_TODAY_COVERAGE:
        problems.append(f"[{list_type}] stars_today 覆盖率 {coverage:.0%} < {STARS_TODAY_COVERAGE:.0%}"
                        f"(选择器可能失效)")
    return problems


def _save_diag(list_type: str, html: str, reason: str):
    try:
        DIAG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%dT%H%M%S")
        safe = list_type.replace(":", "_")
        (DIAG_DIR / f"{ts}_{safe}.html").write_text(
            f"<!-- reason: {reason} -->\n{html[:500000]}", encoding="utf-8")
        print(f"  [diag] 已保存失败页面: {DIAG_DIR}/{ts}_{safe}.html ({reason})")
    except OSError as e:
        print(f"  [diag] 保存诊断页面失败: {e}")


def fetch_list(list_type: str, retries: int = 3) -> dict:
    url = "https://github.com/trending"
    if list_type.startswith("lang:"):
        url += "/" + list_type.split(":", 1)[1]
    url += "?since=daily"
    last_html = ""
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 200 and "github.com/trending" in r.url:
                entries = parse_trending(r.text)
                problems = validate_entries(list_type, entries)
                if not problems:
                    return {"list_type": list_type, "entries": entries}
                print(f"  [{list_type}] 校验未通过: {problems[0]}, attempt {attempt}")
                last_html = r.text
                reason = "; ".join(problems[:2])
            else:
                print(f"  [{list_type}] HTTP {r.status_code}, attempt {attempt}")
                last_html, reason = r.text, f"HTTP {r.status_code}"
        except requests.RequestException as e:
            print(f"  [{list_type}] network error: {e}, attempt {attempt}")
            reason = f"network: {e}"
        if attempt == retries:
            _save_diag(list_type, last_html, reason)
        time.sleep(5 * attempt)
    return {"list_type": list_type, "entries": []}


def fetch_all() -> list[dict]:
    """抓取全部榜单并作为一个批次校验;任一榜单失败 → FetchValidationError。"""
    results = [fetch_list("total")] + [fetch_list(f"lang:{lang}") for lang in LANG_LISTS]
    problems = []
    for rec in results:
        if not rec["entries"]:
            problems.append(f"[{rec['list_type']}] 抓取失败/解析为空")
        else:
            problems.extend(validate_entries(rec["list_type"], rec["entries"]))
    if problems:
        raise FetchValidationError(problems)
    return results


if __name__ == "__main__":
    import json
    for rec in fetch_all():
        print(json.dumps(rec, ensure_ascii=False, indent=1)[:800])
