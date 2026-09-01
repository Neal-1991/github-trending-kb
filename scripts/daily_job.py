"""每日任务编排(状态机化):捕获 → 画像 → 通知,三阶段解耦。

模式:
  python scripts/daily_job.py                     # 完整:捕获(或回放)→ 画像 → 通知
  python scripts/daily_job.py --dry-run           # 零副作用:不写 source/快照、不调 GLM、不发消息
  python scripts/daily_job.py --capture-only      # 只捕获+画像+重建,不通知(CI Job A)
  python scripts/daily_job.py --notify-only       # 只回放 canonical 快照并通知(CI Job B)
  python scripts/daily_job.py --refresh-snapshot  # 显式抓新版本替换 canonical(旧版自动归档)
  python scripts/daily_job.py --date 2026-08-30   # 指定日期(回放该日快照)

幂等设计:
- 当日 canonical 快照存在时默认回放,不重新抓取;刷新必须显式 --refresh-snapshot;
- 每种通知(日报/周报/云文档/链接卡片)在 delivery_log.jsonl 中独立管理状态;
- 飞书无幂等键,语义为"至少一次 + 可检测重复";每条通知携带 snapshot_id 便于识别;
- 通知失败不影响已验证快照的提交,可单独重试通知。
"""
import argparse
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import (DAILY_DIR, FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_OPEN_ID,
                    GITHUB_TOKEN, GLM_API_KEY, PROFILE_DIR, RAW_DIR, README_DIR)
from scripts import delivery_log
from scripts import feishu
from scripts import feishu_doc
from scripts.db import rebuild, refresh_repo_stats, upsert_repo
from scripts.fetch_readmes import fetch_one
from scripts.fetch_trending import fetch_all
from scripts.snapshot_store import (SnapshotExistsError, build_snapshot, load_day_records,
                                    load_snapshot, save_snapshot, snapshot_to_records)

MAX_NEW_PROFILES = 80
TZ = ZoneInfo("Asia/Shanghai")
RAW_META = RAW_DIR / "repo_meta_api.jsonl"
COMPAT_TRENDS = DAILY_DIR / "trends.jsonl"


def today_bj() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def list_done(conn: sqlite3.Connection, date: str, list_type: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM trend_daily WHERE date=? AND list_type=? LIMIT 1",
        (date, list_type)).fetchone() is not None


def append_jsonl(path: Path, obj: dict):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ---------- 阶段 1:捕获 ----------

def capture_stage(conn: sqlite3.Connection, date: str, *, refresh: bool, dry_run: bool,
                  notify_only: bool) -> tuple[list[dict], str]:
    """返回 (records, source_id)。dry-run 只抓取预览,不写任何 source 文件。"""
    if notify_only:
        records, source_id = load_day_records(date)
        if records is None:
            print(f"ERROR: {date} 无 canonical 快照且无 legacy trends.jsonl,无法回放", file=sys.stderr)
            sys.exit(1)
        print(f"[capture] 回放 {date} (source={source_id[:24]}...)")
        return records, source_id

    snap = load_snapshot(date)
    if snap and not refresh:
        print(f"[capture] 回放 canonical 快照 {date} (snapshot_id={snap['snapshot_id'][:24]}...)")
        return snapshot_to_records(snap), snap["snapshot_id"]

    print(f"[capture] 抓取 {date} 榜单{'(refresh,旧版将归档)' if refresh else ''}")
    records = fetch_all()
    for rec in records:
        print(f"  {rec['list_type']}: {len(rec['entries'])} entries")
    if dry_run:
        print("[capture] dry-run: 不写入 canonical 快照与 trends.jsonl")
        return records, "dry-run"

    snapshot = build_snapshot(date, records)
    try:
        save_snapshot(snapshot, overwrite=refresh)
    except SnapshotExistsError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"[capture] canonical 快照已写入 (snapshot_id={snapshot['snapshot_id'][:24]}...)")
    # 兼容导出:trends.jsonl 仍是 db.py 的读取入口,内容与快照一致
    for rec in records:
        if not list_done(conn, date, rec["list_type"]):
            append_jsonl(COMPAT_TRENDS, {"date": date, **rec})
    return records, snapshot["snapshot_id"]


# ---------- 阶段 2:画像 ----------

