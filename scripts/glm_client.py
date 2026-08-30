"""GLM API 客户端:读 README + 元数据 → 项目画像 JSON。

输出字段(与 profiles 表一致):
  one_liner / purpose / boundaries / tech_highlights / maturity
"""
import json
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import GLM_API_KEY, GLM_MODEL

API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

SYSTEM_PROMPT = """你是开源项目分析助手。根据给定的仓库元数据和 README 节选,输出严格的 JSON(不要 markdown 代码块,不要多余文字),字段如下:
{
  "one_liner": "一句话概括项目是什么,不超过40字",
  "purpose": "用途与解决的问题:2-3句,说清楚目标用户、核心场景、解决的痛点",
  "boundaries": "边界与不适用场景:1-2句,明确它不做什么、什么情况下不该用它、已知局限",
  "tech_highlights": "技术栈与实现亮点:1-2句,关键语言/框架/架构模式/值得借鉴的实现",
  "maturity": "成熟度与License:1句,包含许可证、项目阶段、维护活跃度判断"
}
全部用简体中文。信息不足时基于可靠推断,不要编造具体数字。"""


def profile_repo(full_name: str, meta: dict, readme: str, timeout: int = 90) -> dict | None:
    if not GLM_API_KEY:
        return None
    topics = meta.get("topics") or []
    user_content = (
        f"仓库: {full_name}\n"
        f"描述: {meta.get('description') or '无'}\n"
        f"主语言: {meta.get('language') or '未知'}\n"
        f"Topics: {', '.join(topics) if topics else '无'}\n"
        f"License: {meta.get('license') or '未知'}\n"
        f"Stars: {meta.get('stars', '?')} | 创建于: {meta.get('created_at', '?')}\n"
        f"--- README 节选 ---\n{(readme or '(未获取到)')[:6000]}"
    )
    payload = {
        "model": GLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.3,
        "max_tokens": 1024,
    }
    for attempt in range(1, 4):
        try:
            r = requests.post(
                API_URL, json=payload, timeout=timeout,
                headers={"Authorization": f"Bearer {GLM_API_KEY}"},
            )
            if r.status_code == 200:
                text = r.json()["choices"][0]["message"]["content"]
                return _parse_json(text)
            print(f"  [glm] {full_name} HTTP {r.status_code}: {r.text[:160]}, attempt {attempt}")
        except (requests.RequestException, KeyError, json.JSONDecodeError) as e:
            print(f"  [glm] {full_name} error: {e}, attempt {attempt}")
        time.sleep(4 * attempt)
    return None


def _parse_json(text: str) -> dict | None:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None
