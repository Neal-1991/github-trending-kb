"""抓取 github.com/trending 真实榜单(总榜 + 语言分榜)。

无官方 API,解析 HTML;选择器做了防御式处理,结构变动时告警而非崩溃。
返回结构: {"list_type": "...", "entries": [{"rank","repo","description","language","stars_total","stars_today"}]}
"""
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import LANG_LISTS

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
})


def _num(text: str) -> int:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else 0


def parse_trending(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    entries = []
    for i, art in enumerate(soup.select("article.Box-row"), 1):
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


def fetch_list(list_type: str, retries: int = 3) -> dict:
    url = "https://github.com/trending"
    if list_type.startswith("lang:"):
        url += "/" + list_type.split(":", 1)[1]
    url += "?since=daily"
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 200:
                entries = parse_trending(r.text)
                if entries:
                    return {"list_type": list_type, "entries": entries}
                print(f"  [{list_type}] parsed 0 entries, attempt {attempt}")
            else:
                print(f"  [{list_type}] HTTP {r.status_code}, attempt {attempt}")
        except requests.RequestException as e:
            print(f"  [{list_type}] network error: {e}, attempt {attempt}")
        time.sleep(5 * attempt)
    return {"list_type": list_type, "entries": []}


def fetch_all() -> list[dict]:
    return [fetch_list("total")] + [fetch_list(f"lang:{lang}") for lang in LANG_LISTS]


if __name__ == "__main__":
    import json
    for rec in fetch_all():
        print(json.dumps(rec, ensure_ascii=False, indent=1)[:800])