def profile_new_repos(new_names: list[str], dry_run: bool, conn: sqlite3.Connection) -> dict:
    """为新仓库补 API 元数据 + README + GLM 画像。返回 one_liner 映射。"""
    one_liners = {}
    if not new_names:
        return one_liners
    if dry_run:
        print(f"[profile] dry-run: 跳过 {len(new_names)} 个仓库的画像(不调 API/GLM)")
        return one_liners
    from scripts import glm_client
    from scripts.enrich_github_api import fetch as api_fetch, wait_for_quota

    todo = new_names[:MAX_NEW_PROFILES]
    for i, name in enumerate(todo, 1):
        meta = {"description": None, "language": None, "stars": None, "created_at": None,
                "license": None, "topics": []}
        if GITHUB_TOKEN:
            m, r = api_fetch(name)
            wait_for_quota(r)
            if m:
                append_jsonl(RAW_META, m)
                upsert_repo(conn, m, update_existing=True)
                meta = m
            else:
                print(f"  [{i}/{len(todo)}] {name}: api HTTP {r.status_code}")
        readme_path = README_DIR / (name.replace("/", "__") + ".md")
        status = "cached" if readme_path.exists() else fetch_one(name)
        readme = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""
        if GLM_API_KEY:
            p = glm_client.profile_repo(name, meta, readme)
            if p:
                rec = {"full_name": name, **p, "model": glm_client.GLM_MODEL,
                       "source": "glm-api", "generated_at": now_iso()}
                append_jsonl(PROFILE_DIR / "profiles.jsonl", rec)
                # 同连接写入 profiles 表:重跑不会重复生成/计费(review P1-04)
                conn.execute(
                    "INSERT OR REPLACE INTO profiles VALUES (?,?,?,?,?,?,?,?,?)",
                    (name, p.get("one_liner"), p.get("purpose"), p.get("boundaries"),
                     p.get("tech_highlights"), p.get("maturity"), glm_client.GLM_MODEL,
                     "glm-api", rec["generated_at"]))
                conn.execute("UPDATE repos SET profile_status='done' WHERE full_name=?", (name,))
                conn.commit()
                one_liners[name] = p.get("one_liner", "")
                print(f"  [{i}/{len(todo)}] {name}: profiled ✓")
                continue
        print(f"  [{i}/{len(todo)}] {name}: readme={status}")
    return one_liners


def profile_stage(conn: sqlite3.Connection, records: list[dict], date: str,
                  dry_run: bool) -> dict:
    """识别新面孔/缺画像项目并画像;把当日榜单行增量写入数据库。"""
    profiled_names = {r["full_name"] for r in conn.execute("SELECT full_name FROM profiles")}
    new_names = []
    for rec in records:
        for e in rec["entries"]:
            exists = conn.execute("SELECT 1 FROM repos WHERE full_name=?", (e["repo"],)).fetchone()
            e["is_new"] = exists is None
            if e["is_new"]:
                new_names.append(e["repo"])
                if not dry_run:
                    upsert_repo(conn, {
                        "full_name": e["repo"], "description": e.get("description"),
                        "language": e.get("language"), "verified": 0, "source": "trending",
                    })
            if e["repo"] not in profiled_names and e["repo"] not in new_names:
                new_names.append(e["repo"])
    new_names = sorted(set(new_names))
    print(f"[profile] 新面孔 {sum(1 for r in records for e in r['entries'] if e['is_new'])},"
          f"待补画像 {len(new_names)}")

    one_liners = profile_new_repos(new_names, dry_run, conn)

    if not dry_run:
        # 当日榜单行增量入库(替代原第二次全量重建)
        rows = [(date, rec["list_type"], e["rank"], e["repo"], e.get("stars_today"), None)
                for rec in records for e in rec["entries"]]
        if rows:
            conn.executemany("INSERT OR REPLACE INTO trend_daily VALUES (?,?,?,?,?,?)", rows)
            conn.commit()
        refresh_repo_stats(conn)
    return one_liners


# ---------- 阶段 3:通知 ----------

def build_link_card(title: str, template: str, n_entries: int, url: str,
                    note: str, snapshot_id: str) -> dict:
    return {"msg_type": "interactive", "card": {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
             "content": f"今日 **{n_entries}** 条榜单记录已收录,完整榜单与项目画像见云文档:\n"
                        f"**[📖 打开今日日报文档]({url})**"}},
            {"tag": "note", "elements": [{"tag": "plain_text",
             "content": f"{note} · snapshot {snapshot_id[:19]}"}]},
        ]}}


