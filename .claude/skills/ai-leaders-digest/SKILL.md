---
name: ai-leaders-digest
description: >
  Generate a daily Chinese-language digest of AI leaders' tweets via a
  three-step Extract → Context → Summarize pipeline. Use when the user
  asks for an AI leaders digest, daily AI digest, AI 大佬日报, AI 领袖摘要,
  or when this task fires on schedule.
---

# AI Leaders Digest

Generate a Chinese-language digest of ~12 AI/tech leaders' recent tweets, using a three-step **pipeline** (Extract → Context → Summarize) with isolated subagents and explicit per-step responsibilities.

This **replaces** the older Draft → Critique → Refine reflection design. The old design forced the Critique step to invent cross-author "insights" on thin tweet days, which produced strained patterns and hollow "→ 意味着什么" lines. The new design's job is to faithfully summarize what each person said and add factual background — no forced pattern-mining, no editorial cuts disguised as critique.

**Project root:** the directory containing this SKILL.md's grand-grandparent (`<project>/.claude/skills/ai-leaders-digest/SKILL.md`). The scheduled-task wrapper or invoking session should `cd` there before running any of the steps below.

## Workflow

Always `cd` to the project root first.

### Web tool budget (applies across Steps 3 / 4 / 5)

Each subagent may use `WebSearch` and `WebFetch` to verify or enrich content. **Hard cap = 14 calls total across all three steps**, weighted toward Context which is the real fact-grounding step:

| Step | Budget | Use it for |
|------|--------|------------|
| Extract (3) | **2** | only when an unfamiliar named entity in a tweet makes substantive-vs-filler classification ambiguous — NOT for fact-checking |
| Context (4) | **12** | per-substantive-tweet background lookup: find the abstract of a referenced paper, the product page of a launched product, the original thread of a reply, the current role of a person mentioned |
| Summarize (5) | **0** | pure synthesis from the contexted JSON — no new lookups |

When spawning each subagent, tell it explicitly: "you have N web tool calls available — use them on the highest-value facts first." Do not exceed the per-step budget. The budget is a hard ceiling, not a target — using fewer calls is fine.

### Step 1 — Refresh tweet data (idempotent)

```bash
cd <project-root> && python3 fetcher.py fetch
```

This pulls the latest Nitter RSS feeds for all enabled authors and inserts new tweets into `data/ai_leaders.db`. Safe to run repeatedly — duplicates are deduped on `tweet_id`. If the call fails for some sources (Nitter is flaky), continue anyway.

### Step 2 — Build orchestration payload

```bash
python3 digest_generate.py query --days 5 --profile default
```

Returns a JSON object with:
- `meta`: `days`, `date`, `tweet_count`, `sources_ok`, `sources_total`, `tweets_file`, `focus_instructions`
- `prompts.extract`: the Extract prompt with placeholders already filled in (`tweets_file`, `days`, `date`, counts, focus_instructions)
- `prompts.context_template`: the Context prompt template (raw, expects `{extracted}`)
- `prompts.summarize_template`: the Summarize prompt template (already templated with `date`, counts; expects `{contexted}`)

If `tweet_count` is 0, abort and report — no fresh data.

The full tweet bodies are written to `meta.tweets_file` (default `data/latest_tweets_default.json`). The Extract subagent reads this file via `Read`; it must NOT be passed inline as text (too large). Subsequent subagents (Context, Summarize) do NOT read this file — they work from the JSON produced by the previous step.

Window note: `--days 5` (not 3). Three days is too narrow when a single quiet day reduces the signal pool to two days. Five days gives enough buffer to absorb a quiet day without producing thin output.

### Step 3 — Extract (subagent #1, reads raw tweets file)

This is the sorting step. The subagent reads the raw tweets and classifies each one as **substantive** (concrete paper / product / company news / technical detail / strategic decision / reply to another leader / specific workflow result) or **filler** (retweet of a non-substantive item, personal life, off-AI politics, generic congrats, etc.).

It does NOT write any summary. It does NOT make insight claims. It only sorts.

Spawn an Agent with `subagent_type=general-purpose`. Give it:
- **Prompt**: `prompts.extract` returned by Step 2 (already includes the `tweets_file` path and structure docs inline)
- **Goal**: emit a strict JSON block grouped by author, with `substantive` array + `filler_count` + `filler_summary` per author

**Append to the prompt**: "You may use WebSearch up to **2 times** if an unfamiliar entity in a tweet makes substantive-vs-filler classification ambiguous. Do NOT use web for fact-checking — that is the Context step's job. If you don't need the budget, don't use it."

Capture the JSON output. Parse the ```json``` code block out — the subagent's reply may wrap it in narration. The downstream Context step receives this JSON, not the raw tweets file.

### Step 4 — Context (subagent #2, **isolated from raw tweets**)

