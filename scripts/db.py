"""共享 SQLite 层:schema、连接、FTS 索引、全量重建。

数据流设计:CSV/JSONL 文件是 source of truth(进 git),SQLite 是派生索引
(不进 git,本地与 Actions 每次重建,秒级完成)。
"""
import csv
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import DAILY_DIR, DB_PATH, PROFILE_DIR, RAW_DIR

SCHEMA = """
CREATE TABLE repos (
  full_name TEXT PRIMARY KEY,
  description TEXT,
  language TEXT,
  topics TEXT,                -- JSON array
  homepage TEXT,
  license TEXT,
  default_branch TEXT,
  fork INTEGER,
  archived INTEGER,           -- 0/1/NULL(未知)
  stars INTEGER,              -- 当前快照值(快照表或 API)
  forks INTEGER,
  open_issues INTEGER,
  created_at TEXT,
  pushed_at TEXT,
  verified INTEGER DEFAULT 0, -- 1=有可靠元数据(快照或API)
  source TEXT,                -- snapshot / api / snapshot+api
  -- 趋势聚合(每次重建/每日任务后刷新)
  first_trend_date TEXT,
  last_trend_date TEXT,
  trend_days INTEGER,         -- 累计上榜天数(全部榜)
  core_days INTEGER,          -- 进入 Top10 的天数
  best_rank INTEGER,
  best_daily_stars INTEGER,
  profile_status TEXT DEFAULT 'pending'  -- pending / done / no_readme / low_priority
);

CREATE TABLE trend_daily (
  date TEXT NOT NULL,
  list_type TEXT NOT NULL,    -- arch:total / total / lang:python / ...
  rank INTEGER NOT NULL,
  full_name TEXT NOT NULL,
  stars INTEGER,              -- 当日新增星标(arch)或 stars_today(实时榜)
  quality TEXT,               -- arch 专用: full / partial
  PRIMARY KEY (date, list_type, rank)
);
CREATE INDEX idx_trend_repo ON trend_daily(full_name);
CREATE INDEX idx_trend_date ON trend_daily(date);

CREATE TABLE profiles (
  full_name TEXT PRIMARY KEY,
  one_liner TEXT,
  purpose TEXT,
  boundaries TEXT,
  tech_highlights TEXT,
  maturity TEXT,
  model TEXT,
  source TEXT,                -- zcode / glm-api
  generated_at TEXT
);

CREATE TABLE push_log (
  date TEXT NOT NULL,
  list_type TEXT NOT NULL,
  full_name TEXT NOT NULL,
  pushed_at TEXT,
  PRIMARY KEY (date, list_type, full_name)
);
"""


def connect(db_path=DB_PATH) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def sqlite_version() -> tuple:
    return tuple(int(x) for x in sqlite3.sqlite_version.split("."))


def reindex_fts(conn: sqlite3.Connection):
    """重建全文索引。trigram 分词支持中文子串检索;老 SQLite 回退 unicode61。"""
    version = sqlite_version()
    has_trigram = version >= (3, 34, 0)
    tokenizer = "tokenize='trigram'" if has_trigram else "tokenize='unicode61'"
    conn.executescript(f"""
      DROP TABLE IF EXISTS search_fts;
      CREATE VIRTUAL TABLE search_fts USING fts5(
        full_name, description, topics, language, one_liner,
        purpose, boundaries, tech_highlights, maturity,
        {tokenizer}
      );
    """)
    conn.execute("""
      INSERT INTO search_fts
      SELECT r.full_name, COALESCE(r.description,''), COALESCE(r.topics,''),
             COALESCE(r.language,''), COALESCE(p.one_liner,''),
             COALESCE(p.purpose,''), COALESCE(p.boundaries,''),
             COALESCE(p.tech_highlights,''), COALESCE(p.maturity,'')
      FROM repos r LEFT JOIN profiles p USING (full_name)
    """)
    conn.commit()


def refresh_repo_stats(conn: sqlite3.Connection):
    conn.executescript("""
      UPDATE repos SET
        first_trend_date = (SELECT MIN(date) FROM trend_daily t WHERE t.full_name = repos.full_name),
        last_trend_date  = (SELECT MAX(date) FROM trend_daily t WHERE t.full_name = repos.full_name),
        trend_days       = (SELECT COUNT(DISTINCT date) FROM trend_daily t WHERE t.full_name = repos.full_name),
        core_days        = (SELECT COUNT(DISTINCT date) FROM trend_daily t WHERE t.full_name = repos.full_name AND t.rank <= 10),
        best_rank        = (SELECT MIN(rank) FROM trend_daily t WHERE t.full_name = repos.full_name),
        best_daily_stars = (SELECT MAX(stars) FROM trend_daily t WHERE t.full_name = repos.full_name);
    """)
    conn.commit()


