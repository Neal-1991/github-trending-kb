"""跨日画像队列：当天榜单优先，其余名额补积压；状态落盘后可跨 CI checkout 恢复。"""
import json
from datetime import date, timedelta
from pathlib import Path

from scripts.atomic_io import atomic_write_json


def process_queue(conn, current_names: list[str], path: Path, today: str,
                  limit: int, dry_run: bool, profile) -> dict:
    tasks = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    completed = {r["full_name"] for r in conn.execute("SELECT full_name FROM profiles")}
    no_readme = {r["full_name"] for r in conn.execute(
        "SELECT full_name FROM repos WHERE profile_status='no_readme'")}
    # 从真实榜恢复已有积压，不把整个五年历史池无条件纳入每日模型预算。
    backlog = [r["full_name"] for r in conn.execute(
        "SELECT full_name FROM trend_daily WHERE list_type='total' OR list_type LIKE 'lang:%' "
        "GROUP BY full_name ORDER BY MIN(date), full_name")]
    for name in dict.fromkeys(current_names + backlog):
        if name not in completed and name not in tasks:
            missing = name in no_readme
            tasks[name] = {"status": "no_readme" if missing else "pending", "attempts": 0,
                           "queued_at": today, "retry_at": (
                               date.fromisoformat(today) + timedelta(days=30 if missing else 0)
                           ).isoformat()}
    tasks = {name: task for name, task in tasks.items() if name not in completed}
    due = {name for name, task in tasks.items() if task["retry_at"] <= today}
    ordered = [name for name in current_names if name in due]
    ordered += sorted(due - set(ordered), key=lambda n: (tasks[n]["queued_at"], n))
    ordered = list(dict.fromkeys(ordered))
    if dry_run:
        return profile(ordered[:limit], True, conn)
    atomic_write_json(path, tasks, indent=2)
    result = {}
    for name in ordered[:limit]:
        task = tasks[name]
        task["attempts"] += 1
        task["status"] = "retry"
        task["retry_at"] = (date.fromisoformat(today) + timedelta(days=1)).isoformat()
        # 先记尝试；意外中断仍有可恢复的队列，不依赖仓库次日再次上榜。
        atomic_write_json(path, tasks, indent=2)
        result.update(profile([name], False, conn))
        if conn.execute("SELECT 1 FROM profiles WHERE full_name=?", (name,)).fetchone():
            del tasks[name]
        else:
            row = conn.execute("SELECT profile_status FROM repos WHERE full_name=?", (name,)).fetchone()
            if row and row["profile_status"] == "no_readme":
                task["status"] = "no_readme"
                task["retry_at"] = (date.fromisoformat(today) + timedelta(days=30)).isoformat()
        atomic_write_json(path, tasks, indent=2)
    print(f"[profile] 队列剩余 {len(tasks)}，本轮处理 {min(len(ordered), limit)}")
    return result