The Context subagent never sees `tweets_file`. It only sees the JSON produced by Extract. Its job is to add **factual background** to each substantive tweet — what the referenced paper / product / event actually is, key objective numbers if any, whether a person's claimed role is current. This is real fact-grounding, replacing the old Critique step's prose-quality nits.

Spawn an Agent with `subagent_type=general-purpose`. Give it:
- The Context template (`prompts.context_template`) with `{extracted}` substituted to the Step 3 JSON output (the parsed code block content)
- **Do NOT mention `tweets_file`, do NOT pass the tweet JSON path, do NOT include the original tweet content beyond what's in the extracted JSON.**

**Append to the prompt**: "You may use WebSearch / WebFetch up to **12 times, hard ceiling**. Spend more on tweets that name a specific paper / product / company / benchmark; spend less or zero on abstract opinions. Don't burn calls fact-checking author opinions — fact-check named entities and concrete claims."

Capture the JSON output. The subagent should return the same structure as Extract, with each substantive item now carrying `context` (1-3 Chinese sentences) and `sources` (URL list, possibly empty).

### Step 5 — Summarize (subagent #3, **no web, no raw tweets**)

The Summarize subagent produces the final Chinese digest. It reads only the JSON from Context. It does NOT do web lookups. It is **not** allowed to invent cross-author "insights" or "→ 意味着什么" lines — that's exactly the failure mode the old Critique step amplified. Its mandate is faithful transcription + background.

Spawn an Agent with `subagent_type=general-purpose`. Give it:
- The Summarize template (`prompts.summarize_template`) with `{contexted}` substituted to the Step 4 JSON output
- **Do NOT pass `tweets_file` or the raw extracted JSON.**

**Append to the prompt**: "You have **0** web calls. All facts come from the contexted JSON. If something is missing from JSON, omit it — don't invent."

Capture the final text. This is the digest that goes to Step 6 (save) and Step 7 (Gmail draft).

### Step 6 — Save to DB (MANDATORY — must succeed before Step 7)

This step is **not optional**. If it fails, the whole pipeline must stop and surface the error — do NOT proceed to Step 7 (Gmail draft) on a save failure, or the recipient gets a digest that exists nowhere on disk authoritatively.

Write the final text to a temp file (heredoc-safe), then pipe into save-summary. Capture the JSON return.

```bash
TMP=$(mktemp -t ai_leaders_digest.XXXXXX.md)
printf '%s' "$FINAL_TEXT" > "$TMP"
SAVE_RESULT=$(python3 digest_generate.py save-summary --days 5 --profile default < "$TMP")
echo "$SAVE_RESULT"   # expect {"saved": true, "date": "YYYY-MM-DD", ...}
```

Then **verify** the row landed by reading back the latest summary id for today:

```bash
NEW_ID=$(sqlite3 data/ai_leaders.db \
  "SELECT id FROM summaries WHERE date='$(date -u +%Y-%m-%d)' ORDER BY id DESC LIMIT 1")
test -n "$NEW_ID" || { echo "VERIFY FAILED: no row for today"; exit 1; }
echo "saved as summary id=$NEW_ID"
```

**Fallback on failure** (DB locked / corrupted / disk full / verify came back empty): write the markdown to `data/orphan_digest_<YYYY-MM-DD>.md` and continue to Step 7 BUT prepend `[ORPHAN: not in DB] ` to the email subject so the user notices the archival gap.

```bash
# only if save-summary or VERIFY above failed
DATE=$(date -u +%Y-%m-%d)
cp "$TMP" "data/orphan_digest_${DATE}.md"
ORPHAN=1   # set this flag, Step 7 reads it to mutate the subject
```

### Step 7 — Create Gmail draft

Use the Gmail MCP `create_draft` tool to create (not send) a draft email containing the final digest. The user reviews in Gmail and sends manually.

**Convert the digest markdown to HTML before calling `create_draft`** — Gmail's UI does not render markdown, so passing raw markdown shows it as source. Pass BOTH parameters:

- `to`: read `DIGEST_RECIPIENT` from `<project>/.env` (parse `KEY=VALUE` lines, value of `DIGEST_RECIPIENT`). If missing, abort with a clear error.
- `subject`: `AI Leaders Digest YYYY-MM-DD` — but if Step 6 set `ORPHAN=1`, prepend `[ORPHAN: not in DB] ` so the user knows the digest didn't land in `summaries`.
- `body`: the plain markdown text (fallback for non-HTML clients)
- `htmlBody`: the rendered HTML (used by Gmail web/mobile)

Renderer requirements:
- Headings (`#` / `##` / `###`) → `<h1>` / `<h2>` / `<h3>`
- Bullet lists (`- ` or `* `) → `<ul><li>`
- Bold (`**text**`) → `<strong>`
- Inline code (`` `x` ``) → `<code>`
- Links `[text](url)` → `<a href="url">text</a>`
- Blank lines separate paragraphs (`<p>`)
- HTML-escape the source first to prevent injection from any tweet content

