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
from scripts.db import connect, rebuild


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
    rows = conn.execute("""
      SELECT r.full_name, r.description, r.language, r.topics, r.license,
             r.stars, r.created_at, r.core_days, r.best_daily_stars
      FROM repos r
      LEFT JOIN profiles p ON p.full_name = r.full_name
      WHERE p.full_name IS NULL
        AND r.profile_status != 'no_readme'
        AND r.core_days >= ?
      ORDER BY r.core_days DESC, r.best_daily_stars DESC
      LIMIT ?
    """, (args.min_core_days, args.limit)).fetchall()

    todo = []
    for r in rows:
        readme_path = README_DIR / (r["full_name"].replace("/", "__") + ".md")
        if readme_path.exists():
            todo.append((r, readme_path))
    print(f"待画像: {len(todo)} (候选 {len(rows)},跳过无 README {len(rows) - len(todo)})")

    ok = 0
    for i, (r, readme_path) in enumerate(todo, 1):
        meta = {
            "description": r["description"], "language": r["language"],
            "topics": parse_topics(r["topics"]), "license": r["license"],
            "stars": r["stars"], "created_at": r["created_at"],
        }
        readme = readme_path.read_text(encoding="utf-8")
        p = glm_client.profile_repo(r["full_name"], meta, readme)
        if p:
            rec = {"full_name": r["full_name"], **p, "model": GLM_MODEL,
                   "source": "glm-api",
                   "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()}
            with (PROFILE_DIR / "profiles.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            conn.execute("UPDATE repos SET profile_status='done' WHERE full_name=?",
                         (r["full_name"],))
            conn.commit()
            ok += 1
            print(f"  [{i}/{len(todo)}] {r['full_name']} ✓ (core_days={r['core_days']})")
        else:
            print(f"  [{i}/{len(todo)}] {r['full_name']} ✗ 画像失败,下次重试")
    print(f"DONE ok={ok}/{len(todo)};如需 Web 可见请运行 python scripts/build_db.py")


if __name__ == "__main__":
    main()
