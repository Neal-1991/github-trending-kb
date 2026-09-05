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

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import (
    DAILY_DIR,
    FEISHU_APP_ID,
    FEISHU_APP_SECRET,
    FEISHU_OPEN_ID,
    GITHUB_TOKEN,
    GLM_API_KEY,
    PROFILE_DIR,
    RAW_DIR,
    README_DIR,
)
from scripts import delivery_log, feishu, feishu_doc
from scripts.atomic_io import atomic_append_jsonl, atomic_write_text
from scripts.db import rebuild, refresh_repo_stats, reindex_fts, upsert_repo
from scripts.fetch_readmes import fetch_one, persist_missing_status
from scripts.fetch_trending import fetch_all
from scripts.snapshot_store import (
    SnapshotExistsError,
    SnapshotValidationError,
    build_snapshot,
    load_day_records,
    load_snapshot,
    save_snapshot,
    snapshot_to_records,
)

MAX_NEW_PROFILES = 80
TZ = ZoneInfo("Asia/Shanghai")
RAW_META = RAW_DIR / "repo_meta_api.jsonl"
COMPAT_TRENDS = DAILY_DIR / "trends.jsonl"


def today_bj() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def append_jsonl(path: Path, obj: dict):
    atomic_append_jsonl(path, obj)


def sync_compat_records(date: str, records: list[dict]) -> None:
    """原子重写某日兼容导出；可修复 canonical 写入后发生的中断或部分追加。"""
    kept = []
    if COMPAT_TRENDS.exists():
        for line in COMPAT_TRENDS.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("date") != date:
                kept.append(rec)
    kept.extend({"date": date, **rec} for rec in records)
    text = "".join(json.dumps(rec, ensure_ascii=False) + "\n" for rec in kept)
    atomic_write_text(COMPAT_TRENDS, text)


# ---------- 阶段 1:捕获 ----------

