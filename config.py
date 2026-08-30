"""全局配置:路径、榜单定义、密钥统一从 .env 读取。"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DAILY_DIR = DATA_DIR / "daily"
PROFILE_DIR = DATA_DIR / "profiles"
README_DIR = DATA_DIR / "readmes"
DB_PATH = DATA_DIR / "trending.db"

# 密钥(均可为空:缺失时对应功能降级并给出明确提示)
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GLM_API_KEY = os.getenv("GLM_API_KEY", "")
GLM_MODEL = os.getenv("GLM_MODEL", "glm-4.5-flash")
FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", "")
# 飞书自建应用(可发私聊;与群 webhook 二选一,webhook 优先)
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_OPEN_ID = os.getenv("FEISHU_OPEN_ID", "")   # ou_ 开头,发个人
FEISHU_CHAT_ID = os.getenv("FEISHU_CHAT_ID", "")   # oc_ 开头,发群

# 语言分榜(GitHub trending 语言名,小写)
LANG_LISTS = ["python", "typescript", "javascript", "rust"]

# ClickHouse 公共 playground(免费,只读)
CH_PLAYGROUND_URL = "https://play.clickhouse.com/?user=explorer"

# GH Archive 重建区间与质量分层(月度密度为 2026-08-30 实测,单位:星标事件/日)
ARCH_START = "2021-09-01"   # 用户口径"近五年"起点
ARCH_END = "2026-02-01"     # 实际提取到 2026-01-31;之后数据崩坏,留缺口
ARCH_EXTRACT_TOP_N = 50     # 每日提取前50,知识库核心口径为前10
# >=150000: full | >=70000: partial(Top10仍可用) | <70000: degraded(不建榜)
MONTHLY_DENSITY = {
    202109: 142466, 202110: 150000, 202111: 150000, 202112: 150000,
    202201: 150000, 202202: 150000, 202203: 150000, 202204: 150000,
    202205: 150000, 202206: 150000, 202207: 150000, 202208: 150000,
    202209: 150000, 202210: 150000, 202211: 150000, 202212: 150000,
    202301: 150000, 202302: 150000, 202303: 307700, 202304: 250000,
    202305: 250000, 202306: 250000, 202307: 250000, 202308: 250000,
    202309: 250000, 202310: 250000, 202311: 250000, 202312: 250000,
    202401: 250000, 202402: 250000, 202403: 233573, 202404: 250000,
    202405: 250000, 202406: 250000, 202407: 250000, 202408: 250000,
    202409: 250000, 202410: 250000, 202411: 250000, 202412: 250000,
    202501: 250000, 202502: 250000, 202503: 231683, 202504: 202116,
    202505: 183139, 202506: 127159, 202507: 138436, 202508: 129595,
    202509: 121164, 202510: 79405,  202511: 86570,  202512: 91540,
    202601: 98568,
}


def month_quality(yyyymm: int) -> str:
    d = MONTHLY_DENSITY.get(yyyymm, 0)
    if d >= 150000:
        return "full"
    if d >= 70000:
        return "partial"
    return "degraded"
