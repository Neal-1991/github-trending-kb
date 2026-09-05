# GitHub 趋势榜知识库

统计 GitHub 每日趋势榜,沉淀成一个可检索的知识库:历史五年重建榜 + 每日真实榜单增量采集 + AI 项目画像 + 飞书日报推送 + 本地 Web 全文检索。

## 能力一览

| 能力 | 状态 | 说明 |
|---|---|---|
| 五年历史重建榜 | ✅ 已完成 | GH Archive 星标事件重建每日 Top50(2021-09 ~ 2026-02),81,900 条记录 |
| 每日真实榜单 | ✅ 已接入 | 每天 08:00(北京时间)GitHub Actions 抓总榜 + Python/TypeScript/JavaScript/Rust 分榜 |
| AI 项目画像 | ✅ 持续生成 | 用途/解决的问题、边界与不适用、技术栈与亮点、成熟度与 License 四维画像(数量见首页统计) |
| 飞书推送 | ✅ 待配 webhook | 日报单条消息(文档链接卡片或摘要卡片)+ 周日周报 |
| 本地检索系统 | ✅ 已上线 | FastAPI + SQLite FTS5(trigram 分词,中文可用),搜索不依赖 LLM |

## 快速开始

```bash
pip install -r requirements.lock

# 1) 构建/重建知识库(原子:临时库校验通过才替换,约 3 秒)
python scripts/build_db.py

# 2) 本地检索系统
uvicorn web.app:app --port 8000
# 打开 http://127.0.0.1:8000(/healthz /readyz 为健康检查)

# 3) 预览每日任务(零副作用:不写数据、不调 GLM、不发消息)
python scripts/daily_job.py --dry-run

# 4) 运行测试与数据审计
pip install -r requirements.lock
python -m ruff check .
python -m pytest -p no:cacheprovider
python scripts/audit_data.py
```

## 每日任务模式

| 命令 | 行为 |
|---|---|
| `python scripts/daily_job.py` | 完整流程:捕获(或回放当日快照)→ 画像 → 通知 |
| `--dry-run` | 零副作用预览,预览文件写入系统临时目录 |
| `--capture-only` | 只捕获快照 + 画像 + 重建(CI Job A),不通知 |
| `--notify-only --date D` | 只回放 D 日 canonical 快照并发通知(CI Job B) |
| `--refresh-snapshot` | 仅允许刷新今天的快照,旧版自动归档到 `snapshots/history/` |
| `--date D` | 历史日期只回放 canonical/legacy,缺数据明确报错;拒绝未来日期 |

每日榜单以**不可变快照**落盘:`data/daily/snapshots/YYYY/MM/YYYY-MM-DD.json`(含内容寻址 `snapshot_id`)。当日快照已存在时默认回放,不重新抓取;`data/daily/trends.jsonl` 保留为兼容导出。通知(日报/周报/云文档链接)在 `data/daily/delivery_log.jsonl` 中按事件独立管理状态,语义为"至少一次 + 可检测重复";数据提交与通知分属两个 CI job,通知失败不影响数据落库。

今日损坏快照会先归档并重新抓取，通过校验后再重建数据库；历史损坏快照保持失败，不用今天的数据替代。日报与周报分别尝试，发送失败记录事件并以非零状态退出，Actions 可识别失败；归档日志仍参与去重。完整运行未配置飞书时生成本地预览，显式 `--notify-only` 未配置通道则报错。

画像积压保存在 `data/profiles/pending_queue.json`，每天最多处理 80 个：当天榜单优先，其余名额处理不再上榜的积压项目。临时失败次日重试，无 README 的仓库 30 天后复查；已完成画像从队列移除。队列跟随数据提交，不依赖 CI 中被清空的 README 缓存。

## 飞书推送模式

| 配置 | 行为 |
|---|---|
| 仅 `FEISHU_WEBHOOK` | 每日一条摘要卡片 |
| 仅自建应用(无云文档权限) | 每日一条摘要卡片(文档创建失败自动降级) |
| 自建应用 + 云文档权限 | 创建日报云文档,发一条链接卡片(文档含速览/四维画像/新面孔) |

## 配置

复制 `.env.example` 为 `.env` 并填写:

| 变量 | 用途 | 缺失时的降级行为 |
|---|---|---|
| `GITHUB_TOKEN` | 元数据补全、README 抓取备援 | 历史仓库元数据不全,新项目画像质量下降 |
| `GLM_API_KEY` | 每日新项目的 AI 画像 | 推送无一句话点评,画像留空 |
| `GLM_MODEL` | 默认 `glm-4.5-flash`(免费档) | — |
| `FEISHU_WEBHOOK` | 飞书群机器人推送 | 回退自建应用;都未配置则本地预览 |
| `FEISHU_APP_ID/SECRET/OPEN_ID/CHAT_ID` | 自建应用通道(可发云文档) | 缺云文档权限时自动降级为摘要卡片 |

