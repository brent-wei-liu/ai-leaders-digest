#!/usr/bin/env python3
"""
AI Leaders Digest — 数据加载 + 三步 Prompt 模板，供 Hermes cron job 编排。

去掉了 OpenClaw Gateway API 依赖，LLM 调用由 Hermes delegate_task 完成，
三个 subagent 相互隔离，等价于原来的 isolated session。

Usage:
  python3 digest_generate.py query [--days 3] [--profile default]
      → 输出推文数据 JSON（供 cron script 注入）

  python3 digest_generate.py save-summary [--days 3] [--profile default]
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


# ── Three-Step Prompt Templates ──────────────────────────────────────
# 保留原来的三步隔离设计，由 Hermes delegate_task 编排

DRAFT_PROMPT = """你是一位资深 AI 行业分析师，为 AI/ML 从业者撰写摘要。
你的任务是 INTERPRET（解读），而非简单报道。找出模式，解释意义，连接线索。
中文评论，英文人名/术语。简洁、适合手机阅读。

推文数据存储在 JSON 文件中。请用 read_file 工具读取完整文件：
文件路径：{tweets_file}

文件结构（按作者 handle 分组）：
{{
  "<handle>": {{
    "name": "<显示名>",
    "category": "<ai-pioneer/tech-leader/startup/...>",
    "tweets": [
      {{"date": "<ISO8601>", "text": "<推文正文>", "is_retweet": true/false}},
      ...
    ]
  }},
  ...
}}

元信息：
- 时间范围：过去 {days} 天
- 推文总数：{tweet_count} 条
- Sources：{sources_ok}/{sources_total}
- 日期：{date}

请按以下格式生成摘要：

📰 AI Leaders Digest - {date}
(过去 {days} 天，{tweet_count} 条内容，{sources_ok}/{sources_total} sources)

🔮 本期洞察 (Analyst Take)
放在最前面，3-5 个要点：
- 跨人物模式：多位领袖不约而同讨论同一主题时，这种趋同意味着什么？
- "So what" 分析：为什么重要，对行业意味着什么
- 反常识/意外观点：领袖们在哪些方面有分歧？字里行间在说什么？
- 前瞻：从业者接下来该关注什么？
- 要具体且有观点。不要写"AI安全正在被讨论"，要写"Bengio 的 UN 欺骗警告 + Karpathy 的供应链提醒表明安全对话正从理论对齐转向即时运营风险"

🔥 话题聚合
按话题分组，每个话题：
- 列出相关帖子及作者
- 用中文加 1-2 句上下文
- 每个话题最后加粗"→ 意味着什么："——连接作者未连接的点，指出未被提及的内容，或解释二阶影响

{focus_instructions}

👤 人物动态
每位发帖者一条简短中文要点，包括他们当前的战略姿态（在推动什么，为什么）。

保持简洁、手机友好。
只输出摘要正文，不要元评论。"""

CRITIQUE_PROMPT = """你是一位犀利的编辑审稿人，负责审阅 AI 行业摘要。
你没有看过原始推文——你只能评估摘要本身的质量。
你的任务是发现分析质量的弱点，而非事实准确性。
具体、直接、有建设性。用中文写。

审阅以下 AI Leaders Digest 初稿：

DRAFT:
{draft}

评估标准：

1. **洞察质量 (Insight Quality)**
   - 本期洞察是否包含超越事实复述的真正分析？
   - 一位资深 AI 工程师能否从中学到自己读推文学不到的东西？
   - 识别出的模式是真正的跨人物趋同，还是牵强附会？

2. **"So What" 检验**
   - 每个"→ 意味着什么"是否真正增加了洞察？
   - 如果读起来像"X 很重要因为它是大事"——标记为弱
   - 是否解释了二阶影响，还是只是复述一阶事实？

3. **信号 vs 噪音 (Signal vs Noise)**
   - 摘要是否给了真正重要的信号相应的篇幅？
   - 还是像新闻通讯社一样平均覆盖所有内容？
   - 什么应该得到更多关注？什么应该砍掉？

4. **遗漏与盲点 (Blind Spots)**
   - 是否遗漏了领袖之间有趣的紧张关系或分歧？
   - 是否有"沉默的狗"（预期话题上的显著沉默）？
   - 不同话题簇之间是否有非显而易见的联系？

5. **可读性 (Readability)**
   - 手机友好吗？太长？太密？
   - 中文是否自然简洁？

对发现的每个弱点，提供具体改进建议。
最后给出总体质量评分：A（可直接发布）、B（需小修）、C（需大改）。"""

REFINE_PROMPT = """你是一位资深 AI 行业分析师，正在制作摘要的最终版本。
你有原始初稿和编辑反馈。请产出改进后的最终版本。
解决每一个审稿意见。强化弱洞察。砍掉水分。让每句话都有存在价值。
中文评论，英文人名/术语。简洁、适合手机阅读。

原始初稿：
{draft}

编辑审稿意见：
{critique}

规则：
- 解决每一个具体的审稿意见
- 洞察被标记为弱的，要么用非显而易见的观点强化，要么砍掉
- "→ 意味着什么"被标记为显而易见的，要么变得不显而易见，要么删除该话题簇
- 信噪比失衡的，重新分配关注度
- 补充审稿人识别出的遗漏联系或盲点
- 最终输出只有改进后的摘要正文（不要关于修改的元评论）
- 保持相同的格式结构"""


# ── Commands ─────────────────────────────────────────────────────────

def cmd_query(days=3, profile="default"):
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
            "draft": DRAFT_PROMPT.format(
                days=days,
                tweets_file=str(tweets_file),
                date=today,
                tweet_count=data["tweet_count"],
                sources_ok=data["sources_ok"],
                sources_total=data["sources_total"],
                focus_instructions=focus_instructions,
            ),
            "critique_template": CRITIQUE_PROMPT,
            "refine_template": REFINE_PROMPT,
        },
    }

    print(json.dumps(output, ensure_ascii=False))


def cmd_save(days=3, profile="default"):
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
        kwargs = {"days": 3, "profile": "default"}
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
        kwargs = {"days": 3, "profile": "default"}
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