def push_daily(conn: sqlite3.Connection, date: str, records: list[dict],
               one_liners: dict, snapshot_id: str) -> None:
    """日报:单条消息。文档模式=链接卡片;webhook/无应用=摘要卡片。

    状态机(daily_doc 两段 + daily_message 一段,均独立):
      created      文档已创建但链接未发出 → 重试时复用 document_id,不重复建文档
      link_sent    链接卡片已发出 → 整个日报完成
    """
    sent = delivery_log.latest_event("daily_message", date)
    doc_ev = delivery_log.latest_event("daily_doc", date)
    if sent or delivery_log.legacy_daily_pushed(date):
        print("[notify] 日报已发送过,跳过")
        return

    doc_mode = bool(FEISHU_APP_ID and FEISHU_APP_SECRET)
    n_entries = sum(len(r["entries"]) for r in records)

    if doc_mode:
        reuse = doc_ev and doc_ev.get("status") == "created"
        if reuse or not delivery_log.legacy_doc_done(date):
            try:
                if reuse:
                    url, document_id = doc_ev["url"], doc_ev["document_id"]
                    print(f"[notify] 复用已创建文档: {url}")
                else:
                    stamp_new_faces(records, conn, date)
                    blocks = feishu_doc.build_daily_blocks(
                        date, records, load_profiles_map(conn), conn)
                    doc = feishu_doc.generate_doc(f"GitHub 趋势日报 · {date}", blocks, FEISHU_OPEN_ID)
                    url, document_id = doc["url"], doc["document_id"]
                    delivery_log.append_event(kind="daily_doc", date=date, status="created",
                                              document_id=document_id, url=url,
                                              snapshot_id=snapshot_id)
                ok, msg, message_id = feishu.send(build_link_card(
                    f"📄 GitHub 趋势日报 · {date}", "blue", n_entries, url,
                    "文档含: 今日速览 / 重点项目画像(四维) / 今日新面孔", snapshot_id))
                print(f"feishu daily doc: ok={ok} {url} {msg[:120]}")
                if ok:
                    delivery_log.append_event(kind="daily_doc", date=date, status="link_sent",
                                              document_id=document_id, url=url,
                                              message_id=message_id, snapshot_id=snapshot_id)
                    _record_push_log(date, records)
                    delivery_log.append_event(kind="daily_message", date=date, status="sent",
                                              channel="doc", message_id=message_id,
                                              snapshot_id=snapshot_id)
                return
            except feishu_doc.DocScopeError as e:
                print(f"[notify] 云文档不可用(缺权限?),降级为摘要卡片: {e}")
        else:
            print("[notify] 日报文档已发送过(legacy),跳过")
            return

    # webhook 模式 / 文档降级
    ok, msg, message_id = feishu.send(feishu.build_daily_card(date, records, one_liners))
    print(f"feishu daily: ok={ok} {msg[:200]}")
    if ok:
        delivery_log.append_event(kind="daily_message", date=date, status="sent",
                                  channel="card", message_id=message_id, snapshot_id=snapshot_id)
        _record_push_log(date, records)


def _record_push_log(date: str, records: list[dict]):
    for rec in records:
        for e in rec["entries"]:
            append_jsonl(DAILY_DIR / "push_log.jsonl", {
                "date": date, "list_type": rec["list_type"], "full_name": e["repo"],
                "pushed_at": now_iso()})


def push_weekly(conn: sqlite3.Connection, date: str, snapshot_id: str) -> None:
    """周报:状态独立于日报(不再依赖当天日报是否推送)。"""
    if delivery_log.latest_event("weekly_message", date) or delivery_log.legacy_doc_done(f"week-{date}"):
        print("[notify] 周报已发送过,跳过")
        return
    week_start = (datetime.strptime(date, "%Y-%m-%d").date()
                  - timedelta(days=6)).strftime("%Y-%m-%d")
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
    summary = {"week": f"{week_start} ~ {date}", "new_repos": new_repos,
               "profiled": profiled, "top_new": [(r["full_name"], r["s"]) for r in top_new]}

    doc_mode = bool(FEISHU_APP_ID and FEISHU_APP_SECRET)
    if doc_mode:
        try:
            blocks = feishu_doc.build_weekly_blocks(date, summary, load_profiles_map(conn))
            doc = feishu_doc.generate_doc(f"GitHub 趋势周报 · {date}", blocks, FEISHU_OPEN_ID)
            delivery_log.append_event(kind="weekly_doc", date=date, status="created",
                                      document_id=doc["document_id"], url=doc["url"])
            card = {"msg_type": "interactive", "card": {
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text", "content": f"📄 GitHub 趋势周报 · {date}"},
                           "template": "green"},
                "elements": [{"tag": "div", "text": {"tag": "lark_md",
                             "content": f"本周新面孔 **{summary['new_repos']}** 个、"
                                        f"画像 **{summary['profiled']}** 篇。\n"
                                        f"**[📖 打开本周周报文档]({doc['url']})**"}}]}}
            ok, msg, message_id = feishu.send(card)
            print(f"feishu weekly doc: ok={ok} {doc['url']} {msg[:120]}")
            if ok:
                delivery_log.append_event(kind="weekly_doc", date=date, status="link_sent",
                                          document_id=doc["document_id"], url=doc["url"],
                                          message_id=message_id)
                delivery_log.append_event(kind="weekly_message", date=date, status="sent",
                                          channel="doc", message_id=message_id,
                                          snapshot_id=snapshot_id)
            return
        except feishu_doc.DocScopeError as e:
            print(f"[notify] 周报云文档不可用,降级为摘要卡片: {e}")

    ok, msg, message_id = feishu.send(feishu.build_weekly_card(date, summary))
    print(f"feishu weekly: ok={ok} {msg[:200]}")
    if ok:
        delivery_log.append_event(kind="weekly_message", date=date, status="sent",
                                  channel="card", message_id=message_id, snapshot_id=snapshot_id)


