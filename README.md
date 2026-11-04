# AI Leaders Digest

追踪 17 位 AI/科技领袖的公开动态：免费 RSS/API 来源（`sources.json`）加每日 web 搜索，存入 SQLite，由 Claude Code scheduled tasks 编排 Research → Extract → Context → Summarize 流水线生成中文摘要，并通过 Gmail SMTP 发送。

X 已关停 Nitter（2026 年 8 月起 RSS 无数据），因此不再直接抓取推文。`tweets` 表中的历史推文保留，`platform` 为 `x`。

## 架构

```
ai-leaders-fetch  (每天 09:40 PT)
  fetcher.py fetch → sources.json 中的 RSS/Bluesky 来源 → SQLite

ai-leaders-digest (每天 11:40 PT，ai-leaders-digest skill)
  1. fetcher.py fetch            再刷新一次（幂等）
  2. Research subagent           web 搜索过去 2 天 → fetcher.py save-items
  3. digest_generate.py query    内容落盘到 data/latest_tweets_<profile>.json
  4. Extract subagent            分拣 substantive / filler
  5. Context subagent            补事实背景（看不到原始内容）
  6. Summarize subagent          写中文摘要，逐条标注来源（无 web）
  7. digest_generate.py save-summary
  8. email_sender.py             Gmail SMTP；失败时回退到 Gmail 草稿
```

## 文件结构

```
ai-leaders-digest/
├── sources.json            # 作者与每位作者的来源配置
├── fetcher.py              # 数据层：来源抓取、web 条目入库、查询、管理
├── digest_generate.py      # 摘要层：数据加载 + Prompt 模板输出
├── email_sender.py         # Gmail SMTP 发送（按日期防重发）
├── api.py / db.py / static/  # Web UI
├── data/
│   ├── ai_leaders.db       # SQLite 数据库
│   ├── email_sent.jsonl    # 已发送日志（防重发）
│   └── latest_tweets_<profile>.json  # 每次 run 落盘的内容 JSON（供 Extract subagent 读取）
├── .claude/skills/ai-leaders-digest/SKILL.md  # 流水线编排
└── tests/                  # 单元测试 + Playwright UI 测试
```

## 来源

来源按作者配置在 [`sources.json`](./sources.json)，新增来源无需改代码。每个来源独立失败，互不影响。

| 作者 | 来源 | platform |
|------|------|----------|
| Andrej Karpathy | karpathy.bearblog.dev | blog |
| Andrej Karpathy | karpathy-mirr.selfhosted.social（非官方 X 镜像） | x-mirror |
| Sam Altman | blog.samaltman.com | blog |
| Sam Altman | sama-mirr.selfhosted.social（非官方 X 镜像） | x-mirror |
| Yoshua Bengio | Bluesky RSS | bluesky |
| Greg Brockman | blog.gregbrockman.com | blog |
| Andrew Ng | The Batch（仅整期，含 Andrew 的信） | newsletter |
| Sebastian Raschka | Ahead of AI（Substack） | newsletter |
| Simon Willison | simonwillison.net（只收长文 entries） | blog |
| Jack Clark | Import AI（Substack） | newsletter |
| Nathan Lambert | Interconnects（Substack） | newsletter |

Geoffrey Hinton、Elon Musk、Jensen Huang、Paul Graham、Yann LeCun、Demis Hassabis、Soumith Chintala、Serenity (@aleabitoreddit) 没有可用的 feed，只靠 web 搜索（`platform` 为 `web`）。Paul Graham 的 essay RSS 不带日期，无法判断新旧，所以没有收录。

来源类型：
- `feed`：RSS 或 Atom。必填 `url`；可选 `include_url`（只保留链接匹配该正则的条目）和 `link_rewrite`（`from` / `to` 前缀替换）。
- `bluesky`：Bluesky 公开 API。必填 `actor`。

所有来源都需要 `platform` 和 `label`，这两个字段决定摘要和 Web UI 中的来源标注。

### Web 搜索条目

