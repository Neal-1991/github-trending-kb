"""每日任务编排:抓真实趋势榜 → 识别新面孔 → 补元数据/README → GLM 画像 → 飞书推送 → 落盘。

幂等设计:同一 (date, list_type) 不重复落盘、不重复推送。
用法:
  python scripts/daily_job.py            # 正常执行
  python scripts/daily_job.py --dry-run  # 只生成预览不推送、不调 GLM
"""
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import (DAILY_DIR, FEISHU_OPEN_ID, GITHUB_TOKEN, GLM_API_KEY, PROFILE_DIR, README_DIR)
from scripts.db import connect, rebuild, upsert_repo
from scripts.fetch_trending import fetch_all
from scripts.fetch_readmes import fetch_one
from scripts import feishu
from scripts import feishu_doc

MAX_NEW_PROFILES = 80


def today_bj() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")


def append_jsonl(path: Path, obj: dict):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def list_done(conn: sqlite3.Connection, date: str, list_type: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM trend_daily WHERE date=? AND list_type=? LIMIT 1",
        (date, list_type)).fetchone() is not None


def profile_new_repos(new_names: list[str], dry_run: bool, conn: sqlite3.Connection) -> dict:
    """为新仓库补 API 元数据 + README + GLM 画像。返回 one_liner 映射。"""
    one_liners = {}
    if not new_names:
        return one_liners
    from scripts import glm_client
    from scripts.enrich_github_api import fetch as api_fetch, session as gh_session

    for i, name in enumerate(new_names[:MAX_NEW_PROFILES], 1):
        meta = {"description": None, "language": None, "stars": None, "created_at": None,
                "license": None, "topics": []}
        if GITHUB_TOKEN:
            m, r = api_fetch(name)
            from scripts.enrich_github_api import wait_for_quota
            wait_for_quota(r)
            if m:
                append_jsonl(RAW_META, m)
                upsert_repo(conn, m, update_existing=True)
                meta = m
            else:
                print(f"  [{i}/{len(new_names)}] {name}: api HTTP {r.status_code}")
        readme_path = README_DIR / (name.replace("/", "__") + ".md")
        status = fetch_one(name)
        readme = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""
        if not dry_run and GLM_API_KEY:
            p = glm_client.profile_repo(name, meta, readme)
            if p:
                rec = {"full_name": name, **p, "model": glm_client.GLM_MODEL,
                       "source": "glm-api",
                       "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()}
                append_jsonl(PROFILE_DIR / "profiles.jsonl", rec)
                conn.execute("UPDATE repos SET profile_status='done' WHERE full_name=?", (name,))
                conn.commit()
                one_liners[name] = p.get("one_liner", "")
                print(f"  [{i}/{len(new_names)}] {name}: profiled ✓")
                continue
        print(f"  [{i}/{len(new_names)}] {name}: readme={status}")
    return one_liners


RAW_META = None  # 在 main 里绑定 data/raw/repo_meta_api.jsonl


def load_profiles_map(conn: sqlite3.Connection) -> dict:
    return {r["full_name"]: dict(r) for r in conn.execute(
        "SELECT full_name, one_liner, purpose, boundaries, tech_highlights, maturity FROM profiles")}


def stamp_new_faces(records: list[dict], conn: sqlite3.Connection, date: str):
    """按 first_trend_date 补 🆕 标记(从 JSONL 回放的记录没有 is_new)。"""
    first = {r["full_name"]: r["first_trend_date"] for r in conn.execute(
        "SELECT full_name, first_trend_date FROM repos WHERE first_trend_date IS NOT NULL")}
    for rec in records:
        for e in rec["entries"]:
            e["is_new"] = first.get(e["repo"]) == date


def push_daily_doc(date: str, records: list[dict], conn: sqlite3.Connection) -> str | None:
    """生成当日云文档并推送链接卡片;幂等:每日期只生成一次。"""
    doc_log = DAILY_DIR / "doc_log.jsonl"
    done = set()
    if doc_log.exists():
        for line in doc_log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                done.add(json.loads(line)["date"])
    if date in done:
        return None
    stamp_new_faces(records, conn, date)
    blocks = feishu_doc.build_daily_blocks(date, records, load_profiles_map(conn), conn)
    url = feishu_doc.generate_doc(f"GitHub 趋势日报 · {date}", blocks, FEISHU_OPEN_ID)
    append_jsonl(doc_log, {"date": date, "url": url,
                           "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()})
    n_entries = sum(len(r["entries"]) for r in records)
    card = {"msg_type": "interactive", "card": {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": f"📄 GitHub 趋势日报 · {date}"}, "template": "blue"},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
             "content": f"今日 **{n_entries}** 条榜单记录已收录,完整榜单与项目画像见云文档:\n**[📖 打开今日日报文档]({url})**"}},
            {"tag": "note", "elements": [{"tag": "plain_text",
             "content": "文档含: 今日速览 / 重点项目画像(四维) / 今日新面孔"}]},
        ]}}
    ok, msg = feishu.send(card)
    print(f"feishu doc: ok={ok} {url} {msg[:120]}")
    return url


