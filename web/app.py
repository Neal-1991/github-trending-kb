"""GitHub 趋势榜知识库 · 本地 Web 检索系统。

SQLite FTS5 全文检索,不依赖任何 LLM。
启动: uvicorn web.app:app --port 8000  →  http://127.0.0.1:8000

检索契约(review P1-06/P1-07):
- q 最长 200 字符,去重后最多 12 个词;含 NUL/控制字符 → 422;
- FTS(trigram,≥3字符词)与 LIKE 回退(<3字符词)覆盖同一组文本列;
- LIKE 转义 % _ 与转义符;单字符只做仓库名前缀/语言精确匹配;
- 返回真实总数与分页(默认 30,最大 100),排序模式如实标注;
- (list_type, date) 无数据时回退该榜最新日期并提示;
- 数据库只读连接(request 级),缺库明确失败,不创建空库;
- 动态 SVG 文本全部转义;homepage 仅允许 http/https。
"""
import html
import json
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import ARCH_DAILY_STAR_ANOMALY, ROOT
from scripts.db import connect_ro

app = FastAPI(title="GitHub 趋势榜知识库", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "web/static"), name="static")
templates = Jinja2Templates(directory=str(ROOT / "web/templates"))

SEARCHABLE_LIKE_COLS = [
    "r.full_name", "r.description", "r.topics", "r.language",
    "p.one_liner", "p.purpose", "p.boundaries", "p.tech_highlights", "p.maturity",
]


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'")
    return resp


def get_db():
    conn = connect_ro()
    try:
        yield conn
    finally:
        conn.close()


def safe_homepage(url: str | None) -> str | None:
    """仅允许标准化后的 http/链接;javascript: 等一律丢弃。"""
    if not url:
        return None
    url = url.strip()
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return url
    return None


# ---------- 检索解析 ----------

class ParsedQuery:
    def __init__(self, mode: str, terms: list[str]):
        self.mode = mode          # all / single / like / fts
        self.terms = terms


def parse_query(q: str) -> ParsedQuery:
    for ch in q:
        if ord(ch) < 32 and ch not in ("\t", "\n", "\r"):
            raise HTTPException(status_code=422, detail="查询包含不允许的控制字符")
    q = q.replace('"', " ").strip()
    if not q:
        return ParsedQuery("all", [])
    terms = []
    for t in q.split():
        if t not in terms:
            terms.append(t)
    terms = terms[:12]
    if len(terms) == 1 and len(terms[0]) == 1:
        return ParsedQuery("single", terms)
    if any(len(t) < 3 for t in terms):
        return ParsedQuery("like", terms)
    return ParsedQuery("fts", terms)


