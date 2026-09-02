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

# 抓取校验(所有榜单作为一批提交,任一校验失败不生成 canonical 快照)
TRENDING_MIN_ENTRIES = 10    # 单榜条数下限
TRENDING_MAX_ENTRIES = 40    # 单榜条数上限
STARS_TODAY_COVERAGE = 0.6   # stars_today > 0 的覆盖率阈值

# 历史重建榜单日星标异常阈值(疑似刷星;raw 保留,不参与 best_daily_stars
# 与 Web"现象级爆发"默认展示。规则版本 v1:纯阈值)
ARCH_DAILY_STAR_ANOMALY = 15000

# ClickHouse 公共 playground(免费,只读)
CH_PLAYGROUND_URL = "https://play.clickhouse.com/?user=explorer"

# GH Archive 重建区间与质量分层(月度密度为 playground github_events 实测,单位:星标事件/日)
ARCH_START = "2021-09-01"   # 用户口径"近五年"起点
ARCH_END = "2026-03-01"     # 实际提取到 2026-02-28;2026-03 起 WatchEvent 密度跌破 70k/日
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
    202601: 98568,  202602: 79409,
    # 2026-03 起上游 github_events 非推送事件坍缩(WatchEvent 65k/日 → 2k/日,
    # PR/Issue 事件同步崩塌),数据仍存在但密度不可信,按 degraded 不回填。
    # 实测记录(2026-09-02):202603=65394 202604=59537 202605=31818
    #                     202606=10358 202607=2577  202608=2231
}


def month_quality(yyyymm: int) -> str:
    d = MONTHLY_DENSITY.get(yyyymm, 0)
    if d >= 150000:
        return "full"
    if d >= 70000:
        return "partial"
    return "degraded"
