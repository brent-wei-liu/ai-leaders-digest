#!/usr/bin/env python3
"""
AI Leaders Digest — 数据加载 + 三步 Prompt 模板。

Pipeline: Extract → Context → Summarize (three isolated subagents).
Replaces the older Draft → Critique → Refine flow, which forced the
Critique step to invent cross-author "insights" on thin tweet days.

Usage:
  python3 digest_generate.py query [--days 5] [--profile default]
      → 输出推文数据 JSON（含 extract/context_template/summarize_template）

  python3 digest_generate.py save-summary [--days 5] [--profile default]
      → 从 stdin 读取摘要文本，保存到 DB

  python3 digest_generate.py stats
      → 输出简要统计
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB_PATH = os.environ.get(
    "RSS_DB_PATH",
    str(Path(__file__).resolve().parent / "data" / "ai_leaders.db"),
)


# ── Data Loading ──────────────────────────────────────────────────────

def load_tweets(days=3, profile="default"):
    """Load tweets from DB, return formatted data."""
    import sqlite3

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")  # 5s wait vs default 0 — see db.py
    conn.row_factory = sqlite3.Row

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    rows = conn.execute("""
        SELECT t.author, a.name, a.category, t.date, t.text, t.url, t.is_retweet
        FROM tweets t
        JOIN authors a ON t.author = a.handle
        WHERE t.date >= ? AND a.enabled = 1
        ORDER BY t.date DESC
    """, (cutoff,)).fetchall()

    sources_ok = len(set(r["author"] for r in rows))
    sources_total = conn.execute("SELECT COUNT(*) FROM authors WHERE enabled = 1").fetchone()[0]

    # Load focus profile
    profile_row = conn.execute(
        "SELECT rules FROM focus_profiles WHERE name = ?", (profile,)
    ).fetchone()
    profile_rules = json.loads(profile_row["rules"]) if profile_row else {}

    # Group by author
    by_author = {}
    for r in rows:
        a = r["author"]
        if a not in by_author:
            by_author[a] = {"name": r["name"], "category": r["category"], "tweets": []}
        by_author[a]["tweets"].append({
            "date": r["date"],
            "text": r["text"],
            "is_retweet": bool(r["is_retweet"]),
        })

    conn.close()

    return {
        "tweet_count": len(rows),
        "sources_ok": sources_ok,
        "sources_total": sources_total,
        "profile": profile_rules,
        "data": by_author,
    }


def save_summary(content, days=3, profile="default"):
    """Save summary to DB."""
    import sqlite3

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")  # 5s wait vs default 0 — see db.py
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc).isoformat()

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    tweet_count = conn.execute("SELECT COUNT(*) FROM tweets WHERE date >= ?", (cutoff,)).fetchone()[0]
    sources_ok = conn.execute("SELECT COUNT(DISTINCT author) FROM tweets WHERE date >= ?", (cutoff,)).fetchone()[0]
    sources_total = conn.execute("SELECT COUNT(*) FROM authors WHERE enabled = 1").fetchone()[0]

    conn.execute(
        """INSERT INTO summaries (date, days_back, tweet_count, sources_ok, sources_total, focus_profile, content, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (today, days, tweet_count, sources_ok, sources_total, profile, content, now),
    )
    conn.commit()
    conn.close()
    return {"saved": True, "date": today, "tweet_count": tweet_count}


# ── Three-Step Pipeline Prompt Templates ─────────────────────────────
# Pipeline (NOT reflection): Extract → Context → Summarize.
# Replaces the older Draft → Critique → Refine flow that forced the
# Critique step to invent cross-author "insights" on thin tweet days.
# The new flow's job is: faithfully summarize what each person said,
# with web-fetched background for the substantive items. No forced
# pattern-mining, no editorial cuts disguised as critique.

