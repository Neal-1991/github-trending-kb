"""飞书推送:群机器人 webhook 与自建应用(私聊/群聊)双通道。"""
import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_CHAT_ID, FEISHU_OPEN_ID, FEISHU_WEBHOOK

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


def _post_with_retry(url: str, *, attempts: int = 3, timeout: int = 30, **kw) -> requests.Response:
    """统一 POST:网络异常/429/5xx 指数退避重试;429 尊重 Retry-After。"""
    import time as _time
    delay = 2
    for attempt in range(1, attempts + 1):
        try:
            r = requests.post(url, timeout=timeout, **kw)
            if r.status_code == 429 or 500 <= r.status_code < 600:
                wait = int(r.headers.get("Retry-After") or 0) or delay
                print(f"  [feishu] HTTP {r.status_code}, retry in {wait}s (attempt {attempt})")
                _time.sleep(min(wait, 60))
                delay *= 2
                continue
            return r
        except requests.RequestException as e:
            if attempt == attempts:
                raise
            print(f"  [feishu] network error: {e}, retry in {delay}s (attempt {attempt})")
            _time.sleep(delay)
            delay *= 2
    raise requests.RequestException(f"unreachable after {attempts} attempts: {url}")


def _resp_json(r: requests.Response) -> dict:
    try:
        return r.json()
    except ValueError:
        return {"code": -1, "msg": f"non-JSON response: HTTP {r.status_code} {r.text[:200]}"}


def send(card: dict) -> tuple[bool, str, str | None]:
    """webhook 优先,未配置时走自建应用。返回 (ok, msg, message_id)。"""
    if FEISHU_WEBHOOK:
        r = _post_with_retry(FEISHU_WEBHOOK, json=card, timeout=30)
        resp = _resp_json(r)
        ok = (resp.get("StatusCode") == 0) or (resp.get("code") == 0)
        return ok, json.dumps(resp, ensure_ascii=False), None
    if FEISHU_APP_ID and FEISHU_APP_SECRET and (FEISHU_OPEN_ID or FEISHU_CHAT_ID):
        return send_via_app(card)
    return False, "FEISHU_WEBHOOK / FEISHU_APP_* 均未配置", None


_token_cache: dict = {}


def _tenant_access_token() -> str:
    tok = _token_cache.get("token")
    exp = _token_cache.get("expire", 0)
    if tok and time.time() < exp - 120:
        return tok
    r = _post_with_retry(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}, timeout=30)
    data = _resp_json(r)
    if data.get("code") != 0:
        raise RuntimeError(f"获取 tenant_access_token 失败: {data}")
    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expire"] = time.time() + data.get("expire", 3600)
    return data["tenant_access_token"]


def send_via_app(card: dict) -> tuple[bool, str, str | None]:
    """自建应用通道:im/v1/messages 发交互卡片(私聊 open_id 或群 chat_id)。"""
    receive_id = FEISHU_CHAT_ID or FEISHU_OPEN_ID
    id_type = "chat_id" if FEISHU_CHAT_ID else "open_id"
    payload = {
        "receive_id": receive_id,
        "msg_type": "interactive",
        "content": json.dumps(card["card"], ensure_ascii=False),
    }
    try:
        r = _post_with_retry(
            "https://open.feishu.cn/open-apis/im/v1/messages",
            params={"receive_id_type": id_type},
            json=payload, timeout=30,
            headers={"Authorization": f"Bearer {_tenant_access_token()}"},
        )
    except requests.RequestException as e:
        return False, f"network: {e}", None
    data = _resp_json(r)
    code = data.get("code")
    if code == 0:
        message_id = (data.get("data") or {}).get("message_id")
        return True, "sent via app", message_id
    if code in (99991663, 99991661, 99991668):  # token 类错误:清缓存便于重试
        _token_cache.clear()
    return False, f"{code}: {data.get('msg')} {json.dumps(data.get('error'), ensure_ascii=False)[:200]}", None


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