def stamp_new_faces(records: list[dict], conn: sqlite3.Connection, date: str):
    """按 first_trend_date 补 🆕 标记(回放的快照记录不含 is_new)。"""
    first = {r["full_name"]: r["first_trend_date"] for r in conn.execute(
        "SELECT full_name, first_trend_date FROM repos WHERE first_trend_date IS NOT NULL")}
    for rec in records:
        for e in rec["entries"]:
            e.setdefault("is_new", first.get(e["repo"]) == date)


def load_profiles_map(conn: sqlite3.Connection) -> dict:
    return {r["full_name"]: dict(r) for r in conn.execute(
        "SELECT full_name, one_liner, purpose, boundaries, tech_highlights, maturity FROM profiles")}


# ---------- 编排 ----------

def main():
    ap = argparse.ArgumentParser(description="GitHub 趋势每日任务")
    ap.add_argument("--dry-run", action="store_true", help="零副作用预览")
    ap.add_argument("--capture-only", action="store_true", help="只捕获+画像,不通知")
    ap.add_argument("--notify-only", action="store_true", help="只回放快照并通知")
    ap.add_argument("--refresh-snapshot", action="store_true",
                    help="抓取新版本替换 canonical(旧版归档到 snapshots/history)")
    ap.add_argument("--date", default=None, help="指定日期 YYYY-MM-DD(回放)")
    args = ap.parse_args()
    if args.capture_only and args.notify_only:
        ap.error("--capture-only 与 --notify-only 互斥")
    date = args.date or today_bj()
    for d in (DAILY_DIR, PROFILE_DIR, README_DIR):
        d.mkdir(parents=True, exist_ok=True)

    conn = rebuild()
    print(f"[{date}] daily job start "
          f"(dry_run={args.dry_run} capture_only={args.capture_only} "
          f"notify_only={args.notify_only} refresh={args.refresh_snapshot})")

    records, snapshot_id = capture_stage(
        conn, date, refresh=args.refresh_snapshot, dry_run=args.dry_run,
        notify_only=args.notify_only)

    one_liners = {}
    if not args.notify_only:
        one_liners = profile_stage(conn, records, date, dry_run=args.dry_run)
    else:
        stamp_new_faces(records, conn, date)

    if args.dry_run:
        card = feishu.build_daily_card(date, records, one_liners)
        preview = Path(tempfile.gettempdir()) / f"trending_preview_{date}.md"
        preview.write_text(feishu.card_to_markdown(card), encoding="utf-8")
        print(f"[dry-run] 预览写入系统临时目录: {preview}(source 文件零改动)")

    if not (args.dry_run or args.capture_only):
        notify_date_weekday = datetime.strptime(date, "%Y-%m-%d").weekday()
        push_daily(conn, date, records, one_liners, snapshot_id)
        if notify_date_weekday == 6:
            push_weekly(conn, date, snapshot_id)

    total_repos = conn.execute("SELECT count(*) FROM repos").fetchone()[0]
    total_profiles = conn.execute("SELECT count(*) FROM profiles").fetchone()[0]
    print(f"DONE repos={total_repos} profiles={total_profiles} snapshot={snapshot_id[:24]}")


if __name__ == "__main__":
    main()