def capture_stage(conn: sqlite3.Connection | None, date: str, *, refresh: bool, dry_run: bool,
                  notify_only: bool) -> tuple[list[dict], str]:
    """返回 (records, source_id)。dry-run 只抓取预览,不写任何 source 文件。"""
    parsed_date = datetime.strptime(date, "%Y-%m-%d").date()
    if parsed_date.isoformat() != date:
        raise ValueError("日期必须为 YYYY-MM-DD")
    if date > today_bj():
        raise ValueError("不能捕获或回放未来日期")
    if refresh and (notify_only or date != today_bj()):
        raise ValueError("--refresh-snapshot 只允许刷新今天，不能用于历史回放或 notify-only")
    if notify_only or date != today_bj():
        records, source_id = load_day_records(date)
        if records is None:
            raise ValueError(f"{date} 无 canonical 快照且无 legacy trends.jsonl,无法回放")
        print(f"[capture] 回放 {date} (source={source_id[:24]}...)")
        return records, source_id

    recovered = False
    try:
        snap = load_snapshot(date)
    except SnapshotValidationError as exc:
        if date != today_bj():
            raise  # 历史日期 fail closed:损坏的快照必须显式修复,不能被静默绕过
        recovered = True
        print(f"WARN: 今日快照校验失败,已降级为重新抓取(旧版将归档): {exc}", file=sys.stderr)
        snap = None
    if snap and not refresh:
        print(f"[capture] 回放 canonical 快照 {date} (snapshot_id={snap['snapshot_id'][:24]}...)")
        records = snapshot_to_records(snap)
        if not dry_run:
            sync_compat_records(date, records)
        return records, snap["snapshot_id"]

    print(f"[capture] 抓取 {date} 榜单{'(refresh,旧版将归档)' if refresh else ''}"
          f"{'(快照损坏自愈)' if recovered else ''}")
    records = fetch_all()
    for rec in records:
        print(f"  {rec['list_type']}: {len(rec['entries'])} entries")
    if dry_run:
        print("[capture] dry-run: 不写入 canonical 快照与 trends.jsonl")
        return records, "dry-run"

    snapshot = build_snapshot(date, records)
    try:
        save_snapshot(snapshot, overwrite=refresh or recovered)
    except SnapshotExistsError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"[capture] canonical 快照已写入 (snapshot_id={snapshot['snapshot_id'][:24]}...)")
    # 兼容导出不是 source of truth；整日原子重写，重跑可修复部分写入。
    sync_compat_records(date, records)
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
    from scripts.enrich_github_api import fetch as api_fetch
    from scripts.enrich_github_api import wait_for_quota

    todo = new_names[:MAX_NEW_PROFILES]
    if len(new_names) > MAX_NEW_PROFILES:
        print(f"[profile] 待画像 {len(new_names)} 个超过单轮上限 {MAX_NEW_PROFILES},"
              f"截断 {len(new_names) - MAX_NEW_PROFILES} 个,由后续每日任务自动补齐")
    for i, name in enumerate(todo, 1):
        row = conn.execute(
            "SELECT description, language, stars, created_at, license, topics "
            "FROM repos WHERE full_name=?", (name,)).fetchone()
        meta = dict(row) if row else {}
        meta.setdefault("description", None)
        meta.setdefault("language", None)
        meta.setdefault("stars", None)
        meta.setdefault("created_at", None)
        meta.setdefault("license", None)
        meta.setdefault("topics", [])
        if GITHUB_TOKEN:
            try:
                m, r = api_fetch(name)
                wait_for_quota(r)
            except requests.RequestException as exc:
                print(f"  [{i}/{len(todo)}] {name}: api 暂时不可用: {type(exc).__name__}")
                m, r = None, None
            if m:
                append_jsonl(RAW_META, m)
                upsert_repo(conn, m, update_existing=True)
                meta = m
            elif r is not None:
                print(f"  [{i}/{len(todo)}] {name}: api HTTP {r.status_code}")
        readme_path = README_DIR / (name.replace("/", "__") + ".md")
        status = "cached" if readme_path.exists() else fetch_one(name)
        persist_missing_status({name: "skip" if status == "cached" else status})
        readme = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""
        if not readme:
            conn.execute("UPDATE repos SET profile_status=? WHERE full_name=?",
                         ("no_readme" if status == "no_readme" else "pending", name))
            conn.commit()
            print(f"  [{i}/{len(todo)}] {name}: readme={status},跳过 GLM")
            continue
        conn.execute("UPDATE repos SET profile_status='pending' WHERE full_name=? "
                     "AND profile_status='no_readme'", (name,))
        conn.commit()
        input_hash = glm_client.profile_input_hash(name, meta, readme)
        existing_profile = conn.execute(
            "SELECT one_liner FROM profiles WHERE input_hash=?", (input_hash,)).fetchone()
        if existing_profile:
            one_liners[name] = existing_profile["one_liner"] or ""
            print(f"  [{i}/{len(todo)}] {name}: 相同画像输入已完成,跳过 GLM")
            continue
        if GLM_API_KEY:
            p = glm_client.profile_repo(name, meta, readme)
            if p:
                rec = {"full_name": name, **p, "model": glm_client.GLM_MODEL,
                       "source": "glm-api", "generated_at": now_iso(),
                       "input_hash": input_hash,
                       "schema_version": glm_client.PROFILE_SCHEMA_VERSION,
                       "prompt_version": glm_client.PROMPT_VERSION}
                append_jsonl(PROFILE_DIR / "profiles.jsonl", rec)
                # 同连接写入 profiles 表:重跑不会重复生成/计费(review P1-04)
                conn.execute(
                    "INSERT OR REPLACE INTO profiles VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (name, p.get("one_liner"), p.get("purpose"), p.get("boundaries"),
                     p.get("tech_highlights"), p.get("maturity"), glm_client.GLM_MODEL,
                     "glm-api", rec["generated_at"], input_hash,
                     glm_client.PROFILE_SCHEMA_VERSION, glm_client.PROMPT_VERSION))
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
    known_names = {r["full_name"] for r in conn.execute("SELECT full_name FROM repos")}
    earlier_names = {r["full_name"] for r in conn.execute(
        "SELECT DISTINCT full_name FROM trend_daily WHERE date < ?", (date,))}
    new_names, queued = [], set()
    new_repo_meta = {}
    ordered_records = sorted(enumerate(records), key=lambda pair: (
        0 if pair[1]["list_type"] == "total" else 1, pair[0]))
    for _, rec in ordered_records:
        for e in rec["entries"]:
            name = e["repo"]
            e["is_new"] = name not in earlier_names
            if name not in known_names and name not in new_repo_meta:
                new_repo_meta[name] = {
                    "full_name": name, "description": e.get("description"),
                    "language": e.get("language"), "verified": 0, "source": "trending",
                }
            if name not in profiled_names and name not in queued:
                queued.add(name)
                new_names.append(e["repo"])
    if not dry_run:
        for meta in new_repo_meta.values():
            upsert_repo(conn, meta)
    new_face_count = len({e["repo"] for rec in records for e in rec["entries"] if e["is_new"]})
    print(f"[profile] 新面孔 {new_face_count},"
          f"待补画像 {len(new_names)}")

    from scripts.profile_queue import process_queue
    one_liners = process_queue(conn, new_names, PROFILE_DIR / "pending_queue.json",
                               today_bj(), MAX_NEW_PROFILES, dry_run, profile_new_repos)

    if not dry_run:
        # 当日榜单行增量入库(替代原第二次全量重建)。
        # 真实抓取榜(total/lang:*)不含 arch:total,star_anomaly 恒为 0
        # (显式写 0,与 7 列 schema 对齐,阈值判定只发生在 arch CSV 导入路径)。
        rows = [(date, rec["list_type"], e["rank"], e["repo"], e.get("stars_today"), None, 0)
                for rec in records for e in rec["entries"]]
        with conn:
            conn.execute("DELETE FROM trend_daily WHERE date=? AND "
                         "(list_type='total' OR list_type LIKE 'lang:%')", (date,))
            conn.executemany(
                "INSERT OR REPLACE INTO trend_daily"
                " (date, list_type, rank, full_name, stars, quality, star_anomaly)"
                " VALUES (?,?,?,?,?,?,?)", rows)
        refresh_repo_stats(conn)
        reindex_fts(conn)
    return one_liners


