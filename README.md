# AI Leaders Digest

追踪 12 位 AI/科技领袖的推文，通过 Nitter RSS 抓取、SQLite 存储，由 Hermes cron job 编排三步隔离反思流水线（Draft → Critique → Refine）生成高质量中文摘要。

## 架构

```
┌─────────────────────────────────────────────────────────────┐
│  Hermes Cron Jobs                                           │
│                                                             │
│  ┌─────────────────────┐    ┌─────────────────────────────┐ │
│  │ RSS Fetch (4x/day)  │    │ Daily Digest (1x/day 10am) │ │
│  │ 8:00 静默            │    │                             │ │
│  │ 12:00 静默           │    │  script: digest query       │ │
│  │ 16:00 静默           │    │       ↓                     │ │
│  │ 20:00 汇报           │    │  script stdout (~3KB):      │ │
│  │                     │    │   meta + tweets_file 路径   │ │
│  │ script: fetcher.py  │    │   + 3 步 prompt 模板        │ │
│  │       ↓             │    │       ↓                     │ │
│  │    SQLite DB        │    │  推文 JSON 落盘到 data/     │ │
│  └─────────────────────┘    │   latest_tweets_<profile>   │ │
│                             │       ↓                     │ │
│                             │  父 Agent (sonnet-4-5)      │ │
│                             │  编排 delegate_task         │ │
│                             │       ↓                     │ │
│                             │  ┌──────────────────────┐   │ │
│                             │  │ Subagent 1: Draft    │   │ │
│                             │  │ read_file 读推文 JSON│   │ │
│                             │  └──────────┬───────────┘   │ │
│                             │             ↓               │ │
│                             │  ┌──────────────────────┐   │ │
│                             │  │ Subagent 2: Critique │   │ │
│                             │  │ (只看得到初稿，隔离) │   │ │
│                             │  └──────────┬───────────┘   │ │
│                             │             ↓               │ │
│                             │  ┌──────────────────────┐   │ │
│                             │  │ Subagent 3: Refine   │   │ │
│                             │  │ (初稿 + 审稿意见)    │   │ │
│                             │  └──────────┬───────────┘   │ │
│                             │             ↓               │ │
│                             │  ┌──────────────────────┐   │ │
│                             │  │ Step 4: Save Summary │   │ │
│                             │  │ (终稿写入 SQLite DB) │   │ │
│                             │  └──────────┬───────────┘   │ │
│                             │             ↓               │ │
│                             │     最终摘要 → Telegram     │ │
│                             └─────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

## 文件结构

```
~/.hermes/hermes-agent/ai-leaders-digest/
├── fetcher.py              # 数据层：RSS 抓取、SQLite 存储、查询、管理
├── digest_generate.py      # 摘要层：数据加载 + 三步 Prompt 模板输出
├── data/
│   ├── ai_leaders.db       # SQLite 数据库
│   └── latest_tweets_<profile>.json  # 每次 cron run 落盘的推文 JSON（供 Draft subagent 读取）
└── README.md

~/.hermes/scripts/
├── ai_leaders_fetch.py     # Cron 包装：调用 fetcher.py fetch
└── ai_leaders_digest.py    # Cron 包装：调用 digest_generate.py query
```

## 跟踪的 12 位领袖

| Handle | 姓名 | 分类 |
|--------|------|------|
| karpathy | Andrej Karpathy | ai-engineering |
| soumithchintala | Soumith Chintala | ai-engineering |
| sama | Sam Altman | ai-industry |
| gdb | Greg Brockman | ai-industry |
| geoffreyhinton | Geoffrey Hinton | ai-pioneer |
| AndrewYNg | Andrew Ng | ai-pioneer |
| ylecun | Yann LeCun | ai-pioneer |
| Yoshua_Bengio | Yoshua Bengio | ai-pioneer |
| demishassabis | Demis Hassabis | ai-pioneer |
| elonmusk | Elon Musk | tech-leader |
| jensenhuang | Jensen Huang | tech-leader |
| paulg | Paul Graham | startup |

## 核心文件说明

### fetcher.py

纯 Python 标准库，零外部依赖。通过 Nitter RSS 抓取推文，存入 SQLite。

**命令：**

| 命令 | 说明 |
|------|------|
| `fetch [--report-hour H]` | 抓取所有启用作者的 RSS，存入 DB。指定 H 时只在该小时输出完整报告 |
| `query [天数] [--author X] [--category Y] [--profile Z]` | 查询推文，输出 JSON |
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
- 多 Nitter 实例自动 fallback
- tweet_id 自动去重
- `--report-hour` 支持静默抓取（非报告时间只存数据不输出）

### digest_generate.py

数据加载 + 三步 Prompt 模板输出。不调用 LLM，LLM 调用由 Hermes cron agent 通过 delegate_task 完成。

**命令：**

| 命令 | 说明 |
|------|------|
| `query [--days 3] [--profile default]` | 输出推文数据 + 三步 Prompt 模板 JSON |
| `save-summary [--days 3] [--profile default]` | 从 stdin 保存摘要到 DB |
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
    "draft": "初稿 Prompt（告诉 subagent 用 read_file 读 tweets_file）",
    "critique_template": "审稿模板（{draft} 占位符）",
    "refine_template": "精修模板（{draft} + {critique} 占位符）"
  }
}
```

