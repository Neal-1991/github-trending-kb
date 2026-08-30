"""飞书云文档生成:每日/周报以真实 docx 文档推送,含完整项目画像。

流程: create_doc → add_blocks(批量) → grant_access(授权给用户) → 推送链接卡片。
依赖应用权限: docx:document(创建/编辑)、drive:permission(添加协作者)。
"""
import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_OPEN_ID
from scripts.feishu import _tenant_access_token

API = "https://open.feishu.cn/open-apis"


class DocScopeError(RuntimeError):
    """应用缺少云文档相关权限。"""


def create_doc(title: str) -> dict:
    r = requests.post(
        f"{API}/docx/v1/documents",
        json={"folder_token": ""}, timeout=30,
        headers={"Authorization": f"Bearer {_tenant_access_token()}"},
    )
    data = r.json()
    if data.get("code") != 0:
        raise DocScopeError(f"创建文档失败({data.get('code')}): {data.get('msg')}")
    doc = data["data"]["document"]
    return {"document_id": doc["document_id"],
            "url": f"https://feishu.cn/docx/{doc['document_id']}"}


def _run(content: str, *, bold: bool = False, link: str = "") -> dict:
    style = {}
    if bold:
        style["bold"] = True
    if link:
        style["link"] = {"url": link}
    el = {"text_run": {"content": content}}
    if style:
        el["text_run"]["text_element_style"] = style
    return el


def _block(block_type: int, key: str, elements: list) -> dict:
    return {"block_type": block_type, key: {"elements": elements, "style": {}}}


def heading(level: int, content: str) -> dict:
    return _block(2 + level, f"heading{level}", [_run(content)])


def text_block(runs: list) -> dict:
    return _block(2, "text", runs)


def bullet(runs: list) -> dict:
    return _block(12, "bullet", runs)


def add_blocks(doc_id: str, blocks: list, batch: int = 45) -> None:
    headers = {"Authorization": f"Bearer {_tenant_access_token()}"}
    for i in range(0, len(blocks), batch):
        chunk = blocks[i:i + batch]
        r = requests.post(
            f"{API}/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
            params={"document_revision_id": -1},
            json={"index": -1, "children": chunk},
            timeout=30, headers=headers,
        )
        data = r.json()
        if data.get("code") != 0:
            raise DocScopeError(f"写入文档块失败({data.get('code')}): {data.get('msg')}")
        time.sleep(0.3)


def grant_access(doc_id: str, open_id: str) -> None:
    r = requests.post(
        f"{API}/drive/v1/permissions/{doc_id}/members",
        params={"type": "docx", "need_notification": "true"},
        json={"member_type": "openid", "member_id": open_id, "perm": "full_access"},
        timeout=30,
        headers={"Authorization": f"Bearer {_tenant_access_token()}"},
    )
    data = r.json()
    if data.get("code") != 0:
        raise DocScopeError(f"文档授权失败({data.get('code')}): {data.get('msg')}")


# ---------- 内容构建 ----------

LIST_TITLES = {
    "total": "🏆 总榜",
    "arch:total": "🏆 总榜(GH Archive 重建)",
    "lang:python": "🐍 Python 榜",
    "lang:typescript": "🟦 TypeScript 榜",
    "lang:javascript": "🟨 JavaScript 榜",
    "lang:rust": "🦀 Rust 榜",
}

PROFILE_FIELDS = [
    ("purpose", "用途与解决的问题"),
    ("boundaries", "边界与不适用场景"),
    ("tech_highlights", "技术栈与实现亮点"),
    ("maturity", "成熟度与 License"),
]