EXTRACT_PROMPT = """你是 AI 领袖推文摘要流水线的第 1 步：分拣。

任务：把按作者分组的推文按"信号值"分类。不要写摘要，不要做洞察——
只做忠实分拣。

输入：JSON 文件，按作者 handle 分组。
路径：{tweets_file}

文件结构（按作者 handle 分组）：
{{
  "<handle>": {{
    "name": "<显示名>",
    "category": "<ai-pioneer/tech-leader/startup/...>",
    "tweets": [
      {{"date": "<ISO8601>", "text": "<推文正文>", "is_retweet": true/false}},
      ...
    ]
  }}
}}

元信息：
- 时间范围：过去 {days} 天
- 推文总数：{tweet_count} 条
- Sources：{sources_ok}/{sources_total}
- 日期：{date}

请用 Read 工具读取完整文件。然后对每条推文判断：

**substantive（实质性）** —— 满足任一即可：
- 提及具体论文 / 产品发布 / 数据集 / benchmark 数字
- 宣布公司动态（融资、收购、人事、产品线变动）
- 透露技术细节或路线选择
- 公开战略表态（定价、合作、市场）
- 回应/挑战另一位 AI 领袖
- 描述具体工作流 / 工具使用 / 实测结果

**filler（噪声）** —— 全部归入：
- 转推（除非被转推的内容本身满足 substantive 标准；那样保留为 substantive）
- 个人日常 / 度假 / 段子 / 表情
- 与 AI 无关的政治 / 文化战 / 体育 / 名人八卦
- 单纯祝贺 / 致谢 / "thank you" / "amazing"
- 招聘广告（除非附带具体团队信息）

{focus_instructions}

可选 web 调用（最多 **2 次**）：仅当推文里出现一个你完全不认识的实体
（人名、公司名、产品名）且不确定它是不是 substantive 维度的依据时做一次
WebSearch。**不要**用 web 调用做事实校验——那是下一步的事。

输出格式（**严格 JSON**，放在一个 ```json 代码块里，外面不要再写其他文字）：

```json
{{
  "<handle>": {{
    "name": "<显示名>",
    "category": "<category>",
    "substantive": [
      {{
        "date": "<ISO8601>",
        "text": "<原文，保留 emoji 和 URL>",
        "tag": "paper|product|company|strategy|reply|workflow|other",
        "is_retweet": true/false
      }}
    ],
    "filler_count": <整数>,
    "filler_summary": "<一句话概括 filler 推文的主题分布，例如 '主要是英国言论自由+FSD 转推'>"
  }}
}}
```

预算：2 次 web 调用，硬上限。"""

CONTEXT_PROMPT = """你是 AI 领袖推文摘要流水线的第 2 步：背景调研。

任务：为每条 substantive 推文添加事实背景。

**重要**：你看不到原始推文文件——只有上一步抽出的 substantive 列表。
这是设计如此：你的工作是验证和扩展 list 里这些条目，而不是重新做分拣。

上一步输出的 JSON：

{extracted}

对**每条 substantive 推文**，目标是回答：
- 推文提到的论文 / 产品 / 事件，**它是什么**？（1-2 句话）
- 关键的客观数字（论文成绩、产品价格、benchmark 分数、融资金额）能不能补全？
- 推文回应或挑战谁？原线索是什么？
- 提到的公司 / 人物当前的角色 / 状态是否与推文一致？

可选 web 调用（最多 **12 次，硬上限**）：
- WebSearch: 找论文 abstract / 产品页 / 公司新闻
- WebFetch: 拉具体的产品发布页或 arXiv abstract
- 12 次平均分配 ≈ 一个作者约 1-2 次；substantive 列表更长就更省。
- 优先级：含具体名词（产品 / 论文 / 公司 / benchmark）的推文 > 抽象观点推文

输出格式（**严格 JSON**，放在一个 ```json 代码块里）：保持上一步结构，
在每条 substantive 后**新增**两个字段：
- `context`: 一段中文背景（1-3 句），如果没必要做 web 调用可以写 "自明"
- `sources`: URL 数组，记录用到的 web 资源；没用就空数组 `[]`

不要修改 `name` / `category` / `filler_*` 字段。
不要删任何 substantive 项目，即使你觉得它不重要——分拣是上一步的事。

预算：12 次 web 调用，硬上限。"""

