"""飞书群机器人 webhook 推送:日报卡片 + 周报卡片。"""
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import FEISHU_WEBHOOK

LIST_TITLES = {
    "total": "🏆 总榜",
    "arch:total": "🏆 总榜(GH Archive 重建)",
    "lang:python": "🐍 Python 榜",
    "lang:typescript": "🟦 TypeScript 榜",
    "lang:javascript": "🟨 JavaScript 榜",
    "lang:rust": "🦀 Rust 榜",
}


def _line(entry: dict, one_liners: dict) -> str:
    repo = entry["repo"]
    mark = " 🆕" if entry.get("is_new") else ""
    stars = f" +{entry['stars_today']}⭐" if entry.get("stars_today") else ""
    note = one_liners.get(repo, "")
    note = f"\n{note}" if note else ""
    return f"**{entry['rank']}. [{repo}](https://github.com/{repo})**{mark}{stars}{note}"


def build_daily_card(date_str: str, records: list[dict], one_liners: dict) -> dict:
    elements = []
    for rec in records:
        entries = rec["entries"]
        if not entries:
            continue
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md",
                     "content": f"**{LIST_TITLES.get(rec['list_type'], rec['list_type'])} Top {len(entries)}**"},
        })
        lines = "\n".join(_line(e, one_liners) for e in entries)
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": lines}})
        elements.append({"tag": "hr"})
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text",
                      "content": "🆕 = 首次上榜 · 由 GitHub 趋势榜知识库自动生成"}],
    })
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"GitHub 趋势日报 · {date_str}"},
                "template": "blue",
            },
            "elements": elements,
        },
    }


def build_weekly_card(date_str: str, summary: dict) -> dict:
    """summary: {"week": "MM-DD ~ MM-DD", "new_repos": n, "top_new": [(repo, stars)], "profiled": n}"""
    lines = [f"**{i}. [{repo}](https://github.com/{repo})** +{stars}⭐ 🆕"
             for i, (repo, stars) in enumerate(summary["top_new"], 1)]
    elements = [
        {"tag": "div", "text": {"tag": "lark_md",
                                "content": f"**本周({summary['week']})首次上榜新星 Top 10**(按周新增星标)"}},
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines) or "本周无新上榜项目"}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md",
                                "content": f"本周首次上榜 **{summary['new_repos']}** 个项目,"
                                           f"生成画像 **{summary['profiled']}** 篇"}},
        {"tag": "note", "elements": [{"tag": "plain_text",
                                      "content": "由 GitHub 趋势榜知识库自动生成"}]},
    ]
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"GitHub 趋势周报 · {date_str}"},
                "template": "green",
            },
            "elements": elements,
        },
    }


def send(card: dict) -> tuple[bool, str]:
    if not FEISHU_WEBHOOK:
        return False, "FEISHU_WEBHOOK 未配置"
    r = requests.post(FEISHU_WEBHOOK, json=card, timeout=30)
    try:
        resp = r.json()
    except ValueError:
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    ok = (resp.get("StatusCode") == 0) or (resp.get("code") == 0)
    return ok, json.dumps(resp, ensure_ascii=False)


def card_to_markdown(card: dict) -> str:
    """无 webhook 时的本地预览。"""
    elements = card["card"]["elements"]
    out = [f"# {card['card']['header']['title']['content']}", ""]
    for el in elements:
        if el.get("tag") == "div":
            out.append(el["text"]["content"])
            out.append("")
        elif el.get("tag") == "hr":
            out.append("---")
            out.append("")
        elif el.get("tag") == "note":
            out.append("> " + el["elements"][0]["content"])
    return "\n".join(out)