def build_daily_blocks(date_str: str, records: list[dict], profiles: dict, conn) -> list[dict]:
    """records: daily_job 抓取的榜单结构;profiles: {full_name: profile dict}。"""
    blocks = [heading(1, f"GitHub 趋势日报 · {date_str}")]
    entries_all = []
    for rec in records:
        for e in rec["entries"]:
            entries_all.append({**e, "list_type": rec["list_type"]})

    # 一、今日速览
    blocks.append(heading(2, "一、今日速览"))
    for rec in records:
        if not rec["entries"]:
            continue
        blocks.append(heading(3, LIST_TITLES.get(rec["list_type"], rec["list_type"])
                              + f" Top {len(rec['entries'])}"))
        for e in rec["entries"]:
            runs = [_run(f"{e['rank']}. ", bold=True),
                    _run(e["repo"], link=f"https://github.com/{e['repo']}")]
            if e.get("stars_today"):
                runs.append(_run(f"  +{e['stars_today']}⭐", bold=True))
            if e.get("is_new"):
                runs.append(_run("  🆕 首次上榜", bold=True))
            note = profiles.get(e["repo"], {}).get("one_liner") or e.get("description") or ""
            if note:
                runs.append(_run(f"  — {note[:60]}"))
            blocks.append(bullet(runs))

    # 二、重点项目画像(今日出现且有画像的,跨榜去重)
    seen = {}
    for e in entries_all:
        if e["repo"] in profiles and e["repo"] not in seen:
            seen[e["repo"]] = e
    blocks.append(heading(2, f"二、重点项目画像({len(seen)} 个)"))
    if not seen:
        blocks.append(text_block([_run("今日上榜项目暂无画像。")]))
    meta_by_name = {r["full_name"]: dict(r) for r in
                    conn.execute("SELECT full_name, language, license FROM repos")}
    for i, (name, e) in enumerate(sorted(seen.items(),
                                         key=lambda kv: (kv[1]["list_type"] != "total",
                                                         kv[1]["rank"])), 1):
        p = profiles[name]
        meta = meta_by_name.get(name, {})
        head_runs = [_run(f"{i}. ", bold=True),
                     _run(name, bold=True, link=f"https://github.com/{name}")]
        tags = []
        if e.get("stars_today"):
            tags.append(f"+{e['stars_today']}⭐今日")
        if meta.get("language"):
            tags.append(meta["language"])
        if meta.get("license"):
            tags.append(f"License: {meta['license']}")
        if tags:
            head_runs.append(_run("  | " + " | ".join(tags)))
        if tags:
            head_runs.append(_run("  | " + " | ".join(tags)))
        blocks.append(_block(5, "heading3", head_runs))
        for field, label in PROFILE_FIELDS:
            val = (p.get(field) or "").strip()
            if val:
                blocks.append(bullet([_run(f"{label}:", bold=True), _run(f" {val}")]))
        blocks.append(text_block([_run(" ")]))

    # 三、今日新面孔
    new_faces = conn.execute(
        "SELECT full_name, language, best_daily_stars FROM repos "
        "WHERE first_trend_date = ? ORDER BY best_daily_stars DESC", (date_str,)).fetchall()
    blocks.append(heading(2, f"三、今日新面孔({len(new_faces)} 个,首次上榜)"))
    if not new_faces:
        blocks.append(text_block([_run("今日无首次上榜项目。")]))
    for r in new_faces:
        runs = [_run(r["full_name"], link=f"https://github.com/{r['full_name']}")]
        if r["language"]:
            runs.append(_run(f"  [{r['language']}]"))
        if r["best_daily_stars"]:
            runs.append(_run(f"  单日峰值 {r['best_daily_stars']}⭐"))
        one = profiles.get(r["full_name"], {}).get("one_liner")
        if one:
            runs.append(_run(f"  — {one}"))
        blocks.append(bullet(runs))

    blocks.append(text_block([_run(f"由 GitHub 趋势榜知识库自动生成 · {date_str}",
                                   bold=False)]))
    return blocks


def build_weekly_blocks(date_str: str, summary: dict, profiles: dict) -> list[dict]:
    blocks = [heading(1, f"GitHub 趋势周报 · {date_str}")]
    blocks.append(heading(2, f"本周({summary['week']})首次上榜新星 Top 10"))
    for i, (repo, stars) in enumerate(summary["top_new"], 1):
        runs = [_run(f"{i}. ", bold=True),
                _run(repo, link=f"https://github.com/{repo}"),
                _run(f"  周新增 {stars}⭐", bold=True)]
        one = profiles.get(repo, {}).get("one_liner")
        if one:
            runs.append(_run(f"  — {one}"))
        blocks.append(bullet(runs))
    if not summary["top_new"]:
        blocks.append(text_block([_run("本周无新上榜项目。")]))
    blocks.append(heading(2, "本周概况"))
    blocks.append(bullet([_run(f"首次上榜项目: {summary['new_repos']} 个", bold=True)]))
    blocks.append(bullet([_run(f"生成画像: {summary['profiled']} 篇", bold=True)]))
    return blocks


def generate_doc(title: str, blocks: list[dict], open_id: str = "") -> str:
    """创建文档→写入→授权,返回文档 URL。"""
    doc = create_doc(title)
    add_blocks(doc["document_id"], blocks)
    if open_id:
        grant_access(doc["document_id"], open_id)
    return doc["url"]
