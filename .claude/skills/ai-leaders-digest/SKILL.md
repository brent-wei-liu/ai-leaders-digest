---
name: ai-leaders-digest
description: >
  Generate a daily Chinese-language digest of AI leaders' public activity from
  RSS/API sources plus web search, via a Research → Extract → Context →
  Summarize pipeline, and email it. Use when the user asks for an AI leaders
  digest, daily AI digest, AI 大佬日报, AI 领袖摘要, or when this task fires on
  schedule.
---

# AI Leaders Digest

Generate a Chinese-language digest of 13 AI/tech leaders' recent public activity. X can no longer be read directly, so content comes from two places: free RSS/API sources configured per author in `sources.json` (`fetcher.py fetch`), and a web search step that covers everyone else (Step 2). Isolated subagents then run the **pipeline** Extract → Context → Summarize, whose job is to faithfully report what each person said and add factual background. No forced pattern-mining.

**Project root:** the directory containing this SKILL.md's grand-grandparent (`<project>/.claude/skills/ai-leaders-digest/SKILL.md`). Always `cd` there first, and start every shell command with `cd <project-root> && ` because the shell's working directory can reset between commands.

## Workflow

### Web tool budget

**Hard cap = 24 calls total.** Tell each subagent its budget explicitly. It is a ceiling, not a target.

| Step | Budget | Use it for |
|------|--------|------------|
| Research (2) | **16** | `extended` searches (standard mode returns almost nothing recent), at least 5 calls kept for opening pages to confirm dates and exact quotes |
| Extract (4) | **0** | sorting only |
| Context (5) | **8** | background on named papers / products / companies in substantive items |
| Summarize (6) | **0** | pure synthesis from the contexted JSON |

### Step 1 — Refresh feed data (idempotent)

```bash
python3 fetcher.py fetch
```

Pulls every source in `sources.json` into `data/ai_leaders.db`, deduped on `tweet_id`. Each source fails soft: the JSON report lists per-source status under `sources`. A non-zero exit means **every** source failed; note it for the report and continue, since web search can still produce a digest.

### Step 2 — Research (subagent #1, web search)

```bash
python3 digest_generate.py research --days 2
```

Returns `meta` and `prompt`. Spawn an Agent with `subagent_type=general-purpose`, passing `prompt` verbatim.

**Append to the prompt**: "You may use WebSearch (mode extended) / WebFetch up to **16 times, hard ceiling**. Return nothing for an author if you find nothing. Never invent a quote, URL or date."

Parse the ```json``` array out of the reply, write it to a temp file and store it:

```bash
TMP_ITEMS=$(mktemp -t ai_leaders_items.XXXXXX.json)
# write the JSON array to "$TMP_ITEMS"
python3 fetcher.py save-items --days 2 < "$TMP_ITEMS"
```

`save-items` enforces the rules: it rejects items with no source URL, no parseable date, a date older than the window or in the future, or an unknown author, and it counts duplicates of stored items (by URL or content) instead of inserting them. It prints `{"accepted", "duplicates", "rejected"}`. Do not resubmit rejected items. If Research fails or returns nothing, continue with what the feeds produced and say so in the report.

### Step 3 — Build orchestration payload

```bash
python3 digest_generate.py query --days 5 --profile default
```

Returns a JSON object with:
- `meta`: `days`, `date`, `tweet_count`, `sources_ok`, `sources_total`, `tweets_file`, `focus_instructions`
- `prompts.extract`: the Extract prompt with placeholders already filled in
- `prompts.context_template`: the Context prompt template (expects `{extracted}`)
- `prompts.summarize_template`: the Summarize prompt template (expects `{contexted}`)

If `tweet_count` is 0 (the command exits non-zero with an `error`), abort and report: no fresh data.

Item bodies are written to `meta.tweets_file` (default `data/latest_tweets_default.json`), each with `url`, `platform` and `source`. The Extract subagent reads this file via `Read`; it must NOT be passed inline (too large). Later subagents do NOT read this file.

Window note: `--days 5` absorbs a quiet day without producing thin output. Web research only looks back 2 days because it runs daily and earlier findings are already stored.

### Step 4 — Extract (subagent #2, reads the items file)

The sorting step. Classifies each item as **substantive** (concrete paper / product / company news / technical detail / strategic decision / reply to another leader / specific workflow result) or **filler** (non-substantive repost, personal life, off-AI politics, generic congrats). It writes no summary and makes no insight claims.

Spawn an Agent with `subagent_type=general-purpose`, prompt = `prompts.extract`.

**Append to the prompt**: "You have **0** web calls."

Capture the JSON from the ```json``` block. The downstream Context step receives this JSON, not the raw file.

### Step 5 — Context (subagent #3, **isolated from raw items**)

Adds factual background to each substantive item: what the referenced paper / product / event actually is, key objective numbers, whether a claimed role is current.

