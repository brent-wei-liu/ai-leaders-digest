---
name: ai-leaders-digest
description: >
  Generate a daily Chinese-language digest of AI leaders' tweets via a
  three-step Draft / Critique / Refine reflection pipeline. Use when the user
  asks for an AI leaders digest, daily AI digest, AI 大佬日报, AI 领袖摘要,
  reflection digest run, or when this task fires on schedule.
---

# AI Leaders Digest

Generate a daily Chinese-language digest of ~12 AI/tech leaders' recent tweets, using a three-step **isolated reflection** pipeline (Draft → Critique → Refine) that improves analysis quality far beyond a single-shot summary.

**Project root:** `/Users/little_claw/ai-leaders-digest`

## Workflow

Always `cd` to the project root first.

### Step 1 — Refresh tweet data (idempotent)

```bash
cd /Users/little_claw/ai-leaders-digest && python3 fetcher.py fetch
```

This pulls the latest Nitter RSS feeds for all enabled authors and inserts new tweets into `data/ai_leaders.db`. Safe to run repeatedly — duplicates are deduped on `tweet_id`. If the call fails for some sources (Nitter is flaky), continue anyway.

### Step 2 — Build orchestration payload

```bash
python3 digest_generate.py query --days 3 --profile default
```

Returns a JSON object with:
- `meta`: `days`, `date`, `tweet_count`, `sources_ok`, `sources_total`, `tweets_file`, `focus_instructions`
- `prompts.draft`: the Draft prompt with placeholders already filled in
- `prompts.critique_template`: the Critique prompt template (raw, expects `{draft}`)
- `prompts.refine_template`: the Refine prompt template (raw, expects `{draft}` and `{critique}`)

If `tweet_count` is 0, abort and report — no fresh data.

The full tweet bodies are written to `meta.tweets_file` (default `data/latest_tweets_default.json`). The Draft subagent reads this file via `Read`; it must NOT be passed inline as text (too large).

### Step 3 — Draft (subagent #1)

Spawn an Agent with `subagent_type=general-purpose`. Give it:
- **Prompt**: `prompts.draft` returned by Step 2 (already includes the `tweets_file` path inline as plain text instructions)
- **Goal**: produce the initial digest in the format specified by the prompt

Capture the returned draft text. Do not strip or reformat.

### Step 4 — Critique (subagent #2, **ISOLATED**)

This step is the heart of the pipeline. The critique subagent **must not** see the raw tweets — it can only judge the draft on its own merits. This is what catches lazy "X is important because it's important" tautologies that a single-pass writer wouldn't notice.

Spawn an Agent with `subagent_type=general-purpose`. Give it ONLY:
- The Critique template (`prompts.critique_template`) with `{draft}` substituted to the Step 3 output
- **Do NOT mention `tweets_file`, do NOT pass the tweet JSON path, do NOT include any tweet content.** Even if the subagent has Read tool access, it should have no idea what file to look at.

Capture the critique text (which ends with grade A / B / C).

### Step 5 — Refine (subagent #3)

Spawn an Agent with `subagent_type=general-purpose`. Give it:
- The Refine template (`prompts.refine_template`) with `{draft}` and `{critique}` substituted from Steps 3 and 4
- **Goal**: produce the final digest, addressing every critique point

Capture the final text.

### Step 6 — Save to DB

```bash
echo "$FINAL_TEXT" | python3 digest_generate.py save-summary --days 3 --profile default
```

(Use a heredoc or temp file in practice — `echo` mangles multi-line content.)

This appends a row to the `summaries` table with today's date, days_back, tweet_count, sources_ok, sources_total, focus_profile, content, created_at.

### Step 7 — Report

Print briefly:
- Tweet count + sources used
- Critique grade (A / B / C)
- "Saved digest for {date}" or any error

## The three prompt templates (verbatim, embedded for self-containment)

These are duplicated in `digest_generate.py` and produced inside the JSON returned by Step 2 — but kept here so the skill's intent is auditable on its own.

### DRAFT_PROMPT

```
你是一位资深 AI 行业分析师，为 AI/ML 从业者撰写摘要。
你的任务是 INTERPRET（解读），而非简单报道。找出模式，解释意义，连接线索。
中文评论，英文人名/术语。简洁、适合手机阅读。

推文数据存储在 JSON 文件中。请用 read_file 工具读取完整文件：
文件路径：{tweets_file}

文件结构（按作者 handle 分组）：
{
  "<handle>": {
    "name": "<显示名>",
    "category": "<ai-pioneer/tech-leader/startup/...>",
    "tweets": [
      {"date": "<ISO8601>", "text": "<推文正文>", "is_retweet": true/false},
      ...
    ]
  },
  ...
}

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
只输出摘要正文，不要元评论。
```

### CRITIQUE_PROMPT (isolated — no access to original tweets)

```
你是一位犀利的编辑审稿人，负责审阅 AI 行业摘要。
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
最后给出总体质量评分：A（可直接发布）、B（需小修）、C（需大改）。
```

### REFINE_PROMPT

```
你是一位资深 AI 行业分析师，正在制作摘要的最终版本。
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
- 保持相同的格式结构
```

## Why isolated reflection works

A single-pass writer is blind to their own laziness. The Critique subagent, denied access to the source material, is forced to grade the *prose* — not whether the prose covers the source. This catches:
- Tautological "→ 意味着什么" lines that don't actually advance the analysis
- Signal-vs-noise imbalance (every author getting equal weight regardless of news value)
- Format bloat (overly dense / non-mobile-friendly output)

The Refine pass, with both draft and critique in context, produces output that is meaningfully better than the draft — typically tighter, more pointed, with weak insights either strengthened or culled.

Do not collapse the three steps into one — the isolation is the mechanism.
