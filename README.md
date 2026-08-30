# GitHub 趋势榜知识库

统计 GitHub 每日趋势榜,沉淀成一个可检索的知识库:历史五年重建榜 + 每日真实榜单增量采集 + AI 项目画像 + 飞书日报推送 + 本地 Web 全文检索。

## 能力一览

| 能力 | 状态 | 说明 |
|---|---|---|
| 五年历史重建榜 | ✅ 已完成 | GH Archive 星标事件重建每日 Top50(2021-09 ~ 2026-01),80,500 条记录 |
| 每日真实榜单 | ✅ 已接入 | 每天 08:00(北京时间)GitHub Actions 抓总榜 + Python/TypeScript/JavaScript/Rust 分榜 |
| AI 项目画像 | ✅ 26 篇起步 | 用途/解决的问题、边界与不适用、技术栈与亮点、成熟度与 License 四维画像 |
| 飞书推送 | ✅ 待配 webhook | 日报卡片(🆕 首次上榜标记)+ 周日周报 |
| 本地检索系统 | ✅ 已上线 | FastAPI + SQLite FTS5(trigram 分词,中文可用),搜索不依赖 LLM |

## 快速开始

```bash
pip install -r requirements.txt

# 1) 构建/重建知识库(幂等,秒级)
python scripts/build_db.py

# 2) 本地检索系统
uvicorn web.app:app --port 8000
# 打开 http://127.0.0.1:8000

# 3) 手动跑一次每日任务(不推送、不调 GLM)
python scripts/daily_job.py --dry-run
```

## 配置

复制 `.env.example` 为 `.env` 并填写:

| 变量 | 用途 | 缺失时的降级行为 |
|---|---|---|
| `GITHUB_TOKEN` | 元数据补全、README 抓取备援 | 历史仓库元数据不全,新项目画像质量下降 |
| `GLM_API_KEY` | 每日新项目的 AI 画像 | 推送无一句话点评,画像留空 |
| `GLM_MODEL` | 默认 `glm-4.5-flash`(免费档) | — |
| `FEISHU_WEBHOOK` | 飞书群机器人推送 | 日报写成 `data/daily/preview_*.md` 本地预览 |

## 目录结构

```
├── config.py                  # 全局配置(密钥走 .env)
├── scripts/
│   ├── extract_history.py     # ClickHouse playground → 五年星标 Top50
│   ├── dump_repo_meta.py      # repos 快照(2022-07)→ 仓库元数据
│   ├── enrich_github_api.py   # GitHub API 补全快照缺失仓库(需 PAT,可断点续跑)
│   ├── fetch_readmes.py       # raw HEAD 抓 README(免鉴权,幂等)
│   ├── build_db.py            # 全量重建 SQLite(含 FTS5 索引)
│   ├── fetch_trending.py      # 真实趋势榜解析(防御式)
│   ├── glm_client.py          # GLM 画像客户端(四维 JSON 输出)
│   ├── feishu.py              # 飞书卡片构建与推送
│   ├── daily_job.py           # 每日编排:抓榜→新面孔→画像→推送→落盘
│   └── db.py                  # SQLite schema / FTS / 重建逻辑(共享层)
├── data/
│   ├── raw/                   # 历史提取 CSV + 元数据(进 git)
│   ├── daily/                 # 每日趋势 JSONL + 推送日志(进 git)
│   ├── profiles/              # 画像 JSONL(进 git)
│   ├── readmes/               # README 缓存(gitignore)
│   └── trending.db            # SQLite 派生库(gitignore,随时可重建)
├── web/                       # FastAPI 检索系统(templates + static)
├── reports/                   # 分析报告(五年趋势分析报告.md)
└── .github/workflows/daily.yml
```

## 数据设计原则

- **CSV/JSONL 是 source of truth(进 git),SQLite 是派生索引(不进 git)**——Actions 每天跑完把增量 JSONL commit 回仓库,本地 `build_db.py` 一键重建,无状态漂移。
- **同一 (date, list_type) 幂等**:重复执行不会重复落盘、不会重复推送。
- **数据质量显式标注**:GH Archive 重建榜每月标 `full/partial/degraded`,真实榜单与重建榜用 `list_type` 区分(`total` vs `arch:total`)。

## 部署每日任务(GitHub Actions)

1. 新建 GitHub 仓库,推送本项目;
2. 仓库 Settings → Secrets 添加 `GLM_API_KEY`、`FEISHU_WEBHOOK`(可选 `GH_PAT`,有它元数据补全额度更高);
3. Actions 会每天 UTC 00:00(北京 08:00)自动运行并把数据 commit 回仓库。

## 已知限制

- **2026-02 ~ 2026-08-29 为历史数据缺口**(GH Archive 公共源断流),可选 BigQuery 付费补齐(约 $15-25,见报告第八节)。
- 重建榜 ≠ 真实历史榜单(星标增速代理),趋势分析可靠,精确排名不可引用。
- 2023 年后仓库约 85% 缺语言/描述字段,配置 PAT 运行 `enrich_github_api.py` 后自动改善。
- GitHub trending 页面无官方 API,解析器做了防御式处理,页面改版时需要跟进。