Spawn an Agent with `subagent_type=general-purpose`, prompt = `prompts.context_template` with `{extracted}` replaced by the Step 4 JSON. **Do NOT mention `tweets_file`.** Substitute by plain string replacement, not `str.format`, since the JSON contains braces.

**Append to the prompt**: "You may use WebSearch / WebFetch up to **8 times, hard ceiling**. Spend them on specific papers / products / companies / benchmarks, not on opinions."

Capture the JSON. It has the same structure, with each substantive item gaining `context` and `sources` and keeping `url` / `platform` / `source`.

### Step 6 — Summarize (subagent #4, **no web, no raw items**)

Spawn an Agent with `subagent_type=general-purpose`, prompt = `prompts.summarize_template` with `{contexted}` replaced by the Step 5 JSON. **Do NOT pass `tweets_file` or the Extract JSON.**

**Append to the prompt**: "You have **0** web calls. All facts come from the contexted JSON. If something is missing from the JSON, omit it — don't invent."

Capture the final text. Every highlight and bullet must link its source with the platform label the prompt specifies; web items are written as third-party reports ("据 X，……"). If items came back without source links, send it back once to fix them.

### Step 7 — Save to DB (MANDATORY — must succeed before Step 8)

If this fails, do NOT proceed to Step 8 unless the orphan fallback below has written the digest to disk.

```bash
TMP=$(mktemp -t ai_leaders_digest.XXXXXX.md)
# write the final text to "$TMP"
python3 digest_generate.py save-summary --days 5 --profile default < "$TMP"
# expect {"saved": true, "date": "YYYY-MM-DD", ...}
```

Then **verify** the row landed:

```bash
NEW_ID=$(sqlite3 data/ai_leaders.db \
  "SELECT id FROM summaries WHERE date='$(date -u +%Y-%m-%d)' ORDER BY id DESC LIMIT 1")
test -n "$NEW_ID" || { echo "VERIFY FAILED: no row for today"; exit 1; }
echo "saved as summary id=$NEW_ID"
```

**Fallback on failure** (DB locked / corrupted / disk full / verify came back empty): write the markdown to `data/orphan_digest_<YYYY-MM-DD>.md` and continue to Step 8 with `--orphan`.

```bash
# only if save-summary or VERIFY above failed
DATE=$(date -u +%Y-%m-%d)
cp "$TMP" "data/orphan_digest_${DATE}.md"
```

### Step 8 — Send email via SMTP

The sender sends immediately, with no review step. Each digest date goes out once: a repeat run prints `{"sent": false, "skipped": true, ...}` and exits 0 — treat that as delivered, create no draft, and don't pass `--force` unless the user asked for a resend. `save-summary` dates rows in UTC, so use the UTC date here:

```bash
DATE=$(date -u +%Y-%m-%d)
python3 email_sender.py --date "$DATE"
# orphan: python3 email_sender.py --date "$DATE" --file "data/orphan_digest_${DATE}.md" --orphan
```

It reads `DIGEST_RECIPIENT`, `GMAIL_SMTP_USER` and `GMAIL_APP_PASSWORD` from `<project>/.env`. Never print the password. Subject: `AI Leaders Digest YYYY-MM-DD` (with a `[ORPHAN: not in DB] ` prefix for an orphan).

**On a non-zero exit, don't retry.** Keep the error and fall back to a Gmail draft: render with `python3 email_sender.py --date "$DATE" --dry-run --html-out <path>`, then call the Gmail MCP `create_draft` tool with `to` = `DIGEST_RECIPIENT`, the same subject, `body` = the markdown and `htmlBody` = the rendered HTML. If the Gmail MCP is unavailable, report the failure.

### Step 9 — Report

Print briefly:
- Feed sources ok/total + new items; failing sources by name
- Research: web calls used, accepted / duplicates / rejected
- Per-step output: extract author-count, context substantive-count + web calls used, summarize char-count
- Saved summary id (or `[ORPHAN]: ...` reason)
- Delivery status (sent / skipped as already sent / SMTP error + draft id or draft failure)

## Prompt templates

The prompts are defined in `digest_generate.py` (`RESEARCH_PROMPT`, `EXTRACT_PROMPT`, `CONTEXT_PROMPT`, `SUMMARIZE_PROMPT`) and emitted by the `research` and `query` commands. They are not duplicated here — single source of truth.

## Why the steps are isolated

- **Sorting** is its own step, with no analysis pressure.
- **Fact-grounding** fetches the context an item points at, without seeing the raw pool.
- **Writing** has no analytic mandate beyond faithful transcription. If the data is thin, the digest is short. That's honest, not a failure.
- **Research** output passes through `save-items`, so the URL, date and window rules are enforced by code rather than trusted to the model. Exact quotes can't be checked by code, which is why the prompts forbid unverified ones.