SUMMARIZE_PROMPT = """你是 AI 领袖推文摘要流水线的第 3 步：写作。

任务：基于上一步加了背景的 JSON，写一份**忠实**的中文 digest。
**不要**硬挤跨人物 insight，**不要**做"→ 意味着什么"的二阶推论。
如果某天领袖们说的事就是平淡，digest 就应该读起来平淡——这是诚实，不是失败。

**重要**：你只看 contexted JSON，不重新读原始推文。**不要**做 web 调用。

上一步输出的 JSON：

{contexted}

输出格式（**严格按以下 4 段**）：

📰 AI Leaders Digest - {date}
(过去 {days} 天，{tweet_count} 条内容，{sources_ok}/{sources_total} sources)

---

✨ 本期亮点（2-3 条）

挑信息密度最高的 2-3 条 substantive 推文。判断标准是**有具体新东西**
（论文成绩、产品上线、人事变动、benchmark 数字）——**不是**作者的名气。
每条 1 段：
- 第一句：谁说了什么（中文转述 + 关键英文术语保留）
- 第二句起：context（从 JSON 里来的 1-2 句背景）
- 最后一句（**可选**）：如果你看出一个**显然的**对从业者的含义，写一句；
  如果觉得勉强，**不写**

---

👤 逐人小记

按 substantive 推文数从多到少排序。每位作者**一段**：

格式：**作者名 (@handle)**：把 substantive 列表里的内容用第三人称中文转述，
每条尾随该条的 context（"背景：..."）。如果有 filler，最后用一句话点出
（不要详细列）。

如果作者**没有 substantive**（全是 filler）：只写
"**作者名 (@handle)**：仅日常推文（{{filler_summary}}），无重要信号。"

**不要**写"战略姿态"或"在推动什么"——那是上一版的硬挤产物，已删除。
**忠实转述 + 背景补全**就够了。

---

🐕 沉默的狗

列出 enabled 但本期窗口内 0 条推文的作者。每人一行：
`**作者名 (@handle)**: 0 条`

如果所有 enabled 作者都有推文，写一句 "本期所有作者均有推文。" 即可。

---

写作要求：
- 中文为主，英文人名 / 产品 / 术语保留
- 段落式叙述；除"沉默的狗"外不要列项符
- 每个 substantive 推文都要被覆盖；**不要**因为"觉得不重要"就跳过
- 字数不限——该长则长，该短则短

预算：0 次 web 调用。所有信息都在 contexted JSON 里。"""


# ── Commands ─────────────────────────────────────────────────────────