## 目录结构

```
├── config.py                  # 全局配置(密钥走 .env)
├── scripts/
│   ├── atomic_io.py           # 原子文件写入(tempfile → fsync → os.replace)
│   ├── snapshot_store.py      # 每日榜单不可变快照(canonical + history)
│   ├── delivery_log.py        # 通知投递状态(append-only 事件)
│   ├── extract_history.py     # ClickHouse playground → 五年星标 Top50(原子导出)
│   ├── dump_repo_meta.py      # repos 快照(2022-07)→ 仓库元数据(原子导出)
│   ├── enrich_github_api.py   # GitHub API 补全/刷新(--refresh-stale,含 repo_id)
│   ├── fetch_readmes.py       # raw HEAD 抓 README(免鉴权,状态分类)
│   ├── build_db.py            # 全量重建 SQLite(原子替换,含 FTS5 索引)
│   ├── fetch_trending.py      # 真实趋势榜解析 + 批次校验
│   ├── glm_client.py          # GLM 画像客户端(schema 校验 + 重试)
│   ├── feishu.py / feishu_doc.py  # 飞书卡片/云文档(重试 + 降级)
│   ├── daily_job.py           # 每日编排:捕获→画像→通知(三阶段解耦)
│   ├── profile_queue.py       # 持久化跨日画像队列、重试与缺 README 复查
│   ├── audit_data.py          # 只读数据审计(JSONL/CSV/DB/身份/口径)
│   └── db.py                  # SQLite schema / FTS / 原子重建(共享层)
├── tests/                     # pytest 回归测试(离线,全 mock)
├── data/
│   ├── raw/                   # 历史提取 CSV + 元数据(进 git)
│   ├── daily/                 # 每日快照 + 兼容导出 + 投递日志(进 git)
│   ├── profiles/              # 画像 JSONL(进 git)
│   ├── readmes/               # README 缓存(gitignore)
│   └── trending.db            # SQLite 派生库(gitignore,随时可重建)
├── web/                       # FastAPI 检索系统(templates + static)
├── reports/                   # 分析报告与优化规格
└── .github/workflows/daily.yml    # capture-and-persist → notify 两阶段
```

## 数据设计原则

- **CSV/JSONL/快照是 source of truth(进 git),SQLite 是派生索引(不进 git)**——Actions 每天把增量 commit 回仓库,本地 `build_db.py` 一键重建,无状态漂移;**所有正式文件原子替换**,失败时旧数据保持不变。
- **当日快照不可变**,画像/日报/推送引用同一 `snapshot_id`,可追溯一致性;刷新必须显式 `--refresh-snapshot` 并自动归档旧版。
- **聚合采用 trusted 口径**:`full` 全可信、`partial` 仅 Top10 可信、`degraded` 不参与聚合;真实榜(quality 为空)单列;重建榜单日星标 ≥ 15000 视为疑似刷星,不参与"单日峰值"与"现象级爆发"展示(raw 记录保留)。
- **仓库身份现状**:当前仍以 `full_name` 为主键;API 补全已保存 `repo_id`/`node_id`/canonical 名。2026-09-01 已确认公共 `github_events` 表只有 `repo_name`、没有 repository ID，因此按迁移约束暂停 identity v2 历史合并，绝不按名称猜测。同名复用/改名仓库(如 `Jarred-Sumner/bun`)在可获得稳定 ID 的历史源前统计仍标为不可信，可用 `python scripts/audit_data.py` 列出。

## 部署每日任务(GitHub Actions)

1. 新建 GitHub 仓库,推送本项目;
2. 仓库 Settings → Secrets 添加 `GLM_API_KEY`、`FEISHU_WEBHOOK` 或自建应用四件套(可选 `GH_PAT`,额度更高);
3. Actions 每天 UTC 00:00(北京 08:00)运行:Job A `capture-and-persist`(抓取+画像+提交数据)→ Job B `notify`(回放快照+推送);通知失败时数据已保留,可手动重跑 Job B。

## 已知限制

- **2026-03-01 ~ 2026-08-29 为历史数据缺口**(GH Archive 公共源事件密度降至不可信),历史补齐方案见报告;费用需另行核实。
- 重建榜 ≠ 真实历史榜单(星标增速代理),趋势分析可靠,精确排名不可引用。
- `repo_meta_snapshot.csv` 存在 85 个重复仓库(61 个字段冲突),导入为 first-wins;`audit_data.py` 会持续报告,待确定性合并。
- 仓库身份迁移(repo_id 主键)未完成前,改名/同名复用仓库的历史统计与画像存在串档风险(异常数量以当前 audit 为准)。详情页对创建日期晚于首次上榜日期的仓库显示风险提示。
- GitHub trending 页面无官方 API,解析器带批次校验(条数/名次连续/星标覆盖率),页面改版时抓取会显式失败并保存诊断 HTML 到 `data/diagnostics/`,不会写入坏数据。
