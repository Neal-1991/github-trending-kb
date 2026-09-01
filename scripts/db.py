"""共享 SQLite 层:schema、连接、FTS 索引、全量重建。

数据流设计:CSV/JSONL/快照文件是 source of truth(进 git),SQLite 是派生索引
(不进 git,本地与 Actions 每次重建)。

重建是原子的:先在同目录构建临时库,校验(integrity/schema/FTS 行数)通过后
os.replace 正式库;任何一步失败,正式库保持不变。聚合口径为 trusted 语义:
- quality='full' 的记录全部可信;
- quality='partial' 仅 rank<=10 可信(数据源口径);
- quality='degraded' 仅保留原始记录,不参与聚合;
- 真实榜(total/lang:*,quality 为空)全部可信;
- 历史重建榜中单日星标 >= ARCH_DAILY_STAR_ANOMALY 的记录视为疑似刷星,
  不参与 best_daily_stars 与"现象级爆发"展示(raw 记录保留)。
"""
import csv
import json
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import ARCH_DAILY_STAR_ANOMALY, DAILY_DIR, DB_PATH, PROFILE_DIR, RAW_DIR
from scripts.atomic_io import replace_file_with_retry

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
  source TEXT,                -- snapshot / api / snapshot+api / trend
  -- 趋势聚合(每次重建/每日任务后刷新,trusted 口径)
  first_trend_date TEXT,
  last_trend_date TEXT,
  trend_days INTEGER,         -- 累计上榜天数(trusted,全部榜)
  core_days INTEGER,          -- 历史重建榜 arch:total Top10 天数(页面口径)
  best_rank INTEGER,          -- trusted 口径最佳名次
  best_daily_stars INTEGER,   -- trusted 口径单日最高星标(排除疑似刷星)
  profile_status TEXT DEFAULT 'pending'  -- pending / done / no_readme / low_priority
);