`fetcher.py save-items [--days 2]` 从 stdin 读取 JSON 数组（`author`、`date`、`url`、`source_name`、`text`）。没有 URL 或日期、早于窗口、日期在未来或作者未知的条目会被拒绝；与同一作者已有条目 URL 或内容重复的条目不入库。

## Quick start

1. Copy `.env.example` to `.env` and set `DIGEST_RECIPIENT`, `GMAIL_SMTP_USER` and `GMAIL_APP_PASSWORD` — digests are sent via Gmail SMTP (see [Email delivery](#email-delivery)).
2. On first run, the SQLite DB will be auto-created (see `init_db()` in `fetcher.py`). The first `python3 fetcher.py fetch` run will populate it from `sources.json` — no manual seeding needed.
3. Register the scheduled tasks per [Setup scheduled tasks](#setup-scheduled-tasks).
4. (Optional) Launch the Web UI per [Web UI](#web-ui) below to browse tweets and digests.

## Web UI

A FastAPI + single-file HTML browser for the item feed and digest archive, themed after Elden Ring (deep brown/black, hairline gold, Cinzel + Cormorant Garamond). Reads the same `data/ai_leaders.db`, adds two columns (`starred`, `starred_at`) for marking tweets via additive `ALTER TABLE` migration on startup.

**Dependencies** (shared with job-pipeline):
```bash
pip3 install fastapi uvicorn
```

**Start the server**:
```bash
cd /path/to/ai-leaders-digest && python3 api.py
```

The server binds `0.0.0.0:8081`, so:
- Local desktop: http://127.0.0.1:8081
- Phone on the same Wi-Fi: `http://<mac-lan-ip>:8081` (find the Mac's IP with `ipconfig getifaddr en0`)

First time accessing from another device, **macOS firewall** will prompt: System Settings → Network → Firewall → Allow incoming connections for `python3` (or the Anaconda/Homebrew interpreter you used).

**Routes**:
- `/` — single-page UI (Tweets tab + Digests tab)
- `/api/stats`, `/api/authors`, `/api/tweets`, `/api/summaries`, `/api/summaries/{id}`
- `POST /api/tweets/{id}/star` and `/unstar`

**Features**:
- Search tweets full-text, filter by author, "★ Starred only" toggle
- Star / unstar tweets (persisted)
- Click an author name to jump to their feed
- Open any historical digest as rendered HTML in a modal
- Mobile-friendly (≤768 px breakpoint, 44 px touch targets)

## Testing

Unit tests (no network, temp DB):

```bash
python3 -m pytest tests/test_fetcher.py tests/test_email_sender.py
```

End-to-end UI regression suite using Playwright (Python). Hits the real local API — does not mock, does not spawn the server itself.

Install once:

```bash
pip install -r requirements-dev.txt
python3 -m playwright install chromium
```

Run (in a separate terminal from the running server):

```bash
python3 api.py                      # terminal 1 — keep the server up
pytest tests/ui/                    # terminal 2 — run the suite
```

Useful flags:
- `pytest tests/ui/ --headed` — watch the browser drive the page in real time
- `pytest tests/ui/ -k tab` — run a single test by name fragment
- `AI_LEADERS_URL=http://192.168.1.x:8081 pytest tests/ui/` — point at a remote / phone-LAN server

The whole suite is gated by `_require_server` in `tests/ui/conftest.py` — if the API isn't reachable the suite is cleanly skipped, not failed.

## 核心文件说明

### fetcher.py

纯 Python 标准库，零外部依赖。抓取 `sources.json` 中的来源，存入 SQLite。

**命令：**

| 命令 | 说明 |
|------|------|
| `fetch [--report-hour H]` | 抓取所有启用作者的来源，存入 DB。指定 H 时只在该小时输出完整报告 |
| `save-items [--days 2]` | 从 stdin 保存 web 搜索条目（校验 + 去重） |
| `query [天数] [--author X] [--category Y] [--profile Z]` | 查询内容，输出 JSON |
| `authors` | 列出所有跟踪的人 |
| `profiles` | 列出所有 Focus Profile |
| `add-profile <名> <JSON>` | 添加自定义 Focus Profile |
| `subscribers` | 列出订阅者 |
| `add-subscriber --email <email> [--name <name>] [--profile <profile>]` | 添加订阅者 |
| `remove-subscriber <email-or-phone>` | 删除订阅者 |
| `toggle-subscriber <email-or-phone>` | 启用/暂停订阅者 |
| `stats [天数]` | 统计信息 |
| `save-summary [天数] [profile]` | 从 stdin 保存摘要到 DB |

**特性：**
- 每个来源独立失败；全部失败时才以非零状态退出
- tweet_id 自动去重（`feed:` / `bsky:` / `web:` 前缀）
- `--report-hour` 支持静默抓取（非报告时间只存数据不输出）

### digest_generate.py

数据加载 + Prompt 模板输出。不调用 LLM，LLM 调用由 `ai-leaders-digest` skill 通过 Claude Code 的 `Agent` (Task) 工具编排。

**命令：**

| 命令 | 说明 |
|------|------|
| `research [--days 2]` | 输出 Research 步骤的 prompt（搜索提示 + 已有条目） |
| `query [--days 5] [--profile default]` | 内容落盘，输出元信息 + 三步 Prompt 模板 JSON |
| `save-summary [--days 5] [--profile default]` | 从 stdin 保存摘要到 DB |
| `stats` | 简要统计 |

**query 输出 JSON 结构：**
```json
{
  "meta": {
    "days", "date", "tweet_count", "sources_ok", "sources_total",
    "profile", "focus_instructions",
    "tweets_file": "data/latest_tweets_<profile>.json 的绝对路径"
  },
  "prompts": {
    "extract": "分拣 Prompt（告诉 subagent 用 Read 读 tweets_file）",
    "context_template": "背景调研模板（{extracted} 占位符）",
    "summarize_template": "写作模板（{contexted} 占位符）"
  }
}
```

**设计要点：内容原文不进 stdout**。早期设计把 120KB+ 的 JSON 内嵌到 prompt 中，导致父 agent 的首次 LLM API 请求 payload 过大、TTFT 超过 10 分钟被 cron inactivity watchdog 杀掉。改造后内容落盘到 `data/latest_tweets_<profile>.json`，stdout 只传文件路径，由 Extract subagent 用 `Read` 工具自己读。

## 流水线隔离设计

| 步骤 | Subagent | 输入 | 输出 | Web 预算 |
|------|----------|------|------|----------|
| Research | #1 | 作者搜索提示 + 已有条目 | web 条目 JSON（经 `save-items` 校验） | 18 |
| Extract | #2 | 全部内容文件 | substantive / filler 分拣 JSON | 0 |
| Context | #3 | 只有 Extract JSON | 加 `context` 和 `sources` 的 JSON | 8 |
| Summarize | #4 | 只有 Context JSON | 终稿，逐条标注来源 | 0 |

每个 subagent 通过 Claude Code 的 `Agent` (Task) 工具创建，天然上下文隔离。

## Focus Profiles

控制摘要如何分配关注度。

| Profile | 重点 | 权重 | 非重点处理 |
|---------|------|------|-----------|
| default | 均衡关注 | 1x | normal |
| karpathy | Andrej Karpathy | 2x | brief |
| ai-tech | Karpathy + Andrew Ng | 2x | brief |
| founders | Paul Graham + Sam Altman | 2x | brief |

自定义示例：
```bash
python3 fetcher.py add-profile myprofile '{
  "focus_authors": ["karpathy", "ylecun"],
  "focus_categories": ["ai-pioneer"],
  "focus_weight": 2,
  "focus_instructions": "比较他们对 world models 的看法",
  "others": "skip",
  "max_summary_length": "long"
}'
```

## 数据库结构

SQLite（`data/ai_leaders.db`），5 张表：

| 表 | 说明 |
|----|------|
| authors | 跟踪的人（handle, name, category, enabled）。`rss_url` 已废弃，来源见 `sources.json` |
| tweets | 所有内容（历史推文、feed、web），按 tweet_id 去重。`platform`：x / x-mirror / bluesky / blog / newsletter / web；`source_name`：来源名；`url`：来源链接 |
| summaries | 生成的摘要历史 |
| focus_profiles | Focus 配置 |
| subscribers | 订阅者 |

## Scheduled Tasks (Claude Code)

| Task | 时间 (PT) | 说明 |
|------|----------|------|
| `ai-leaders-fetch` | 09:40 | 跑 `python3 fetcher.py fetch` |
| `ai-leaders-digest` | 11:40 | 调用 `ai-leaders-digest` skill：web 搜索 + 三步流水线 + 保存 DB + 发送邮件 |

任务由 Claude Code 的 scheduled-tasks 系统管理（`~/.claude/scheduled-tasks/`），无须独立守护进程。

### Setup scheduled tasks

Scheduled tasks are declared in [`schedules.yaml`](./schedules.yaml). To register them on your machine, open Cowork (Claude Code) in this project and say:

> Read schedules.yaml and register every task defined there as a Cowork scheduled task.

Claude will use the `schedule` skill to create each task, copying the relevant SKILL.md (or embedding the inline `script:` for skill-less tasks like `ai-leaders-fetch`) into `~/Documents/Claude/Scheduled/`.

## Email delivery

Digests are sent by `email_sender.py` through `smtp.gmail.com:587` (STARTTLS) as plain text + rendered HTML. Set `DIGEST_RECIPIENT`, `GMAIL_SMTP_USER` and `GMAIL_APP_PASSWORD` in `.env` (see `.env.example`); the App Password comes from https://myaccount.google.com/apppasswords and requires 2-Step Verification. Preview without sending using `python3 email_sender.py --dry-run`. Each digest date is sent once (logged in `data/email_sent.jsonl`); use `--force` to resend. If sending fails, the skill falls back to a Gmail draft via the Gmail MCP `create_draft` tool when that connector is available.

## 手动使用

```bash
cd /path/to/ai-leaders-digest

# 抓取最新内容
python3 fetcher.py fetch

# 查看统计
python3 fetcher.py stats 7
python3 digest_generate.py stats

# 查看 Karpathy 最近 7 天
python3 fetcher.py query 7 --profile karpathy

# 列出所有作者
python3 fetcher.py authors
```

## 迁移说明

从 OpenClaw → Hermes → Claude Code 演进而来。当前迭代的主要改动：

- `rss_digest.py` → `fetcher.py`（重命名，功能不变）
- `digest_generate.py` 去掉了 OpenClaw Gateway API 调用，输出 JSON + 三步 Prompt 模板
- 编排层从 Hermes `delegate_task` 切到 Claude Code `Agent` (Task) 工具，由 `.claude/skills/ai-leaders-digest/SKILL.md` 描述完整流水线
- 调度层从 Hermes cron 切到 Claude Code scheduled-tasks（`~/.claude/scheduled-tasks/`），不再依赖独立守护进程
- Nitter 关停后，数据源改为 `sources.json` 中的 RSS/API 来源 + web 搜索；发送方式从 Gmail 草稿改为 SMTP

## 已知限制

- 8 位作者没有 feed，只靠 web 搜索，覆盖率取决于媒体是否报道；"沉默的狗"只代表没抓到，不代表本人没发言
- X 镜像是非官方账号，随时可能停更；`fetch` 报告里每个来源的 `newest` 可用来发现停更
- 无法用代码核实 web 条目的引语是否逐字准确，只能靠 prompt 约束
- 摘要窗口 5 天、每天发送，同一条内容可能连续几天出现
- 流水线串行执行，生成摘要需要 10-15 分钟
- 每次 run 会覆盖 `data/latest_tweets_<profile>.json`（只保留最新一份，供 Extract subagent 读取）