def cmd_query(days=5, profile="default"):
    """输出元信息 + prompt 模板 JSON 到 stdout；推文数据写到磁盘文件。

    为避免父 agent 的 API payload 过大导致 TTFT 超时（>10min），
    推文 JSON 不再内嵌到 prompt 中，而是落盘到 data/latest_tweets_<profile>.json，
    由 Draft subagent 自己用 read_file 读取。
    """
    data = load_tweets(days, profile)

    if data["tweet_count"] == 0:
        print(json.dumps({"error": "没有找到推文数据，请先运行 fetcher.py fetch"}, ensure_ascii=False))
        sys.exit(1)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Build focus instructions
    focus_instructions = ""
    pr = data["profile"]
    if pr.get("focus_authors"):
        focus_instructions += f"重点关注（给予 {pr.get('focus_weight', 2)}x 篇幅）：{', '.join(pr['focus_authors'])}\n"
    if pr.get("focus_instructions"):
        focus_instructions += pr["focus_instructions"] + "\n"
    if pr.get("others") == "brief":
        focus_instructions += "非重点作者每人只给一行。\n"

    # 推文数据落盘，供 Draft subagent 读取（绝对路径避免 cwd 问题）
    data_dir = Path(DB_PATH).resolve().parent
    data_dir.mkdir(parents=True, exist_ok=True)
    tweets_file = data_dir / f"latest_tweets_{profile}.json"
    tweets_file.write_text(
        json.dumps(data["data"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 输出瘦身后的编排数据（不含推文正文）
    output = {
        "meta": {
            "days": days,
            "date": today,
            "tweet_count": data["tweet_count"],
            "sources_ok": data["sources_ok"],
            "sources_total": data["sources_total"],
            "profile": profile,
            "focus_instructions": focus_instructions,
            "tweets_file": str(tweets_file),
        },
        "prompts": {
            # Step 3: read raw tweets, classify substantive vs filler
            "extract": EXTRACT_PROMPT.format(
                days=days,
                tweets_file=str(tweets_file),
                date=today,
                tweet_count=data["tweet_count"],
                sources_ok=data["sources_ok"],
                sources_total=data["sources_total"],
                focus_instructions=focus_instructions,
            ),
            # Step 4: background lookup on substantive items (no raw tweets)
            "context_template": CONTEXT_PROMPT,
            # Step 5: write the digest from contexted JSON (no web, no raw tweets)
            "summarize_template": SUMMARIZE_PROMPT.format(
                date=today,
                days=days,
                tweet_count=data["tweet_count"],
                sources_ok=data["sources_ok"],
                sources_total=data["sources_total"],
                contexted="{contexted}",  # left as placeholder; SKILL.md fills it in
            ),
        },
    }

    print(json.dumps(output, ensure_ascii=False))


def cmd_save(days=5, profile="default"):
    """从 stdin 读取摘要，保存到 DB。"""
    content = sys.stdin.read().strip()
    if not content:
        print('{"error": "stdin 无内容"}')
        sys.exit(1)

    result = save_summary(content, days, profile)
    print(json.dumps(result, ensure_ascii=False))


def cmd_stats():
    """输出简要统计。"""
    import sqlite3

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")  # 5s wait vs default 0 — see db.py
    conn.row_factory = sqlite3.Row

    for days in [3, 7]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        count = conn.execute("SELECT COUNT(*) FROM tweets WHERE date >= ?", (cutoff,)).fetchone()[0]
        sources = conn.execute("SELECT COUNT(DISTINCT author) FROM tweets WHERE date >= ?", (cutoff,)).fetchone()[0]
        print(f"过去 {days} 天：{count} 条推文，{sources} 位作者")

    summaries = conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
    latest = conn.execute("SELECT date, focus_profile FROM summaries ORDER BY id DESC LIMIT 1").fetchone()
    print(f"历史摘要：{summaries} 篇")
    if latest:
        print(f"最近一篇：{latest['date']}（profile: {latest['focus_profile']}）")

    conn.close()


# ── CLI ───────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]
    args = sys.argv[2:]

    if cmd == "query":
        kwargs = {"days": 5, "profile": "default"}
        i = 0
        while i < len(args):
            if args[i] == "--days" and i + 1 < len(args):
                kwargs["days"] = int(args[i + 1]); i += 2
            elif args[i] == "--profile" and i + 1 < len(args):
                kwargs["profile"] = args[i + 1]; i += 2
            else:
                i += 1
        cmd_query(**kwargs)

    elif cmd == "save-summary":
        kwargs = {"days": 5, "profile": "default"}
        i = 0
        while i < len(args):
            if args[i] == "--days" and i + 1 < len(args):
                kwargs["days"] = int(args[i + 1]); i += 2
            elif args[i] == "--profile" and i + 1 < len(args):
                kwargs["profile"] = args[i + 1]; i += 2
            else:
                i += 1
        cmd_save(**kwargs)

    elif cmd == "stats":
        cmd_stats()

    else:
        print(f"未知命令: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