# ---------- 阶段 3:通知 ----------


class NotificationError(RuntimeError):
    """消息未被发送通道确认；保留状态并让编排器以失败退出。"""


def send_notification(card: dict, kind: str, date: str, snapshot_id: str):
    try:
        result = feishu.send(card)
    except (requests.RequestException, RuntimeError) as exc:
        delivery_log.append_event(kind=kind, date=date, snapshot_id=snapshot_id,
                                  status="failed", error_type=type(exc).__name__)
        raise NotificationError(f"{kind} 发送异常: {type(exc).__name__}") from exc
    if not result[0]:
        delivery_log.append_event(kind=kind, date=date, snapshot_id=snapshot_id,
                                  status="failed", error="channel_rejected")
        raise NotificationError(f"{kind} 未发送成功: {result[1][:200]}")
    return result

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
    sent = delivery_log.latest_event("daily_message", date, snapshot_id=snapshot_id)
    doc_ev = delivery_log.latest_event("daily_doc", date, snapshot_id=snapshot_id)
    any_modern = (delivery_log.latest_event("daily_message", date)
                  or delivery_log.latest_event("daily_doc", date))
    if (sent and sent.get("status") == "sent") or (
            not any_modern and delivery_log.legacy_daily_pushed(date)):
        print("[notify] 日报已发送过,跳过")
        return
    if doc_ev and doc_ev.get("status") == "link_sent":
        # 链接已成功发出但最终事件未落盘：补记状态，不重复发送外部消息。
        _record_push_log(date, records)
        delivery_log.append_event(
            kind="daily_message", date=date, status="sent", channel="doc",
            message_id=doc_ev.get("message_id"), snapshot_id=snapshot_id,
            recovered_from="daily_doc:link_sent")
        print("[notify] 日报链接已发送,已修复最终投递状态")
        return

    doc_mode = bool(FEISHU_APP_ID and FEISHU_APP_SECRET)
    n_entries = sum(len(r["entries"]) for r in records)

    if doc_mode:
        reuse = doc_ev and doc_ev.get("status") == "created"
        legacy_doc_blocks = not delivery_log.latest_event("daily_doc", date)
        if reuse or not (legacy_doc_blocks and delivery_log.legacy_doc_done(date)):
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
                ok, msg, message_id = send_notification(build_link_card(
                    f"📄 GitHub 趋势日报 · {date}", "blue", n_entries, url,
                    "文档含: 今日速览 / 重点项目画像(四维) / 今日新面孔", snapshot_id),
                    "daily_message", date, snapshot_id)
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
    stored = {name: p["one_liner"] or "" for name, p in load_profiles_map(conn).items()}
    stored.update(one_liners)
    card = feishu.build_daily_card(date, records, stored)
    card["card"]["elements"].append({"tag": "note", "elements": [
        {"tag": "plain_text", "content": f"snapshot {snapshot_id}"}]})
    ok, msg, message_id = send_notification(card, "daily_message", date, snapshot_id)
    print(f"feishu daily: ok={ok} {msg[:200]}")
    if ok:
        delivery_log.append_event(kind="daily_message", date=date, status="sent",
                                  channel="card", message_id=message_id, snapshot_id=snapshot_id)
        _record_push_log(date, records)