def rebuild(db_path=DB_PATH, close: sqlite3.Connection | None = None) -> sqlite3.Connection:
    """从 raw/daily/profiles 文件全量重建数据库(幂等)。

    close: 传入旧连接先关闭,避免 Windows 文件锁;unlink 重试应对并发读(如 Web 服务)。
    """
    db_path = Path(db_path)
    if close is not None:
        close.close()
    import time as _time
    for attempt in range(30):
        try:
            if db_path.exists():
                db_path.unlink()
            break
        except PermissionError:
            _time.sleep(1)
    else:
        raise RuntimeError(
            f"{db_path} 被其他进程占用(可能是运行中的 Web 服务),请先停止 uvicorn 再重建")
    conn = connect(db_path)
    conn.executescript(SCHEMA)

    # 1) 仓库元数据:repos 快照
    snap = RAW_DIR / "repo_meta_snapshot.csv"
    if snap.exists():
        for row in csv_reader(snap):
            upsert_repo(conn, {
                "full_name": row["full_name"],
                "description": row["description"] or None,
                "language": row["language"] or None,
                "topics": row["topics"] or "[]",
                "homepage": row["homepage"] or None,
                "license": row["license_key"] or None,
                "default_branch": row["default_branch"] or None,
                "fork": 1 if row["fork"] == "true" else 0,
                "archived": 1 if row["archived"] == "true" else 0,
                "stars": int(row["stargazers_count"] or 0),
                "forks": int(row["forks_count"] or 0),
                "open_issues": int(row["open_issues_count"] or 0),
                "created_at": row["created_at"] or None,
                "pushed_at": row["pushed_at"] or None,
                "verified": 1,
                "source": "snapshot",
            })

    # 1b) GitHub API 补全(可选,行覆盖快照)
    api_meta = RAW_DIR / "repo_meta_api.jsonl"
    if api_meta.exists():
        for line in api_meta.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            m = json.loads(line)
            upsert_repo(conn, {
                "full_name": m["full_name"],
                "description": m.get("description"),
                "language": m.get("language"),
                "topics": json.dumps(m.get("topics") or [], ensure_ascii=False),
                "homepage": m.get("homepage"),
                "license": m.get("license"),
                "default_branch": m.get("default_branch"),
                "fork": 1 if m.get("fork") else 0,
                "archived": 1 if m.get("archived") else 0,
                "stars": m.get("stars"),
                "forks": m.get("forks"),
                "open_issues": m.get("open_issues"),
                "created_at": m.get("created_at"),
                "pushed_at": m.get("pushed_at"),
                "verified": 1,
                "source": "api",
            }, update_existing=True)

    # 2) 趋势:GH Archive 重建榜(文件内已按日期+名次排序)
    arch = RAW_DIR / "trends_gharchive.csv"
    if arch.exists():
        by_date = defaultdict(list)
        rows = list(csv_reader(arch))
        for row in rows:
            by_date[row["date"]].append(row)
        conn.executemany(
            "INSERT OR REPLACE INTO trend_daily VALUES (?,?,?,?,?,?)",
            (
                (d, "arch:total", i + 1, r["repo"], int(r["stars"]), r["quality"])
                for d, rs in sorted(by_date.items())
                for i, r in enumerate(rs)
            ),
        )
        conn.commit()

    # 2b) 趋势:每日真实榜单 JSONL
    trends_jsonl = DAILY_DIR / "trends.jsonl"
    if trends_jsonl.exists():
        entries = []
        for line in trends_jsonl.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            for e in rec["entries"]:
                entries.append((rec["date"], rec["list_type"], e["rank"], e["repo"],
                                e.get("stars_today"), None))
        conn.executemany("INSERT OR REPLACE INTO trend_daily VALUES (?,?,?,?,?,?)", entries)
        conn.commit()

    # 2c) 趋势中出现但缺元数据的仓库补占位行,保证 repos ⊇ trend_daily 的仓库集合
    conn.execute("""
      INSERT OR IGNORE INTO repos (full_name, verified, source, profile_status)
      SELECT DISTINCT full_name, 0, 'trend', 'pending' FROM trend_daily
    """)
    conn.commit()

    # 3) 画像
    profiles_file = PROFILE_DIR / "profiles.jsonl"
    if profiles_file.exists():
        for line in profiles_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            p = json.loads(line)
            conn.execute(
                "INSERT OR REPLACE INTO profiles VALUES (?,?,?,?,?,?,?,?,?)",
                (p["full_name"], p.get("one_liner"), p.get("purpose"), p.get("boundaries"),
                 p.get("tech_highlights"), p.get("maturity"), p.get("model"),
                 p.get("source"), p.get("generated_at")),
            )
        conn.execute("UPDATE repos SET profile_status='done' WHERE full_name IN (SELECT full_name FROM profiles)")
        conn.commit()

    # 4) 推送日志
    push_file = DAILY_DIR / "push_log.jsonl"
    if push_file.exists():
        for line in push_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            conn.execute("INSERT OR REPLACE INTO push_log VALUES (?,?,?,?)",
                         (r["date"], r["list_type"], r["full_name"], r.get("pushed_at")))
        conn.commit()

    refresh_repo_stats(conn)
    reindex_fts(conn)
    return conn


REPO_COLS = frozenset({
    "full_name", "description", "language", "topics", "homepage", "license",
    "default_branch", "fork", "archived", "stars", "forks", "open_issues",
    "created_at", "pushed_at", "verified", "source",
    "first_trend_date", "last_trend_date", "trend_days", "core_days",
    "best_rank", "best_daily_stars", "profile_status",
})


def upsert_repo(conn: sqlite3.Connection, m: dict, update_existing: bool = False):
    m = {k: v for k, v in m.items() if k in REPO_COLS}
    if isinstance(m.get("topics"), list):
        m["topics"] = json.dumps(m["topics"], ensure_ascii=False)
    existing = conn.execute("SELECT 1 FROM repos WHERE full_name=?", (m["full_name"],)).fetchone()
    if existing and not update_existing:
        return
    if existing:
        sets = ",".join(f"{k}=?" for k in m if k != "full_name")
        conn.execute(f"UPDATE repos SET {sets} WHERE full_name=?",
                     [v for k, v in m.items() if k != "full_name"] + [m["full_name"]])
    else:
        cols = ",".join(m.keys())
        qs = ",".join("?" for _ in m)
        conn.execute(f"INSERT INTO repos ({cols}) VALUES ({qs})", list(m.values()))
    conn.commit()


def csv_reader(path: Path):
    return csv.DictReader(open(path, encoding="utf-8"))