def _like_escape(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_clause(term: str) -> tuple[str, list]:
    like = f"%{_like_escape(term)}%"
    conds, params = [], []
    for col in SEARCHABLE_LIKE_COLS:
        conds.append(f"{col} LIKE ? ESCAPE '\\'")
        params.append(like)
    return "(" + " OR ".join(conds) + ")", params


SORT_LABELS = {
    "fts": "相关度排序(bm25)",
    "like": "按上榜热度排序(含短词模糊匹配)",
    "single": "前缀匹配 · 按上榜热度排序",
    "all": "按上榜热度排序",
}


# ---------- 页面 ----------

@app.get("/", response_class=HTMLResponse)
def index(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    stats = conn.execute("""
      SELECT
        (SELECT count(*) FROM repos WHERE verified=1) AS repos_meta,
        (SELECT count(*) FROM repos) AS repos_all,
        (SELECT count(*) FROM profiles) AS profiles,
        (SELECT count(DISTINCT date) FROM trend_daily) AS days,
        (SELECT MIN(date) FROM trend_daily) AS date_from,
        (SELECT MAX(date) FROM trend_daily) AS date_to,
        (SELECT count(*) FROM repos WHERE first_trend_date >= date('now','-7 day')) AS new_week
    """).fetchone()
    latest_real = conn.execute("""
      SELECT date, list_type, rank, full_name, stars FROM trend_daily
      WHERE list_type='total' AND date=(SELECT MAX(date) FROM trend_daily WHERE list_type='total')
      ORDER BY rank LIMIT 10
    """).fetchall()
    latest_real_date = latest_real[0]["date"] if latest_real else None
    new_faces = conn.execute("""
      SELECT full_name, first_trend_date, language, best_daily_stars, description
      FROM repos WHERE first_trend_date IS NOT NULL
      ORDER BY first_trend_date DESC, best_daily_stars DESC LIMIT 12
    """).fetchall()
    return templates.TemplateResponse(request, "index.html", {
        "stats": stats, "latest_real": latest_real, "latest_real_date": latest_real_date,
        "new_faces": new_faces,
    })


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, conn: sqlite3.Connection = Depends(get_db),
           q: str = Query("", max_length=200),
           lang: str = "", has_profile: str = "",
           page: int = Query(1, ge=1), per_page: int = Query(30, ge=1, le=100)):
    pq = parse_query(q)
    filters, filter_params = [], []
    if lang:
        filters.append("r.language = ?")
        filter_params.append(lang)
    if has_profile:
        filters.append("p.full_name IS NOT NULL")

    match_conds: list[str] = []
    match_params: list = []
    from_clause = "repos r LEFT JOIN profiles p ON p.full_name = r.full_name"
    select_cols = ("r.full_name, r.description, r.language, r.stars, r.core_days,"
                   " r.first_trend_date, r.verified, p.one_liner")
    order = "r.core_days DESC, r.full_name"

    if pq.mode == "fts":
        from_clause = ("search_fts f JOIN repos r ON r.full_name = f.full_name "
                       "LEFT JOIN profiles p ON p.full_name = r.full_name")
        select_cols += ", bm25(search_fts) AS score"
        match_conds.append("search_fts MATCH ?")
        match_params = [" AND ".join(f'"{t}"' for t in pq.terms)]
        order = "score"
    elif pq.mode == "single":
        t = pq.terms[0]
        match_conds.append("(r.full_name LIKE ? ESCAPE '\\' OR r.language = ?)")
        match_params += [f"{_like_escape(t)}%", t]
    elif pq.mode == "like":
        for t in pq.terms:
            c, p = _like_clause(t)
            match_conds.append(c)
            match_params += p

    conds = match_conds + filters
    where_clause = (" WHERE " + " AND ".join(conds)) if conds else ""
    count = conn.execute(
        f"SELECT count(*) FROM {from_clause}{where_clause}",
        match_params + filter_params).fetchone()[0]

    sql = (f"SELECT {select_cols} FROM {from_clause}{where_clause} "
           f"ORDER BY {order} LIMIT ? OFFSET ?")
    rows = conn.execute(sql, match_params + filter_params + [per_page, (page - 1) * per_page]).fetchall()

    langs = conn.execute("""
      SELECT language, count(*) n FROM repos
      WHERE language IS NOT NULL GROUP BY language ORDER BY n DESC LIMIT 15
    """).fetchall()
    pages = max(1, -(-count // per_page))
    return templates.TemplateResponse(request, "search.html", {
        "q": q, "rows": rows, "langs": langs, "sel_lang": lang,
        "has_profile": has_profile, "total": count, "page": page,
        "pages": pages, "per_page": per_page, "mode": pq.mode,
        "sort_label": SORT_LABELS[pq.mode],
    })


@app.get("/repo/{full_name:path}", response_class=HTMLResponse)
def repo_detail(request: Request, full_name: str,
                conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("""
      SELECT r.*, p.one_liner, p.purpose, p.boundaries, p.tech_highlights,
             p.maturity, p.model AS profile_model, p.source AS profile_source
      FROM repos r LEFT JOIN profiles p ON p.full_name = r.full_name
      WHERE r.full_name = ?
    """, (full_name,)).fetchall()
    if not row:
        return templates.TemplateResponse(request, "missing.html",
                                          {"full_name": full_name}, status_code=404)
    repo = dict(row[0])
    trend = conn.execute("""
      SELECT date, stars FROM trend_daily
      WHERE full_name = ? AND list_type='arch:total' AND rank <= 10
        AND (quality='full' OR quality='partial')
      ORDER BY date
    """, (full_name,)).fetchall()
    all_trend = conn.execute("""
      SELECT date, stars FROM trend_daily
      WHERE full_name = ? AND list_type='arch:total'
      ORDER BY date
    """, (full_name,)).fetchall()
    spark = sparkline([r["stars"] for r in trend])
    topics = []
    try:
        topics = json.loads(repo["topics"] or "[]")
    except (ValueError, TypeError):
        pass
    repo["homepage"] = safe_homepage(repo.get("homepage"))
    return templates.TemplateResponse(request, "repo.html", {
        "r": repo, "trend": trend, "all_count": len(all_trend), "spark": spark,
        "topics": topics,
    })


@app.get("/browse", response_class=HTMLResponse)
def browse(request: Request, conn: sqlite3.Connection = Depends(get_db),
           d: str = "", list_type: str = "arch:total", month: str = ""):
    lists = conn.execute(
        "SELECT DISTINCT list_type FROM trend_daily ORDER BY list_type").fetchall()
    valid_types = {r["list_type"] for r in lists}
    fallback_note = False
    if list_type not in valid_types:
        list_type = "arch:total" if "arch:total" in valid_types else (
            sorted(valid_types)[0] if valid_types else list_type)
        fallback_note = True

    def date_exists(date_str: str) -> bool:
        return bool(conn.execute(
            "SELECT 1 FROM trend_daily WHERE date=? AND list_type=? LIMIT 1",
            (date_str, list_type)).fetchone())

    max_date = conn.execute(
        "SELECT MAX(date) m FROM trend_daily WHERE list_type=?", (list_type,)).fetchone()["m"]
    if not d or not date_exists(d):
        d, fallback_note = (max_date or d), True

    months = conn.execute("""
      SELECT DISTINCT substr(date,1,7) m FROM trend_daily WHERE list_type=?
      ORDER BY m DESC
    """, (list_type,)).fetchall()
    valid_months = {r["m"] for r in months}
    month_filter = month if month in valid_months else d[:7]
    if not date_exists(d):  # 月份切换后原日期不在该月,取该月最新一天
        latest_in_month = conn.execute("""
          SELECT MAX(date) m FROM trend_daily WHERE list_type=? AND substr(date,1,7)=?
        """, (list_type, month_filter)).fetchone()["m"]
        if latest_in_month:
            d = latest_in_month
    dates = conn.execute("""
      SELECT DISTINCT date FROM trend_daily
      WHERE list_type=? AND substr(date,1,7)=? ORDER BY date DESC
    """, (list_type, month_filter)).fetchall()
    rows = conn.execute("""
      SELECT rank, full_name, stars, quality FROM trend_daily
      WHERE date = ? AND list_type = ? ORDER BY rank
    """, (d, list_type)).fetchall()
    return templates.TemplateResponse(request, "browse.html", {
        "d": d, "list_type": list_type, "rows": rows, "dates": dates, "lists": lists,
        "months": months, "month": month_filter, "fallback_note": fallback_note,
    })


@app.get("/trends", response_class=HTMLResponse)
def trends(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    lang_by_quarter = conn.execute("""
      SELECT substr(r.date,1,4) || 'Q' || ((CAST(substr(r.date,6,2) AS INTEGER)-1)/3+1) AS q,
             COALESCE(NULLIF(t2.language,''),'其他/未知') AS lang, count(*) n
      FROM trend_daily r LEFT JOIN repos t2 ON t2.full_name = r.full_name
      WHERE r.list_type='arch:total' AND r.rank <= 10
        AND (r.quality='full' OR r.quality='partial')
      GROUP BY q, lang ORDER BY q, n DESC
    """).fetchall()
    quarters = sorted({r["q"] for r in lang_by_quarter})
    top_langs = {}
    for r in lang_by_quarter:
        top_langs[r["lang"]] = top_langs.get(r["lang"], 0) + r["n"]
    top_lang_names = [lang for lang, _ in sorted(top_langs.items(), key=lambda x: -x[1])[:8]]
    series = {lang: [] for lang in top_lang_names + ["其他"]}
    by_q = {}
    for r in lang_by_quarter:
        by_q.setdefault(r["q"], {})[r["lang"]] = r["n"]
    for qt in quarters:
        d = by_q.get(qt, {})
        for lang in top_lang_names:
            series[lang].append(d.get(lang, 0))
        series["其他"].append(sum(v for k, v in d.items() if k not in top_lang_names))
    stacked = stacked_bars(quarters, series)

    persistent = conn.execute("""
      SELECT full_name, core_days, trend_days, best_rank, first_trend_date, language
      FROM repos WHERE core_days >= 10 ORDER BY core_days DESC LIMIT 15
    """).fetchall()
    # 疑似刷星(单日星标 >= ARCH_DAILY_STAR_ANOMALY)默认不作为爆发事件展示
    spikes = conn.execute("""
      SELECT date, full_name, stars FROM trend_daily
      WHERE list_type='arch:total' AND rank<=10 AND quality='full'
        AND stars < ?
      ORDER BY stars DESC LIMIT 12
    """, (ARCH_DAILY_STAR_ANOMALY,)).fetchall()
    quality = conn.execute("""
      SELECT substr(date,1,7) m, quality, count(DISTINCT date) days FROM trend_daily
      WHERE list_type='arch:total' GROUP BY m, quality ORDER BY m
    """).fetchall()
    return templates.TemplateResponse(request, "trends.html", {
        "quarters": quarters, "series": series, "stacked": stacked,
        "persistent": persistent, "spikes": spikes, "quality": quality,
    })


@app.get("/healthz")
def healthz():
    return JSONResponse({"status": "ok"})


@app.get("/readyz")
def readyz(conn: sqlite3.Connection = Depends(get_db)):
    try:
        n = conn.execute("SELECT count(*) FROM repos").fetchone()[0]
        conn.execute("SELECT count(*) FROM search_fts").fetchone()
    except sqlite3.Error as e:
        return JSONResponse({"status": "unavailable", "error": str(e)}, status_code=503)
    return JSONResponse({"status": "ready", "repos": n})


# ---------- SVG(动态文本一律转义) ----------

PALETTE = ["#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
           "#edc948", "#b07aa1", "#ff9da7", "#9c755f"]


def sparkline(points: list, w: int = 640, h: int = 90) -> str:
    if not points:
        return ""
    mx = max(points) or 1
    step = w / max(len(points) - 1, 1)
    coords = " ".join(f"{i * step:.1f},{h - 6 - (p / mx) * (h - 14):.1f}"
                      for i, p in enumerate(points))
    area = f"M0,{h} L" + coords.replace(" ", " L") + f" L{w},{h} Z"
    return (f'<svg viewBox="0 0 {w} {h}" class="spark" role="img" '
            f'aria-label="历史单日星标曲线">'
            f'<path d="{area}" fill="rgba(78,121,169,.15)"/>'
            f'<polyline points="{coords}" fill="none" stroke="#4e79a7" stroke-width="2"/></svg>')


def stacked_bars(quarters: list, series: dict, w: int = 900, h: int = 260) -> str:
    """季度堆叠柱:每季度一根柱,按语言堆叠。语言名等动态文本全部 html 转义。"""
    totals = [max(sum(series[lang][i] for lang in series), 1) for i in range(len(quarters))]
    bar_w = w / max(len(quarters), 1) * 0.72
    gap = w / max(len(quarters), 1)
    out = [f'<svg viewBox="0 0 {w} {h + 24}" class="stacked" role="img" '
           f'aria-label="各季度 Top10 项目语言构成堆叠图">']
    for li, lang in enumerate(series):
        color = PALETTE[li % len(PALETTE)]
        safe_lang = html.escape(str(lang))
        acc = 0
        for i, qt in enumerate(quarters):
            v = series[lang][i]
            if not v:
                continue
            th = totals[i]
            bh = v / th * h
            y = h - acc / th * h - bh
            out.append(f'<rect x="{i * gap + (gap - bar_w) / 2:.1f}" y="{y:.1f}" '
                       f'width="{bar_w:.1f}" height="{bh:.1f}" fill="{color}"><title>'
                       f"{html.escape(str(qt))} {safe_lang}: {v}</title></rect>")
            acc += v
    for i, qt in enumerate(quarters):
        out.append(f'<text x="{i * gap + gap / 2:.1f}" y="{h + 16}" text-anchor="middle" '
                   f'class="axis">{html.escape(str(qt))[2:]}</text>')
    out.append("</svg>")
    legend = "".join(
        f'<span class="legend-item"><i style="background:{PALETTE[i % len(PALETTE)]}"></i>'
        f'{html.escape(str(lang))}</span>'
        for i, lang in enumerate(series))
    return "".join(out) + f'<div class="legend">{legend}</div>'