**设计要点：推文原文不进 stdout**。早期设计把 120KB+ 推文 JSON 内嵌到 draft prompt 中，导致父 agent 的首次 LLM API 请求 payload 过大、TTFT 超过 10 分钟被 cron inactivity watchdog 杀掉。改造后推文落盘到 `data/latest_tweets_<profile>.json`，stdout 只传文件路径（~3KB），由 Draft subagent 用 `read_file` 工具自己读。subagent 的 system prompt 不包含父 agent 的工具 schema + memory，payload 小很多。

## 三步隔离反思设计

核心思想：审稿人看不到原始数据，只能评估摘要质量。

| 步骤 | Subagent | 输入 | 输出 | 隔离 |
|------|----------|------|------|------|
| Draft | #1 | 原始推文 + 格式指令 | 初稿 | 看得到原始推文 |
| Critique | #2 | 只有初稿 | 审稿意见 + A/B/C 评分 | 看不到原始推文 |
| Refine | #3 | 初稿 + 审稿意见 | 终稿 | 看不到原始推文 |

每个 subagent 通过 Hermes `delegate_task` 创建，天然上下文隔离。

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
| authors | 跟踪的人（handle, name, category, rss_url, enabled） |
| tweets | 所有推文，按 tweet_id 去重 |
| summaries | 生成的摘要历史 |
| focus_profiles | Focus 配置 |
| subscribers | 订阅者 |

## Cron Jobs

| Job | 时间 (PST) | 模型 | 说明 |
|-----|-----------|------|------|
| AI Leaders RSS Fetch | 8:00, 12:00, 16:00, 20:00 | — | 抓取 RSS，20 点发汇报 |
| AI Leaders Daily Digest | 10:00 | 父 agent: sonnet-4-5；subagents: 默认（opus） | 三步反思生成摘要，保存到 DB，发送到 Telegram |

**为什么父 agent 用 sonnet-4-5？** 父 agent 只做编排（parse JSON + 3 次 `delegate_task` + 1 次 `terminal` 保存），不需要深度推理。sonnet 启动 TTFT 明显比 opus 快，避免 cron inactivity watchdog 超时。subagent 才是真正生产内容的，保留默认 opus 保证质量。

## 手动使用

```bash
cd ~/.hermes/hermes-agent/ai-leaders-digest

# 抓取最新推文
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

从 OpenClaw workspace 迁移而来。主要改动：

- `rss_digest.py` → `fetcher.py`（重命名，功能不变）
- `digest_generate.py` 去掉了 OpenClaw Gateway API 调用，改为输出 JSON + Prompt 模板，LLM 调用由 Hermes delegate_task 完成
- Cron 脚本必须是 .py（Hermes scheduler 固定用 Python 解释器执行）
- Cron 脚本必须放在 `~/.hermes/scripts/`（路径校验限制）

## 已知限制

- Nitter 实例不稳定，有 fallback 但可能全挂（尤其 Elon Musk 的 RSS）
- 三步 delegate_task 串行执行，生成摘要需要 10-15 分钟
- 每次 cron run 会覆盖 `data/latest_tweets_<profile>.json`（只保留最新一份，供 Draft subagent 读取）
