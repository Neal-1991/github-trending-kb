"""批量生成项目画像(GLM API):按优先级补齐缺少画像的核心仓库。

优先级:Top10 上榜天数 → 单日峰值;跳过无 README 的仓库。
可断点续跑(已有画像的自动跳过)。
用法:
  python scripts/profile_batch.py            # 跑完当前批次上限
  python scripts/profile_batch.py --limit 50 # 最多补 50 个
  python scripts/profile_batch.py --min-core-days 5
"""
import argparse
import ast
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import GLM_API_KEY, GLM_MODEL, PROFILE_DIR, README_DIR
from scripts import glm_client
from scripts.atomic_io import atomic_append_jsonl
from scripts.db import connect


def parse_topics(s) -> list:
    """topics 列可能是 JSON、单引号数组或纯逗号串,宽容解析。"""
    if not s:
        return []
    if isinstance(s, list):
        return s
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except (ValueError, TypeError):
        pass
    try:
        v = ast.literal_eval(s)
        return list(v) if isinstance(v, (list, tuple)) else []
    except (ValueError, SyntaxError):
        return [t.strip() for t in re.split(r"[,;]", s) if t.strip()][:10]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--min-core-days", type=int, default=3,
                    help="只画像 Top10 上榜天数达到该值的项目(0=全部有README的)")
    args = ap.parse_args()

    if not GLM_API_KEY:
        print("GLM_API_KEY 未配置(.env),无法生成画像。")
        sys.exit(1)

    conn = connect()
    # 先按优先级取全部候选,过滤 README 后再截 limit:避免候选窗口被无 README 项目占满
    # 而饿死有效候选(review P1-04)。有画像的项目不进入候选(含本次运行刚完成的)。
    candidates = conn.execute("""
      SELECT r.full_name, r.description, r.language, r.topics, r.license,
             r.stars, r.created_at, r.core_days, r.best_daily_stars
      FROM repos r
      WHERE r.profile_status != 'done'
        AND r.full_name NOT IN (SELECT full_name FROM profiles)
        AND r.profile_status != 'no_readme'
        AND r.core_days >= ?
      ORDER BY r.core_days DESC, r.best_daily_stars DESC
    """, (args.min_core_days,)).fetchall()

    todo = []
    skipped_no_readme = 0
    for r in candidates:
        readme_path = README_DIR / (r["full_name"].replace("/", "__") + ".md")
        if readme_path.exists():
            todo.append((r, readme_path))
            if len(todo) >= args.limit:
                break
        else:
            skipped_no_readme += 1
    print(f"待画像: {len(todo)} (候选 {len(candidates)},跳过无 README {skipped_no_readme})")

    ok = 0
    for i, (r, readme_path) in enumerate(todo, 1):
        meta = {
            "description": r["description"], "language": r["language"],
            "topics": parse_topics(r["topics"]), "license": r["license"],
            "stars": r["stars"], "created_at": r["created_at"],
        }
        readme = readme_path.read_text(encoding="utf-8")
        input_hash = glm_client.profile_input_hash(r["full_name"], meta, readme, GLM_MODEL)
        if conn.execute("SELECT 1 FROM profiles WHERE input_hash=?", (input_hash,)).fetchone():
            print(f"  [{i}/{len(todo)}] {r['full_name']} = 相同输入已完成,跳过")
            continue
        p = glm_client.profile_repo(r["full_name"], meta, readme)
        if p:
            rec = {"full_name": r["full_name"], **p, "model": GLM_MODEL,
                   "source": "glm-api",
                   "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                   "input_hash": input_hash,
                   "schema_version": glm_client.PROFILE_SCHEMA_VERSION,
                   "prompt_version": glm_client.PROMPT_VERSION}
            atomic_append_jsonl(PROFILE_DIR / "profiles.jsonl", rec)
            # 同一连接写 profiles 表:重跑不重复生成/计费;Web 端 rebuild 后可见
            conn.execute("INSERT OR REPLACE INTO profiles VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (r["full_name"], p.get("one_liner"), p.get("purpose"),
                          p.get("boundaries"), p.get("tech_highlights"), p.get("maturity"),
                          GLM_MODEL, "glm-api", rec["generated_at"], input_hash,
                          glm_client.PROFILE_SCHEMA_VERSION, glm_client.PROMPT_VERSION))
            conn.execute("UPDATE repos SET profile_status='done' WHERE full_name=?",
                         (r["full_name"],))
            conn.commit()
            ok += 1
            print(f"  [{i}/{len(todo)}] {r['full_name']} ✓ (core_days={r['core_days']})")
        else:
            print(f"  [{i}/{len(todo)}] {r['full_name']} ✗ 画像失败,下次重试")
    print(f"DONE ok={ok}/{len(todo)};如需 Web 可见请运行 python scripts/build_db.py")
    conn.close()


if __name__ == "__main__":
    main()