def _record_push_log(date: str, records: list[dict]):
    """兼容推送日志按键原子合并，避免逐行追加中断留下半日数据。"""
    path = DAILY_DIR / "push_log.jsonl"
    by_key = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            by_key[(item["date"], item["list_type"], item["full_name"])] = item
    pushed_at = now_iso()
    for rec in records:
        for e in rec["entries"]:
            item = {"date": date, "list_type": rec["list_type"],
                    "full_name": e["repo"], "pushed_at": pushed_at}
            by_key[(date, rec["list_type"], e["repo"])] = item
    text = "".join(json.dumps(item, ensure_ascii=False) + "\n"
                   for _, item in sorted(by_key.items()))
    atomic_write_text(path, text)


def push_weekly(conn: sqlite3.Connection, date: str, snapshot_id: str) -> None:
    """周报:状态独立于日报(不再依赖当天日报是否推送)。"""
    sent = delivery_log.latest_event("weekly_message", date, snapshot_id=snapshot_id)
    doc_ev = delivery_log.latest_event("weekly_doc", date, snapshot_id=snapshot_id)
    any_modern = (delivery_log.latest_event("weekly_message", date)
                  or delivery_log.latest_event("weekly_doc", date))
    if (sent and sent.get("status") == "sent") or (
            not any_modern and delivery_log.legacy_doc_done(f"week-{date}")):
        print("[notify] 周报已发送过,跳过")
        return
    if doc_ev and doc_ev.get("status") == "link_sent":
        delivery_log.append_event(
            kind="weekly_message", date=date, status="sent", channel="doc",
            message_id=doc_ev.get("message_id"), snapshot_id=snapshot_id,
            recovered_from="weekly_doc:link_sent")
        print("[notify] 周报链接已发送,已修复最终投递状态")
        return
    week_start = (datetime.strptime(date, "%Y-%m-%d").date()
                  - timedelta(days=6)).strftime("%Y-%m-%d")
    top_new = conn.execute("""
        SELECT t.full_name, SUM(t.stars) s FROM trend_daily t
        WHERE t.date BETWEEN ? AND ? AND t.rank <= 10 AND t.list_type = 'total'
        GROUP BY t.full_name
        HAVING (SELECT MIN(date) FROM trend_daily t2 WHERE t2.full_name = t.full_name) >= ?
        ORDER BY s DESC LIMIT 10
    """, (week_start, date, week_start)).fetchall()
    new_repos = conn.execute(
        "SELECT count(*) FROM repos WHERE first_trend_date BETWEEN ? AND ?",
        (week_start, date)).fetchone()[0]
    week_end = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).date().isoformat()
    profiled = conn.execute(
        "SELECT count(*) FROM profiles WHERE julianday(generated_at) >= julianday(?) "
        "AND julianday(generated_at) < julianday(?)",
        (f"{week_start}T00:00:00+08:00", f"{week_end}T00:00:00+08:00")).fetchone()[0]
    summary = {"week": f"{week_start} ~ {date}", "new_repos": new_repos,
               "profiled": profiled, "top_new": [(r["full_name"], r["s"]) for r in top_new]}

    doc_mode = bool(FEISHU_APP_ID and FEISHU_APP_SECRET)
    if doc_mode:
        try:
            reuse = doc_ev and doc_ev.get("status") == "created"
            if reuse:
                doc = {"document_id": doc_ev["document_id"], "url": doc_ev["url"]}
                print(f"[notify] 复用已创建周报文档: {doc['url']}")
            else:
                blocks = feishu_doc.build_weekly_blocks(date, summary, load_profiles_map(conn))
                doc = feishu_doc.generate_doc(f"GitHub 趋势周报 · {date}", blocks, FEISHU_OPEN_ID)
                delivery_log.append_event(kind="weekly_doc", date=date, status="created",
                                          document_id=doc["document_id"], url=doc["url"],
                                          snapshot_id=snapshot_id)
            card = {"msg_type": "interactive", "card": {
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text", "content": f"📄 GitHub 趋势周报 · {date}"},
                           "template": "green"},
                "elements": [{"tag": "div", "text": {"tag": "lark_md",
                             "content": f"本周新面孔 **{summary['new_repos']}** 个、"
                                        f"画像 **{summary['profiled']}** 篇。\n"
                                        f"**[📖 打开本周周报文档]({doc['url']})**"}}]}}
            card["card"]["elements"].append({"tag": "note", "elements": [
                {"tag": "plain_text", "content": f"snapshot {snapshot_id}"}]})
            ok, msg, message_id = send_notification(card, "weekly_message", date, snapshot_id)
            print(f"feishu weekly doc: ok={ok} {doc['url']} {msg[:120]}")
            if ok:
                delivery_log.append_event(kind="weekly_doc", date=date, status="link_sent",
                                          document_id=doc["document_id"], url=doc["url"],
                                          message_id=message_id, snapshot_id=snapshot_id)
                delivery_log.append_event(kind="weekly_message", date=date, status="sent",
                                          channel="doc", message_id=message_id,
                                          snapshot_id=snapshot_id)
            return
        except feishu_doc.DocScopeError as e:
            print(f"[notify] 周报云文档不可用,降级为摘要卡片: {e}")

    card = feishu.build_weekly_card(date, summary)
    card["card"]["elements"].append({"tag": "note", "elements": [
        {"tag": "plain_text", "content": f"snapshot {snapshot_id}"}]})
    ok, msg, message_id = send_notification(card, "weekly_message", date, snapshot_id)
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
    temp_owner = None
    conn = None
    try:
        print(f"[{date}] daily job start "
              f"(dry_run={args.dry_run} capture_only={args.capture_only} "
              f"notify_only={args.notify_only} refresh={args.refresh_snapshot})")
        # 捕获无需 DB；先校验/修复今日快照，再由已验证 source 构建派生库。
        records, snapshot_id = capture_stage(
            None, date, refresh=args.refresh_snapshot, dry_run=args.dry_run,
            notify_only=args.notify_only)
        if args.dry_run:
            # 只在系统临时目录构建派生库，项目内 DB 与目录均不变化。
            temp_owner = tempfile.TemporaryDirectory(prefix="trending-kb-dry-run-")
            conn = rebuild(Path(temp_owner.name) / "trending.db")
        else:
            for d in (DAILY_DIR, PROFILE_DIR, README_DIR):
                d.mkdir(parents=True, exist_ok=True)
            conn = rebuild()

        one_liners = {}
        if not args.notify_only:
            one_liners = profile_stage(conn, records, date, dry_run=args.dry_run)
        else:
            stamp_new_faces(records, conn, date)

        one_liners = {name: p["one_liner"] or "" for name, p in load_profiles_map(conn).items()}
        notification_configured = bool(feishu.FEISHU_WEBHOOK or (
            feishu.FEISHU_APP_ID and feishu.FEISHU_APP_SECRET
            and (feishu.FEISHU_OPEN_ID or feishu.FEISHU_CHAT_ID)))
        if args.dry_run or (not args.capture_only and not notification_configured):
            card = feishu.build_daily_card(date, records, one_liners)
            preview = Path(tempfile.gettempdir()) / f"trending_preview_{date}.md"
            preview.write_text(feishu.card_to_markdown(card), encoding="utf-8")
            print(f"[preview] 预览写入系统临时目录: {preview}")

        if not (args.dry_run or args.capture_only):
            if not notification_configured:
                if args.notify_only:
                    raise NotificationError("notify-only 未配置发送通道，已生成本地预览")
                print("[notify] 未配置发送通道，已降级本地预览")
            else:
                notify_stage(conn, date, records, one_liners, snapshot_id)

        total_repos = conn.execute("SELECT count(*) FROM repos").fetchone()[0]
        total_profiles = conn.execute("SELECT count(*) FROM profiles").fetchone()[0]
        print(f"DONE repos={total_repos} profiles={total_profiles} snapshot={snapshot_id[:24]}")
    finally:
        if conn is not None:
            conn.close()
        if temp_owner is not None:
            temp_owner.cleanup()


def notify_stage(conn, date, records, one_liners, snapshot_id):
    """各通知独立尝试，失败统一汇总，已成功的事件不会因另一个失败而丢失。"""
    errors = []
    jobs = [("daily_message", lambda: push_daily(conn, date, records, one_liners, snapshot_id))]
    if datetime.strptime(date, "%Y-%m-%d").weekday() == 6:
        jobs.append(("weekly_message", lambda: push_weekly(conn, date, snapshot_id)))
    for kind, job in jobs:
        try:
            job()
        except Exception as exc:
            errors.append(f"{kind}: {type(exc).__name__}")
            # 文档创建等发生于 send_notification 之前的异常同样需要可观测。
            if not isinstance(exc, NotificationError):
                state = delivery_log.latest_event(kind, date, snapshot_id=snapshot_id)
                if not state or state.get("status") != "sent":
                    delivery_log.append_event(kind=kind, date=date, snapshot_id=snapshot_id,
                                              status="failed", error_type=type(exc).__name__)
            print(f"[notify] {kind} 失败: {type(exc).__name__}", file=sys.stderr)
    if errors:
        raise NotificationError("; ".join(errors))


if __name__ == "__main__":
    main()