Wrap the HTML in a minimal shell with inline styles for readability:
```html
<!DOCTYPE html><html><body style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:680px;margin:auto;padding:20px;line-height:1.5;color:#222;">
  ...rendered content...
</body></html>
```

Prefer Python's `markdown` library if available (`python3 -c "import markdown; print(markdown.markdown(open('<file>').read()))"`). Otherwise inline a minimal converter:

```python
import html, re
def md_to_html(md):
    safe = html.escape(md)
    out, in_list, para = [], False, []
    bold = re.compile(r'\*\*(.+?)\*\*')
    code = re.compile(r'`([^`]+?)`')
    link = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')
    def inline(s):
        s = bold.sub(r'<strong>\1</strong>', s)
        s = code.sub(r'<code>\1</code>', s)
        s = link.sub(r'<a href="\2">\1</a>', s)
        return s
    def flush_para():
        nonlocal para
        if para:
            out.append('<p>' + inline(' '.join(para).strip()) + '</p>')
            para = []
    def close_list():
        nonlocal in_list
        if in_list: out.append('</ul>'); in_list = False
    for line in safe.split('\n'):
        s = line.strip()
        m = re.match(r'^(#{1,3})\s+(.+)$', s)
        if m:
            flush_para(); close_list()
            lvl = len(m.group(1))
            out.append(f'<h{lvl}>{inline(m.group(2))}</h{lvl}>')
        elif s.startswith('- ') or s.startswith('* '):
            flush_para()
            if not in_list: out.append('<ul>'); in_list = True
            out.append(f'<li>{inline(s[2:])}</li>')
        elif not s:
            flush_para(); close_list()
        else:
            close_list(); para.append(s)
    flush_para(); close_list()
    return ('<!DOCTYPE html><html><body style="font-family:-apple-system,'
            'BlinkMacSystemFont,Segoe UI,sans-serif;max-width:680px;'
            'margin:auto;padding:20px;line-height:1.5;color:#222;">'
            + '\n'.join(out) + '</body></html>')
```

If the Gmail MCP isn't available, surface that to the user — do not silently fall back to anything else.

### Step 8 — Report

Print briefly:
- Tweet count + sources used
- Per-step output: extract author-count, context substantive-count + web calls used, summarize char-count
- "Saved digest for {date}" + draft status (created with id `<id>` / Gmail MCP unavailable / error)

## Prompt templates

The three prompt strings are defined in `digest_generate.py` (`EXTRACT_PROMPT`, `CONTEXT_PROMPT`, `SUMMARIZE_PROMPT`) and emitted via `query` in Step 2's JSON under `prompts.extract` / `prompts.context_template` / `prompts.summarize_template`. They are not duplicated here — single source of truth.

Their high-level intent, for auditability:

**EXTRACT** — Input: raw tweets JSON file path. Output: same author-grouped structure but each tweet is now in either a `substantive[]` array (with a `tag` for paper / product / company / strategy / reply / workflow) or counted into `filler_count` with a one-line `filler_summary`. The subagent does NOT write any summary, only sorts. Web budget 2 (rarely needed; for ambiguous entities only).

**CONTEXT** — Input: the Extract JSON only (no raw tweets). Output: same structure with each substantive item gaining a `context` (1-3 Chinese sentences of factual background) and `sources` (URL list). The subagent does NOT re-classify. Web budget 12 (heavy lookups: paper abstracts, product pages, original threads of replies, role confirmations).

**SUMMARIZE** — Input: the Context JSON only (no raw tweets, no web). Output: a four-section Chinese digest with the header, **本期亮点** (2-3 high-density items), **逐人小记** (one paragraph per substantive author, faithful transcription + context — no forced strategic poses or "意味着什么"), and **🐕 沉默的狗** (enabled authors with 0 tweets in the window). Length not enforced — writes as long or short as the contexted JSON warrants.

## Why this pipeline replaces Draft → Critique → Refine

The old Critique step was tasked with grading "insight quality" of a draft it couldn't trace back to source. On thin tweet days that meant the Refine step rewrote with **harder pattern claims** to placate the critique — which produced strained "→ 意味着什么" lines that weren't actually in the data. The output read sharper but was less faithful.

The new pipeline factors the work cleanly:
- **Sorting** is its own step. No analysis pressure.
- **Fact-grounding** is its own step. Replaces "fact-checking the draft" with "actually fetching the context the tweet pointed at."
- **Writing** has no analytic mandate beyond faithful transcription. If the data is thin, the digest is short. That's honest, not a failure.

Isolation between steps is preserved (Context can't see raw tweets; Summarize can't see raw tweets and can't do web). The mechanism that made the old design work — denying each step access to upstream raw material — is kept; the failure mode (forced insight on thin data) is removed.
