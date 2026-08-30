"""全量重建 SQLite 知识库:data/trending.db(含 FTS5 检索索引)。幂等,可随时重跑。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.db import rebuild


def main():
    conn = rebuild()
    n_repos = conn.execute("SELECT count(*) FROM repos").fetchone()[0]
    n_trends = conn.execute("SELECT count(*) FROM trend_daily").fetchone()[0]
    n_days = conn.execute("SELECT count(DISTINCT date) FROM trend_daily").fetchone()[0]
    n_profiles = conn.execute("SELECT count(*) FROM profiles").fetchone()[0]
    n_langs = conn.execute(
        "SELECT count(*) FROM repos WHERE language IS NOT NULL").fetchone()[0]
    print(f"repos={n_repos} (language known: {n_langs})")
    print(f"trend rows={n_trends} across {n_days} days")
    print(f"profiles={n_profiles}")
    print("FTS5 tokenizer:", conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='search_fts'").fetchone()[0].split("tokenize=")[-1])


if __name__ == "__main__":
    main()