def push_weekly_doc(date: str, summary: dict, conn: sqlite3.Connection):
    doc_log = DAILY_DIR / "doc_log.jsonl"
    profiles = load_profiles_map(conn)
    blocks = feishu_doc.build_weekly_blocks(date, summary, profiles)
    url = feishu_doc.generate_doc(f"GitHub 趋势周报 · {date}", blocks, FEISHU_OPEN_ID)
    append_jsonl(doc_log, {"date": f"week-{date}", "url": url,
                           "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()})
    card = {"msg_type": "interactive", "card": {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": f"📄 GitHub 趋势周报 · {date}"}, "template": "green"},
        "elements": [{"tag": "div", "text": {"tag": "lark_md",
                     "content": f"本周新面孔 **{summary['new_repos']}** 个、画像 **{summary['profiled']}** 篇。\n**[📖 打开本周周报文档]({url})**"}}]}}
    ok, msg = feishu.send(card)
    print(f"feishu weekly doc: ok={ok} {url} {msg[:120]}")


def main():
    global RAW_META
    RAW_META = Path(sys.argv[0]).resolve().parent.parent / "data/raw/repo_meta_api.jsonl"
    dry_run = "--dry-run" in sys.argv
    date = today_bj()
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    README_DIR.mkdir(parents=True, exist_ok=True)

    conn = rebuild()
    print(f"[{date}] daily job start (dry_run={dry_run})")

    # 1) 抓榜
    records = fetch_all()
    for rec in records:
        print(f"  {rec['list_type']}: {len(rec['entries'])} entries")

    # 2) 新面孔识别 + 落盘趋势
    new_names = []
    for rec in records:
        if not rec["entries"] or list_done(conn, date, rec["list_type"]):
            continue
        for e in rec["entries"]:
            exists = conn.execute("SELECT 1 FROM repos WHERE full_name=?", (e["repo"],)).fetchone()
            e["is_new"] = exists is None
            if e["is_new"]:
                new_names.append(e["repo"])
                upsert_repo(conn, {
                    "full_name": e["repo"], "description": e.get("description"),
                    "language": e.get("language"), "verified": 0, "source": "trending",
                })
        append_jsonl(DAILY_DIR / "trends.jsonl", {"date": date, **rec})
    new_names = sorted(set(new_names))
    print(f"new repos today: {len(new_names)}")

    # 3) 新仓库画像(API 元数据 + README + GLM)
    one_liners = profile_new_repos(new_names, dry_run, conn)

    # 4) 重建库,使推送判断与周报聚合基于最新数据
    conn = rebuild(close=conn)

    # 5) 推送日报
    pushed = {r["full_name"] for r in conn.execute(
        "SELECT full_name FROM push_log WHERE date=?", (date,))}
    if pushed:
        print(f"already pushed today, skip push")
    else:
        card = feishu.build_daily_card(date, records, one_liners)
        if dry_run:
            preview = DAILY_DIR / f"preview_{date}.md"
            preview.write_text(feishu.card_to_markdown(card), encoding="utf-8")
            print(f"dry-run: card preview -> {preview}")
        else:
            ok, msg = feishu.send(card)
            print(f"feishu daily: ok={ok} {msg[:200]}")
            if ok:
                for rec in records:
                    for e in rec["entries"]:
                        append_jsonl(DAILY_DIR / "push_log.jsonl", {
                            "date": date, "list_type": rec["list_type"],
                            "full_name": e["repo"],
                            "pushed_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                        })

    # 5.5) 云文档日报(幂等,卡片只做入口)
    if not dry_run:
        try:
            push_daily_doc(date, records, conn)
        except feishu_doc.DocScopeError as e:
            print(f"feishu doc ERROR(需在飞书开放平台为应用开通云文档权限): {e}")

    # 6) 周报(北京时间周日)
    if datetime.now(ZoneInfo("Asia/Shanghai")).weekday() == 6 and not dry_run and not pushed:
        week_start = (datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(days=6)).strftime("%Y-%m-%d")
        top_new = conn.execute("""
            SELECT t.full_name, SUM(t.stars) s FROM trend_daily t
            WHERE t.date >= ? AND t.rank <= 10 AND t.list_type = 'total'
            GROUP BY t.full_name
            HAVING (SELECT MIN(date) FROM trend_daily t2 WHERE t2.full_name = t.full_name) >= ?
            ORDER BY s DESC LIMIT 10
        """, (week_start, week_start)).fetchall()
        new_repos = conn.execute(
            "SELECT count(*) FROM repos WHERE first_trend_date >= ?", (week_start,)).fetchone()[0]
        profiled = conn.execute(
            "SELECT count(*) FROM profiles WHERE generated_at >= ?", (week_start,)).fetchone()[0]
        summary = {
            "week": f"{week_start} ~ {date}",
            "new_repos": new_repos,
            "profiled": profiled,
            "top_new": [(r["full_name"], r["s"]) for r in top_new],
        }
        ok, msg = feishu.send(feishu.build_weekly_card(date, summary))
        print(f"feishu weekly: ok={ok} {msg[:200]}")
        try:
            push_weekly_doc(date, summary, conn)
        except feishu_doc.DocScopeError as e:
            print(f"feishu weekly doc ERROR: {e}")

    # 7) 最终重建 + 汇总
    conn = rebuild(close=conn)
    total_repos = conn.execute("SELECT count(*) FROM repos").fetchone()[0]
    total_profiles = conn.execute("SELECT count(*) FROM profiles").fetchone()[0]
    print(f"DONE repos={total_repos} profiles={total_profiles}")


if __name__ == "__main__":
    main()