CREATE TABLE trend_daily (
  date TEXT NOT NULL,
  list_type TEXT NOT NULL,    -- arch:total / total / lang:python / ...
  rank INTEGER NOT NULL,
  full_name TEXT NOT NULL,
  stars INTEGER,              -- 当日新增星标(arch)或 stars_today(实时榜)
  quality TEXT,               -- arch 专用: full / partial / degraded
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

# trusted 聚合口径:full 全可信、partial 仅 Top10、degraded 排除、真实榜(quality IS NULL)可信
_TRUSTED_WHERE = """
  (quality IS NULL OR quality = 'full' OR (quality = 'partial' AND rank <= 10))
"""
# 疑似刷星:历史重建榜单日星标异常高(raw 保留,但不作为可信峰值展示)
_ANOMALY_WHERE = " NOT (list_type = 'arch:total' AND stars >= %d)" % ARCH_DAILY_STAR_ANOMALY


def connect(db_path=None) -> sqlite3.Connection:
    db_path = Path(db_path or DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def connect_ro(db_path=None) -> sqlite3.Connection:
    """Web 等只读场景:缺库时明确失败,不创建空库。"""
    db_path = Path(db_path or DB_PATH)
    if not db_path.exists():
        raise FileNotFoundError(f"数据库不存在: {db_path}(请先运行 scripts/build_db.py)")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
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
    """trusted 口径聚合(详见模块 docstring)。单次事务。"""
    conn.executescript("""
      UPDATE repos SET
        first_trend_date = (SELECT MIN(date) FROM trend_daily t
                            WHERE t.full_name = repos.full_name AND """ + _TRUSTED_WHERE.strip() + """),
        last_trend_date  = (SELECT MAX(date) FROM trend_daily t
                            WHERE t.full_name = repos.full_name AND """ + _TRUSTED_WHERE.strip() + """),
        trend_days       = (SELECT COUNT(DISTINCT date) FROM trend_daily t
                            WHERE t.full_name = repos.full_name AND """ + _TRUSTED_WHERE.strip() + """),
        core_days        = (SELECT COUNT(DISTINCT date) FROM trend_daily t
                            WHERE t.full_name = repos.full_name
                              AND t.list_type = 'arch:total' AND t.rank <= 10),
        best_rank        = (SELECT MIN(rank) FROM trend_daily t
                            WHERE t.full_name = repos.full_name AND """ + _TRUSTED_WHERE.strip() + """),
        best_daily_stars = (SELECT MAX(stars) FROM trend_daily t
                            WHERE t.full_name = repos.full_name AND """ + _TRUSTED_WHERE.strip() + """
                              AND """ + _ANOMALY_WHERE.strip() + """);
    """)
    conn.commit()


def _import_sources(conn: sqlite3.Connection):
    """从 source 文件导入全部数据。每个阶段一个事务;异常向上抛,由 rebuild 清理临时库。"""
    # 1) 仓库元数据:repos 快照(INSERT OR IGNORE = first-wins,与历史行为一致;
    #    重复与冲突由 audit_data.py 报告)
    snap = RAW_DIR / "repo_meta_snapshot.csv"
    if snap.exists():
        with snap.open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        conn.executemany(
            "INSERT OR IGNORE INTO repos (full_name, description, language, topics, homepage,"
            " license, default_branch, fork, archived, stars, forks, open_issues,"
            " created_at, pushed_at, verified, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'snapshot')",
            (
                (r["full_name"], r["description"] or None, r["language"] or None,
                 _normalize_topics(r["topics"]), r["homepage"] or None, r["license_key"] or None,
                 r["default_branch"] or None, 1 if r["fork"] == "true" else 0,
                 1 if r["archived"] == "true" else 0, int(r["stargazers_count"] or 0),
                 int(r["forks_count"] or 0), int(r["open_issues_count"] or 0),
                 r["created_at"] or None, r["pushed_at"] or None)
                for r in rows
            ),
        )
        conn.commit()

    # 1b) GitHub API 补全(覆盖快照;full_name 为 API 响应中的 canonical 名,
    #     同时保存请求名与 repository id,为身份迁移做准备)
    api_meta = RAW_DIR / "repo_meta_api.jsonl"
    if api_meta.exists():
        records = []
        for line in api_meta.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            m = json.loads(line)
            records.append((
                m["full_name"], m.get("description"), m.get("language"),
                json.dumps(m.get("topics") or [], ensure_ascii=False), m.get("homepage"),
                m.get("license"), m.get("default_branch"),
                1 if m.get("fork") else 0, 1 if m.get("archived") else 0,
                m.get("stars"), m.get("forks"), m.get("open_issues"),
                m.get("created_at"), m.get("pushed_at"),
            ))
        conn.executemany(
            "INSERT INTO repos (full_name, description, language, topics, homepage,"
            " license, default_branch, fork, archived, stars, forks, open_issues,"
            " created_at, pushed_at, verified, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'api')"
            " ON CONFLICT(full_name) DO UPDATE SET"
            " description=excluded.description, language=excluded.language, topics=excluded.topics,"
            " homepage=excluded.homepage, license=excluded.license,"
            " default_branch=excluded.default_branch, fork=excluded.fork,"
            " archived=excluded.archived, stars=excluded.stars, forks=excluded.forks,"
            " open_issues=excluded.open_issues, created_at=excluded.created_at,"
            " pushed_at=excluded.pushed_at, verified=1, source='api'",
            records,
        )
        conn.commit()

    # 2) 趋势:GH Archive 重建榜(文件内已按日期+名次排序)
    arch = RAW_DIR / "trends_gharchive.csv"
    if arch.exists():
        by_date = defaultdict(list)
        with arch.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
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

    # 2b) 趋势:每日真实榜单 JSONL(canonical 快照的兼容导出)
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
        records = []
        for line in profiles_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            p = json.loads(line)
            records.append((p["full_name"], p.get("one_liner"), p.get("purpose"),
                            p.get("boundaries"), p.get("tech_highlights"), p.get("maturity"),
                            p.get("model"), p.get("source"), p.get("generated_at")))
        conn.executemany("INSERT OR REPLACE INTO profiles VALUES (?,?,?,?,?,?,?,?,?)", records)
        conn.execute(
            "UPDATE repos SET profile_status='done' WHERE full_name IN (SELECT full_name FROM profiles)")
        conn.commit()

    # 4) 推送日志
    push_file = DAILY_DIR / "push_log.jsonl"
    if push_file.exists():
        records = []
        for line in push_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            records.append((r["date"], r["list_type"], r["full_name"], r.get("pushed_at")))
        conn.executemany("INSERT OR REPLACE INTO push_log VALUES (?,?,?,?)", records)
        conn.commit()


def _validate_db(conn: sqlite3.Connection, had_sources: bool):
    """重建校验:任一失败抛错,正式库不被触碰。"""
    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise RuntimeError("重建校验失败: integrity_check 不通过")
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    required = {"repos", "trend_daily", "profiles", "push_log", "search_fts"}
    missing = required - tables
    if missing:
        raise RuntimeError(f"重建校验失败: 缺少表 {sorted(missing)}")
    if had_sources:
        n_repos = conn.execute("SELECT count(*) FROM repos").fetchone()[0]
        n_fts = conn.execute("SELECT count(*) FROM search_fts").fetchone()[0]
        if n_repos == 0:
            raise RuntimeError("重建校验失败: source 文件存在但 repos 为空")
        if n_fts != n_repos:
            raise RuntimeError(f"重建校验失败: FTS 行数 {n_fts} != repos 行数 {n_repos}")


def rebuild(db_path=None, close: sqlite3.Connection | None = None) -> sqlite3.Connection:
    """从 raw/daily/profiles 文件全量重建数据库(原子、幂等)。

    流程:同目录临时库构建 → 完整性/FTS 校验 → os.replace 正式库。
    失败时删除临时库并抛错,正式库保持不变、可继续服务。
    """
    target = Path(db_path or DB_PATH)
    if close is not None:
        close.close()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.rebuild-{os.getpid()}.tmp")
    if tmp.exists():
        tmp.unlink()
    conn = None
    try:
        conn = connect(tmp)
        conn.executescript(SCHEMA)
        had_sources = any(
            p.exists() for p in [
                RAW_DIR / "repo_meta_snapshot.csv", RAW_DIR / "repo_meta_api.jsonl",
                RAW_DIR / "trends_gharchive.csv", DAILY_DIR / "trends.jsonl",
                PROFILE_DIR / "profiles.jsonl"])
        _import_sources(conn)
        refresh_repo_stats(conn)
        reindex_fts(conn)
        _validate_db(conn, had_sources)
        conn.close()
        conn = None
        replace_file_with_retry(tmp, target)
    except BaseException:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise
    return connect(target)


REPO_COLS = frozenset({
    "full_name", "description", "language", "topics", "homepage", "license",
    "default_branch", "fork", "archived", "stars", "forks", "open_issues",
    "created_at", "pushed_at", "verified", "source",
    "first_trend_date", "last_trend_date", "trend_days", "core_days",
    "best_rank", "best_daily_stars", "profile_status",
})


def _normalize_topics(raw) -> str:
    """快照 CSV 的 topics 可能是 ['a','b'] 单引号数组或纯串,统一成 JSON 数组。"""
    if isinstance(raw, list):
        return json.dumps(raw, ensure_ascii=False)
    if not raw:
        return "[]"
    try:
        v = json.loads(raw)
        return json.dumps(v, ensure_ascii=False) if isinstance(v, list) else "[]"
    except (ValueError, TypeError):
        pass
    try:
        import ast
        v = ast.literal_eval(raw)
        return json.dumps(list(v), ensure_ascii=False) if isinstance(v, (list, tuple)) else "[]"
    except (ValueError, SyntaxError):
        return "[]"


def upsert_repo(conn: sqlite3.Connection, m: dict, update_existing: bool = False):
    """增量写入单条仓库(每日任务用;全量重建走批量路径)。"""
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
