"""GitHub 趋势榜知识库 · 本地 Web 检索系统。

纯本地运行,SQLite FTS5 全文检索,不依赖任何 LLM。
启动: uvicorn web.app:app --port 8000  →  http://127.0.0.1:8000
"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import ROOT
from scripts.db import connect

app = FastAPI(title="GitHub 趋势榜知识库", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "web/static"), name="static")
templates = Jinja2Templates(directory=str(ROOT / "web/templates"))


def db():
    return connect()


def query(sql: str, params=()):
    conn = db()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ---------- 检索 ----------

def fts_expression(q: str) -> str:
    q = q.replace('"', " ").strip()
    terms = [t for t in q.split() if len(t) >= 2]
    if not terms and len(q.replace(" ", "")) >= 1:
        terms = [q.replace(" ", "")]
    return " AND ".join(f'"{t}"' for t in terms)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    stats = query("""
      SELECT
        (SELECT count(*) FROM repos WHERE verified=1) AS repos_meta,
        (SELECT count(*) FROM repos) AS repos_all,
        (SELECT count(*) FROM profiles) AS profiles,
        (SELECT count(DISTINCT date) FROM trend_daily) AS days,
        (SELECT MIN(date) FROM trend_daily) AS date_from,
        (SELECT MAX(date) FROM trend_daily) AS date_to,
        (SELECT count(*) FROM repos WHERE first_trend_date >= date('now','-7 day')) AS new_week
    """)[0]
    latest_real = query("""
      SELECT date, list_type, rank, full_name, stars FROM trend_daily
      WHERE list_type='total' AND date=(SELECT MAX(date) FROM trend_daily WHERE list_type='total')
      ORDER BY rank LIMIT 10
    """)
    latest_real_date = latest_real[0]["date"] if latest_real else None
    new_faces = query("""
      SELECT full_name, first_trend_date, language, best_daily_stars, description
      FROM repos WHERE first_trend_date IS NOT NULL
      ORDER BY first_trend_date DESC, best_daily_stars DESC LIMIT 12
    """)
    return templates.TemplateResponse(request, "index.html", {
        "stats": stats, "latest_real": latest_real, "latest_real_date": latest_real_date,
        "new_faces": new_faces,
    })


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = "", lang: str = "", has_profile: str = ""):
    rows = []
    expr = fts_expression(q) if q else ""
    if expr:
        sql = """
          SELECT r.full_name, r.description, r.language, r.stars, r.core_days,
                 r.first_trend_date, r.verified, p.one_liner,
                 bm25(search_fts) AS score
          FROM search_fts f
          JOIN repos r ON r.full_name = f.full_name
          LEFT JOIN profiles p ON p.full_name = r.full_name
          WHERE search_fts MATCH ?
        """
        params = [expr]
        if lang:
            sql += " AND r.language = ?"
            params.append(lang)
        if has_profile:
            sql += " AND p.full_name IS NOT NULL"
        sql += " ORDER BY score LIMIT 60"
        rows = query(sql, params)
    else:
        sql = """
          SELECT full_name, description, language, stars, core_days,
                 first_trend_date, verified, p.one_liner AS one_liner, core_days AS score
          FROM repos r LEFT JOIN profiles p USING (full_name)
          WHERE 1=1
        """
        params = []
        if lang:
            sql += " AND r.language = ?"
            params.append(lang)
        if has_profile:
            sql += " AND p.full_name IS NOT NULL"
        sql += " ORDER BY core_days DESC LIMIT 60"
        rows = query(sql, params)
    langs = query("""
      SELECT language, count(*) n FROM repos
      WHERE language IS NOT NULL GROUP BY language ORDER BY n DESC LIMIT 15
    """)
    return templates.TemplateResponse(request, "search.html", {
        "q": q, "rows": rows, "langs": langs, "sel_lang": lang,
        "has_profile": has_profile,
    })


@app.get("/repo/{full_name:path}", response_class=HTMLResponse)
def repo_detail(request: Request, full_name: str):
    row = query("""
      SELECT r.*, p.one_liner, p.purpose, p.boundaries, p.tech_highlights,
             p.maturity, p.model AS profile_model, p.source AS profile_source
      FROM repos r LEFT JOIN profiles p ON p.full_name = r.full_name
      WHERE r.full_name = ?
    """, (full_name,))
    if not row:
        return templates.TemplateResponse(request, "missing.html", {"full_name": full_name}, status_code=404)
    repo = row[0]
    trend = query("""
      SELECT date, stars FROM trend_daily
      WHERE full_name = ? AND list_type='arch:total' AND rank <= 10
      ORDER BY date
    """, (full_name,))
    all_trend = query("""
      SELECT date, stars FROM trend_daily
      WHERE full_name = ? AND list_type='arch:total'
      ORDER BY date
    """, (full_name,))
    spark = sparkline([r["stars"] for r in all_trend])
    topics = []
    try:
        topics = json.loads(repo["topics"] or "[]")
    except (ValueError, TypeError):
        pass
    return templates.TemplateResponse(request, "repo.html", {
        "r": repo, "trend": trend, "all_count": len(all_trend), "spark": spark,
        "topics": topics,
    })


@app.get("/browse", response_class=HTMLResponse)
def browse(request: Request, d: str = "", list_type: str = "arch:total"):
    max_date = query("SELECT MAX(date) m FROM trend_daily WHERE list_type=?", (list_type,))[0]["m"]
    d = d or max_date
    rows = query("""
      SELECT rank, full_name, stars, quality FROM trend_daily
      WHERE date = ? AND list_type = ? ORDER BY rank
    """, (d, list_type))
    dates = query("""
      SELECT DISTINCT date FROM trend_daily WHERE list_type=? ORDER BY date DESC LIMIT 120
    """, (list_type,))
    lists = query("SELECT DISTINCT list_type FROM trend_daily ORDER BY list_type")
    return templates.TemplateResponse(request, "browse.html", {
        "d": d, "list_type": list_type, "rows": rows, "dates": dates, "lists": lists,
    })


@app.get("/trends", response_class=HTMLResponse)
def trends(request: Request):
    # 语言份额按季度(重建榜 arch:total,rank<=10)
    lang_by_quarter = query("""
      SELECT substr(r.date,1,4) || 'Q' || ((CAST(substr(r.date,6,2) AS INTEGER)-1)/3+1) AS q,
             COALESCE(NULLIF(t2.language,''),'其他/未知') AS lang, count(*) n
      FROM trend_daily r LEFT JOIN repos t2 ON t2.full_name = r.full_name
      WHERE r.list_type='arch:total' AND r.rank <= 10
      GROUP BY q, lang ORDER BY q, n DESC
    """)
    quarters = sorted({r["q"] for r in lang_by_quarter})
    top_langs = {}
    for r in lang_by_quarter:
        top_langs[r["lang"]] = top_langs.get(r["lang"], 0) + r["n"]
    top_lang_names = [l for l, _ in sorted(top_langs.items(), key=lambda x: -x[1])[:8]]
    other = [l for l in top_langs if l not in top_lang_names]
    series = {l: [] for l in top_lang_names + ["其他"]}
    by_q = {}
    for r in lang_by_quarter:
        by_q.setdefault(r["q"], {})[r["lang"]] = r["n"]
    for qt in quarters:
        d = by_q.get(qt, {})
        for l in top_lang_names:
            series[l].append(d.get(l, 0))
        series["其他"].append(sum(v for k, v in d.items() if k not in top_lang_names))
    stacked = stacked_bars(quarters, series)

    persistent = query("""
      SELECT full_name, core_days, trend_days, best_rank, first_trend_date, language
      FROM repos WHERE core_days >= 10 ORDER BY core_days DESC LIMIT 15
    """)
    spikes = query("""
      SELECT date, full_name, stars FROM trend_daily
      WHERE list_type='arch:total' AND rank<=10 AND quality='full'
      ORDER BY stars DESC LIMIT 12
    """)
    quality = query("""
      SELECT substr(date,1,7) m, quality, count(DISTINCT date) days FROM trend_daily
      WHERE list_type='arch:total' GROUP BY m, quality ORDER BY m
    """)
    return templates.TemplateResponse(request, "trends.html", {
        "quarters": quarters, "series": series, "stacked": stacked,
        "persistent": persistent, "spikes": spikes, "quality": quality,
    })


# ---------- SVG ----------

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
    return (f'<svg viewBox="0 0 {w} {h}" class="spark">'
            f'<path d="{area}" fill="rgba(78,121,169,.15)"/>'
            f'<polyline points="{coords}" fill="none" stroke="#4e79a7" stroke-width="2"/></svg>')


def stacked_bars(quarters: list, series: dict, w: int = 900, h: int = 260) -> str:
    """季度堆叠柱:每季度一根柱,按语言堆叠。"""
    totals = [max(sum(series[l][i] for l in series), 1) for i in range(len(quarters))]
    bar_w = w / max(len(quarters), 1) * 0.72
    gap = w / max(len(quarters), 1)
    out = [f'<svg viewBox="0 0 {w} {h + 24}" class="stacked">']
    for li, lang in enumerate(series):
        color = PALETTE[li % len(PALETTE)]
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
                       f"{qt} {lang}: {v}</title></rect>")
            acc += v
    for i, qt in enumerate(quarters):
        out.append(f'<text x="{i * gap + gap / 2:.1f}" y="{h + 16}" text-anchor="middle" '
                   f'class="axis">{qt[2:]}</text>')
    out.append("</svg>")
    legend = "".join(
        f'<span class="legend-item"><i style="background:{PALETTE[i % len(PALETTE)]}"></i>{lang}</span>'
        for i, lang in enumerate(series))
    return "".join(out) + f'<div class="legend">{legend}</div>'
