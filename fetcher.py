#!/usr/bin/env python3
"""
AI Leaders source fetcher with SQLite persistence.

Authors and their per-author sources live in sources.json. Everything here is
stdlib only and spends no LLM tokens; web search findings arrive via save-items.

Usage:
  python3 fetcher.py fetch                 # Fetch all configured sources → store in DB
  python3 fetcher.py save-items [--days 2] # Store web-search findings (JSON array on stdin)
  python3 fetcher.py query [days] [--author X] [--category Y] [--profile Z]
                                           # Query items from DB, output JSON
  python3 fetcher.py save-summary          # Save summary text from stdin
  python3 fetcher.py authors               # List all authors
  python3 fetcher.py profiles              # List all focus profiles
  python3 fetcher.py add-profile <name> <json>  # Add a focus profile
  python3 fetcher.py subscribers           # List subscribers
  python3 fetcher.py add-subscriber <phone> [name] [profile]
  python3 fetcher.py remove-subscriber <phone>
  python3 fetcher.py toggle-subscriber <phone>  # Enable/disable
  python3 fetcher.py stats [days]          # Quick stats
"""

import difflib
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

DB_PATH = os.environ.get(
    "RSS_DB_PATH",
    str(Path(__file__).resolve().parent / "data" / "ai_leaders.db"),
)

SOURCES_PATH = os.environ.get(
    "AI_LEADERS_SOURCES",
    str(Path(__file__).resolve().parent / "sources.json"),
)

BLUESKY_FEED_API = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
BLUESKY_LIMIT = 50
HTTP_TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (compatible; RSS-Reader/1.0)"
MAX_TEXT_CHARS = 2000
DEDUPE_LOOKBACK_DAYS = 30
SIMILAR_TEXT_RATIO = 0.9
MIN_CONTAINED_TEXT_CHARS = 40

DEFAULT_PROFILES = [
    ("default", "均衡关注所有人", json.dumps({
        "focus_authors": [],
        "focus_categories": [],
        "focus_weight": 1,
        "focus_instructions": "",
        "others": "normal",
        "max_summary_length": "medium"
    })),
    ("karpathy", "重点关注 Andrej Karpathy", json.dumps({
        "focus_authors": ["karpathy"],
        "focus_categories": [],
        "focus_weight": 2,
        "focus_instructions": "重点分析 Karpathy 的技术观点、代码和项目动态，对比其他人的相关看法",
        "others": "brief",
        "max_summary_length": "medium"
    })),
    ("ai-tech", "重点关注 AI 技术动态", json.dumps({
        "focus_authors": ["karpathy", "AndrewYNg"],
        "focus_categories": ["ai-engineering", "ai-pioneer"],
        "focus_weight": 2,
        "focus_instructions": "重点分析技术细节、论文、开源项目和工程实践",
        "others": "brief",
        "max_summary_length": "long"
    })),
    ("founders", "重点关注创业和行业", json.dumps({
        "focus_authors": ["paulg", "sama"],
        "focus_categories": ["startup", "ai-industry"],
        "focus_weight": 2,
        "focus_instructions": "重点分析创业建议、融资动态、行业趋势",
        "others": "brief",
        "max_summary_length": "medium"
    })),
]


# ── Database ──────────────────────────────────────────────────────────

def get_db():
    """Get or create the database connection."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # TRUNCATE (not DELETE): the data/ dir is a virtiofs mount that denies
    # file unlink ("Operation not permitted"). DELETE mode must remove the
    # rollback journal on every commit, which fails and surfaces as
    # "disk I/O error" — and a leftover hot journal then blocks all opens.
    # TRUNCATE is still crash-safe but truncates the journal in place instead
    # of unlinking it, which this mount allows.
    conn.execute("PRAGMA journal_mode=TRUNCATE")
    # 5s wait when a concurrent writer (api.py) holds the lock — without this
    # the default 0 makes every overlap a hard SQLITE_BUSY failure.
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn):
    """Create tables and seed data if needed."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS authors (
            handle      TEXT PRIMARY KEY,
            name        TEXT,
            category    TEXT,
            rss_url     TEXT,
            enabled     INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS tweets (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            author      TEXT NOT NULL REFERENCES authors(handle),
            tweet_id    TEXT,  -- unique per author: idx_tweets_author_tweet_id
            date        TEXT NOT NULL,
            text        TEXT NOT NULL,
            url         TEXT,
            is_retweet  INTEGER DEFAULT 0,
            fetched_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS summaries (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT NOT NULL,
            days_back       INTEGER DEFAULT 3,
            tweet_count     INTEGER,
            sources_ok      INTEGER,
            sources_total   INTEGER,
            focus_profile   TEXT DEFAULT 'default',
            content         TEXT NOT NULL,
            created_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS focus_profiles (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT UNIQUE NOT NULL,
            description TEXT,
            rules       TEXT NOT NULL,
            is_default  INTEGER DEFAULT 0,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS subscribers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT,
            phone       TEXT,
            email       TEXT,
            channel     TEXT DEFAULT 'email',
            profile     TEXT DEFAULT 'default',
            enabled     INTEGER DEFAULT 1,
            created_at  TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_tweets_author_date ON tweets(author, date);
        CREATE INDEX IF NOT EXISTS idx_summaries_date ON summaries(date);
    """)

    ensure_source_columns(conn)

    # Seed authors. OR IGNORE (not "only if empty") so an author added to
    # sources.json later also lands in an existing DB; rows already there
    # (including their enabled flag) are left untouched.
    conn.executemany(
        "INSERT OR IGNORE INTO authors (handle, name, category) VALUES (?, ?, ?)",
        [(a["handle"], a["name"], a["category"]) for a in load_author_config()],
    )
    # rss_url is unused now that sources.json owns sources; clear the dead values.
    conn.execute("UPDATE authors SET rss_url = NULL WHERE rss_url IS NOT NULL")

    # Seed profiles if empty
    if conn.execute("SELECT COUNT(*) FROM focus_profiles").fetchone()[0] == 0:
        now = datetime.now(timezone.utc).isoformat()
        conn.executemany(
            "INSERT INTO focus_profiles (name, description, rules, created_at) VALUES (?, ?, ?, ?)",
            [(n, d, r, now) for n, d, r in DEFAULT_PROFILES],
        )
        # Mark default
        conn.execute("UPDATE focus_profiles SET is_default = 1 WHERE name = 'default'")

    conn.commit()


def ensure_source_columns(conn):
    """Additive migration: where each row came from. Legacy rows are X tweets."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tweets)").fetchall()}
    for col, typedef in [
        ("platform", "TEXT DEFAULT 'x'"),
        ("source_name", "TEXT DEFAULT NULL"),
    ]:
        if col not in cols:
            conn.execute(f"ALTER TABLE tweets ADD COLUMN {col} {typedef}")
    # Per author, not global: a repost puts another author's post id in this
    # author's feed. Restored DB copies lost the original UNIQUE(tweet_id); one
    # that still holds duplicates gets a plain index and relies on the
    # existence check in store_items until it is deduped.
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tweets_author_tweet_id "
                     "ON tweets(author, tweet_id)")
        conn.execute("DROP INDEX IF EXISTS idx_tweets_tweet_id")
    except sqlite3.IntegrityError:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tweets_tweet_id ON tweets(tweet_id)")


def load_author_config():
    with open(SOURCES_PATH, encoding="utf-8") as f:
        return json.load(f)


# ── Source Fetching ───────────────────────────────────────────────────

def parse_date(date_str):
    """Parse a feed or user-supplied date; returns an aware UTC datetime or None."""
    if not date_str:
        return None
    date_str = date_str.strip()
    parsed = None
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S GMT",
        "%d %b %Y %H:%M %z",  # Bluesky profile RSS
    ):
        try:
            parsed = datetime.strptime(date_str, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def clean_html(raw):
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw or "")
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h[1-6]>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\s*\n\s*", "\n", text).strip()


def compose_text(title, body):
    """Title + body, capped. Titles without letters or digits (e.g. "-") are noise."""
    title = clean_html(title)
    if not re.search(r"\w", title) or body.startswith(title):
        title = ""
    text = f"{title}\n{body}".strip() if title else body
    return text[:MAX_TEXT_CHARS]


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read()


def parse_feed(content):
    """Parse RSS 2.0 or Atom bytes into raw items (id, url, date, text)."""
    atom = "{http://www.w3.org/2005/Atom}"
    root = ET.fromstring(content)
    items = []
    if root.tag == f"{atom}feed":
        for entry in root.findall(f"{atom}entry"):
            links = entry.findall(f"{atom}link")
            alternate = [l for l in links if l.get("rel", "alternate") == "alternate"]
            link = alternate[0].get("href", "") if alternate else ""
            body = entry.findtext(f"{atom}content") or entry.findtext(f"{atom}summary") or ""
            # published, not updated: Atom feeds may bump updated on every render.
            date = entry.findtext(f"{atom}published") or entry.findtext(f"{atom}updated")
            items.append({
                "id": entry.findtext(f"{atom}id") or link,
                "url": link.strip(),
                "date": parse_date(date),
                "text": compose_text(entry.findtext(f"{atom}title", ""), clean_html(body)),
            })
        return items

    channel = root.find("channel")
    if channel is None:
        raise ValueError("neither an RSS channel nor an Atom feed")
    for item in channel.findall("item"):
        link = item.findtext("link", "").strip()
        body = (item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
                or item.findtext("description") or "")
        date = item.findtext("pubDate") or item.findtext("{http://purl.org/dc/elements/1.1/}date")
        items.append({
            "id": item.findtext("guid") or link,
            "url": link,
            "date": parse_date(date),
            "text": compose_text(item.findtext("title", ""), clean_html(body)),
        })
    return items


def fetch_feed_source(source):
    items = parse_feed(http_get(source["url"]))
    include = source.get("include_url")
    if include:
        items = [i for i in items if re.search(include, i["url"])]
    rewrite = source.get("link_rewrite")
    for item in items:
        if rewrite and item["url"].startswith(rewrite["from"]):
            item["url"] = rewrite["to"] + item["url"][len(rewrite["from"]):]
        item["id"] = f"feed:{item['id']}"
        item["is_retweet"] = 1 if item["text"].startswith("RT @") else 0
    return items


def fetch_bluesky_source(source):
    query = urllib.parse.urlencode({"actor": source["actor"], "limit": BLUESKY_LIMIT})
    data = json.loads(http_get(f"{BLUESKY_FEED_API}?{query}"))
    items = []
    for entry in data.get("feed", []):
        post = entry["post"]
        record = post.get("record", {})
        text = record.get("text", "").strip()
        external = (post.get("embed") or {}).get("external", {}).get("uri")
        if external and external not in text:
            text = f"{text}\n{external}".strip()
        items.append({
            "id": f"bsky:{post['uri']}",
            "url": f"https://bsky.app/profile/{post['author']['handle']}/post/{post['uri'].rsplit('/', 1)[-1]}",
            "date": parse_date(record.get("createdAt")),
            "text": text[:MAX_TEXT_CHARS],
            "is_retweet": 1 if "reason" in entry or text.startswith("RT @") else 0,
        })
    return items


SOURCE_FETCHERS = {
    "feed": fetch_feed_source,
    "bluesky": fetch_bluesky_source,
}


def fetch_source(source):
    """Fetch one configured source. Raises on any failure; the caller fails soft."""
    fetcher = SOURCE_FETCHERS.get(source.get("type"))
    if fetcher is None:
        raise ValueError(f"unknown source type {source.get('type')!r}")
    return fetcher(source)


def store_items(conn, handle, source, items, now):
    """Insert fetched items; undated, unlinked and empty ones are skipped. Returns (new, dupes)."""
    new = dupes = 0
    for item in items:
        if not item["date"] or not item["url"] or not item["text"]:
            continue
        if conn.execute("SELECT 1 FROM tweets WHERE author = ? AND tweet_id = ?",
                        (handle, item["id"])).fetchone():
            dupes += 1
            continue
        conn.execute(
            """INSERT INTO tweets
               (author, tweet_id, date, text, url, is_retweet, fetched_at, platform, source_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (handle, item["id"], item["date"].isoformat(), item["text"], item["url"],
             item["is_retweet"], now, source.get("platform", "feed"), source.get("label")),
        )
        new += 1
    return new, dupes


def cmd_fetch(conn, args=None):
    """Fetch every configured source of every enabled author and store the items.

    --report-hour H : only output full report if current local hour == H.
                      Otherwise output minimal JSON with "report": false.

    Entry guard: bail out before writing if integrity_check fails — writing
    fresh rows into an already-corrupt index would make the eventual
    dump+restore lossier and bury the original failure point in noise.
    """
    report_hour = None
    if args:
        for i, a in enumerate(args):
            if a == "--report-hour" and i + 1 < len(args):
                report_hour = int(args[i + 1])

    # Cheap pre-flight: PRAGMA integrity_check stops at the first failure on
    # a healthy DB and takes <100ms here. If we abort on corruption, an
    # operator gets paged within hours instead of waiting weeks for
    # downstream queries to surface it.
    try:
        check = conn.execute("PRAGMA integrity_check").fetchone()
        check_result = check[0] if check else "missing"
    except sqlite3.DatabaseError as e:
        check_result = f"error: {e}"
    if check_result != "ok":
        sys.stderr.write(
            f"FATAL: ai_leaders.db PRAGMA integrity_check returned "
            f"{check_result!r}; aborting fetch to avoid compounding "
            f"corruption. Run dump+restore before next fetch.\n"
        )
        sys.exit(1)

    enabled = {r["handle"] for r in conn.execute("SELECT handle FROM authors WHERE enabled = 1")}
    now = datetime.now(timezone.utc).isoformat()
    stats = {"total": 0, "ok": 0, "failed": 0, "new_items": 0, "dupes": 0,
             "failed_names": [], "sources": []}

    for author in load_author_config():
        if author["handle"] not in enabled:
            continue
        for source in author.get("sources", []):
            stats["total"] += 1
            label = source.get("label") or source.get("url") or source.get("actor")
            report = {"author": author["handle"], "source": label}
            try:
                items = fetch_source(source)
                new, dupes = store_items(conn, author["handle"], source, items, now)
                # Commit per source so the write lock is released promptly; a
                # later failure or kill leaves earlier sources' rows intact.
                conn.commit()
            except Exception as e:  # fail soft: one bad source must not stop the rest
                conn.rollback()
                stats["failed"] += 1
                stats["failed_names"].append(f"{author['name']}: {label}")
                report.update(status="failed", error=f"{type(e).__name__}: {e}")
            else:
                stats["ok"] += 1
                stats["new_items"] += new
                stats["dupes"] += dupes
                dates = [i["date"] for i in items if i["date"]]
                report.update(status="ok", new=new, dupes=dupes,
                              newest=max(dates).isoformat() if dates else None)
            stats["sources"].append(report)
            time.sleep(0.5)

    # Determine if we should output a full report
    import zoneinfo
    local_hour = datetime.now(zoneinfo.ZoneInfo("America/Los_Angeles")).hour

    if report_hour is not None:
        stats["report"] = (local_hour == report_hour)
    else:
        stats["report"] = True  # always report if no --report-hour flag

    print(json.dumps(stats, ensure_ascii=False, indent=2))

    # Partial failure is tolerated; every source failing is not — exit non-zero
    # so the scheduled run surfaces it instead of reporting a quiet "0 new items".
    if stats["total"] and stats["ok"] == 0:
        sys.stderr.write(f"FATAL: all {stats['total']} sources failed.\n")
        sys.exit(1)


# ── Web Findings ──────────────────────────────────────────────────────

TRACKING_PARAMS = re.compile(r"^(utm_.*|fbclid|gclid|ref|ref_src|s|t)$", re.I)


def canonical_url(url):
    """Normalise a URL for comparison: scheme, host case, www, fragment, tracking."""
    parts = urllib.parse.urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    query = urllib.parse.urlencode(
        [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if not TRACKING_PARAMS.match(k)]
    )
    return f"{host}{parts.path.rstrip('/')}" + (f"?{query}" if query else "")


def normalize_text(text):
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def is_duplicate(url, text, existing):
    """True if url or text matches an existing (url, text) row of this author."""
    canon = canonical_url(url)
    norm = normalize_text(text)
    for other_url, other_text in existing:
        if other_url and canonical_url(other_url) == canon:
            return True
        other = normalize_text(other_text or "")
        if not other:
            continue
        shorter, longer = sorted((norm, other), key=len)
        if len(shorter) >= MIN_CONTAINED_TEXT_CHARS and shorter in longer:
            return True
        if norm == other or difflib.SequenceMatcher(None, norm, other).ratio() >= SIMILAR_TEXT_RATIO:
            return True
    return False


def validate_web_item(raw, handles, cutoff, now):
    """Return (clean_item, None) or (None, rejection_reason)."""
    if not isinstance(raw, dict):
        return None, "not an object"
    handle = handles.get(str(raw.get("author", "")).lstrip("@").lower())
    if not handle:
        return None, "unknown author"
    url = str(raw.get("url") or "").strip()
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None, "missing or invalid source url"
    text = str(raw.get("text") or "").strip()
    if not text:
        return None, "empty text"
    date = parse_date(str(raw.get("date") or ""))
    if not date:
        return None, "missing or unparseable date"
    if date < cutoff:
        return None, "older than window"
    if date > now + timedelta(days=1):
        return None, "date in the future"
    source_name = str(raw.get("source_name") or "").strip() or parts.netloc.lower().removeprefix("www.")
    return {"author": handle, "url": url, "text": text[:MAX_TEXT_CHARS],
            "date": date, "source_name": source_name}, None


def cmd_save_items(conn, args):
    """Store web-search findings from a JSON array on stdin.

    Each item: {"author": handle, "date": ISO date, "url": source URL,
    "text": what they said or did, "source_name": publication}. Items without
    a URL or date, older than --days, or duplicating a stored item are rejected.
    """
    days = 2
    if "--days" in args:
        days = int(args[args.index("--days") + 1])
    try:
        payload = json.loads(sys.stdin.read())
    except ValueError as e:
        print(json.dumps({"error": f"invalid JSON on stdin: {e}"}))
        sys.exit(1)
    if not isinstance(payload, list):
        print(json.dumps({"error": "expected a JSON array"}))
        sys.exit(1)

    now = datetime.now(timezone.utc)
    # Whole days: a date-only item from the oldest day of the window still counts.
    cutoff = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    lookback = (now - timedelta(days=DEDUPE_LOOKBACK_DAYS)).isoformat()
    handles = {r["handle"].lower(): r["handle"]
               for r in conn.execute("SELECT handle FROM authors WHERE enabled = 1")}

    accepted, duplicates, rejected = 0, 0, []
    for index, raw in enumerate(payload):
        item, reason = validate_web_item(raw, handles, cutoff, now)
        if not item:
            rejected.append({"index": index, "reason": reason})
            continue
        existing = conn.execute(
            "SELECT url, text FROM tweets WHERE author = ? AND date >= ?",
            (item["author"], lookback),
        ).fetchall()
        if is_duplicate(item["url"], item["text"], [(r["url"], r["text"]) for r in existing]):
            duplicates += 1
            continue
        key = f"{item['author']}|{canonical_url(item['url'])}"
        conn.execute(
            """INSERT INTO tweets
               (author, tweet_id, date, text, url, is_retweet, fetched_at, platform, source_name)
               VALUES (?, ?, ?, ?, ?, 0, ?, 'web', ?)""",
            (item["author"], f"web:{hashlib.sha1(key.encode()).hexdigest()[:16]}",
             item["date"].isoformat(), item["text"], item["url"], now.isoformat(),
             item["source_name"]),
        )
        accepted += 1
    conn.commit()
    print(json.dumps({"accepted": accepted, "duplicates": duplicates, "rejected": rejected},
                     ensure_ascii=False))


# ── Querying ──────────────────────────────────────────────────────────

def cmd_query(conn, args):
    """Query items and output JSON for LLM summarization."""
    days = 3
    author_filter = None
    category_filter = None
    profile_name = "default"

    i = 0
    while i < len(args):
        if args[i] == "--author":
            author_filter = args[i + 1]
            i += 2
        elif args[i] == "--category":
            category_filter = args[i + 1]
            i += 2
        elif args[i] == "--profile":
            profile_name = args[i + 1]
            i += 2
        elif args[i].isdigit():
            days = int(args[i])
            i += 1
        else:
            i += 1

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    # Build query
    where = ["t.date >= ?"]
    params = [cutoff]

    if author_filter:
        where.append("t.author = ?")
        params.append(author_filter)

    if category_filter:
        where.append("a.category = ?")
        params.append(category_filter)

    sql = f"""
        SELECT t.author, a.name, a.category, t.tweet_id, t.date, t.text, t.url, t.is_retweet,
               t.platform, t.source_name
        FROM tweets t
        JOIN authors a ON t.author = a.handle
        WHERE {' AND '.join(where)}
        ORDER BY t.date DESC
    """
    rows = conn.execute(sql, params).fetchall()

    # Load focus profile
    profile_row = conn.execute(
        "SELECT rules FROM focus_profiles WHERE name = ?", (profile_name,)
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
            "url": r["url"],
            "is_retweet": bool(r["is_retweet"]),
            "platform": r["platform"],
            "source": r["source_name"],
        })

    output = {
        "query": {
            "days_back": days,
            "cutoff": cutoff,
            "author_filter": author_filter,
            "category_filter": category_filter,
            "profile": profile_name,
        },
        "focus_profile": profile_rules,
        "total_tweets": len(rows),
        "authors_with_data": len(by_author),
        "data": by_author,
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))


def cmd_save_summary(conn, args):
    """Save a summary from stdin."""
    content = sys.stdin.read().strip()
    if not content:
        print('{"error": "no content on stdin"}')
        return

    days_back = int(args[0]) if args else 3
    profile = args[1] if len(args) > 1 else "default"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc).isoformat()

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    tweet_count = conn.execute(
        "SELECT COUNT(*) FROM tweets WHERE date >= ?", (cutoff,)
    ).fetchone()[0]
    sources_ok = conn.execute(
        """SELECT COUNT(DISTINCT author) FROM tweets WHERE date >= ?""", (cutoff,)
    ).fetchone()[0]
    sources_total = conn.execute(
        "SELECT COUNT(*) FROM authors WHERE enabled = 1"
    ).fetchone()[0]

    conn.execute(
        """INSERT INTO summaries (date, days_back, tweet_count, sources_ok, sources_total, focus_profile, content, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (today, days_back, tweet_count, sources_ok, sources_total, profile, content, now),
    )
    conn.commit()
    print(json.dumps({"saved": True, "date": today, "profile": profile, "tweet_count": tweet_count}))


def cmd_authors(conn):
    """List all authors."""
    rows = conn.execute("SELECT handle, name, category, enabled FROM authors ORDER BY category, name").fetchall()
    for r in rows:
        status = "✅" if r["enabled"] else "❌"
        print(f"{status} {r['handle']:20s} {r['name']:25s} [{r['category']}]")


def cmd_profiles(conn):
    """List all focus profiles."""
    rows = conn.execute("SELECT name, description, rules, is_default FROM focus_profiles ORDER BY name").fetchall()
    for r in rows:
        default = " (default)" if r["is_default"] else ""
        rules = json.loads(r["rules"])
        focus = rules.get("focus_authors", [])
        print(f"  {r['name']}{default}: {r['description']}")
        if focus:
            print(f"    → focus: {', '.join(focus)} (weight: {rules.get('focus_weight', 1)}x)")


def cmd_add_profile(conn, args):
    """Add a focus profile."""
    if len(args) < 2:
        print('Usage: add-profile <name> <json-rules>')
        return
    name = args[0]
    rules = args[1]
    now = datetime.now(timezone.utc).isoformat()
    try:
        json.loads(rules)  # validate JSON
    except json.JSONDecodeError:
        print(f'{{"error": "invalid JSON: {rules}"}}')
        return

    conn.execute(
        "INSERT OR REPLACE INTO focus_profiles (name, rules, created_at) VALUES (?, ?, ?)",
        (name, rules, now),
    )
    conn.commit()
    print(json.dumps({"added": name}))


def cmd_subscribers(conn):
    """List all subscribers."""
    rows = conn.execute(
        "SELECT name, phone, email, channel, profile, enabled FROM subscribers ORDER BY name"
    ).fetchall()
    if not rows:
        print("No subscribers yet. Use: add-subscriber --email <email> [--name <name>] [--profile <profile>]")
        return
    for r in rows:
        status = "✅" if r["enabled"] else "⏸️"
        name = r["name"] or "(no name)"
        target = r["email"] or r["phone"] or "?"
        print(f"  {status} {target:35s}  {name:20s}  profile={r['profile']}  via {r['channel']}")


def cmd_add_subscriber(conn, args):
    """Add a subscriber. Usage: add-subscriber --email <email> [--name <name>] [--profile <profile>] [--phone <phone>]"""
    email = None
    phone = None
    name = None
    profile = "default"
    channel = "email"

    i = 0
    while i < len(args):
        if args[i] == "--email" and i + 1 < len(args):
            email = args[i + 1]; i += 2
        elif args[i] == "--phone" and i + 1 < len(args):
            phone = args[i + 1]; channel = "whatsapp"; i += 2
        elif args[i] == "--name" and i + 1 < len(args):
            name = args[i + 1]; i += 2
        elif args[i] == "--profile" and i + 1 < len(args):
            profile = args[i + 1]; i += 2
        else:
            # Legacy positional: first arg is email or phone
            if not email and "@" in args[i]:
                email = args[i]
            elif not phone:
                phone = args[i]
            elif not name:
                name = args[i]
            i += 1

    if not email and not phone:
        print('Usage: add-subscriber --email <email> [--name <name>] [--profile <profile>]')
        return

    now = datetime.now(timezone.utc).isoformat()
    target = email or phone
    try:
        conn.execute(
            "INSERT INTO subscribers (name, phone, email, channel, profile, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (name, phone, email, channel, profile, now),
        )
        conn.commit()
        print(json.dumps({"added": target, "name": name, "profile": profile, "channel": channel}))
    except sqlite3.IntegrityError:
        print(json.dumps({"error": f"{target} already subscribed"}))


def cmd_remove_subscriber(conn, args):
    """Remove a subscriber. Usage: remove-subscriber <email-or-phone>"""
    if not args:
        print('Usage: remove-subscriber <email-or-phone>')
        return
    target = args[0]
    conn.execute("DELETE FROM subscribers WHERE email = ? OR phone = ?", (target, target))
    conn.commit()
    print(json.dumps({"removed": target}))


def cmd_toggle_subscriber(conn, args):
    """Enable/disable a subscriber. Usage: toggle-subscriber <email-or-phone>"""
    if not args:
        print('Usage: toggle-subscriber <email-or-phone>')
        return
    target = args[0]
    row = conn.execute(
        "SELECT enabled FROM subscribers WHERE email = ? OR phone = ?", (target, target)
    ).fetchone()
    if not row:
        print(json.dumps({"error": f"{target} not found"}))
        return
    new_val = 0 if row["enabled"] else 1
    conn.execute(
        "UPDATE subscribers SET enabled = ? WHERE email = ? OR phone = ?",
        (new_val, target, target),
    )
    conn.commit()
    status = "enabled" if new_val else "disabled"
    print(json.dumps({"target": target, "status": status}))


def cmd_stats(conn, args):
    """Quick stats."""
    days = int(args[0]) if args else 7
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    total = conn.execute("SELECT COUNT(*) FROM tweets WHERE date >= ?", (cutoff,)).fetchone()[0]
    by_author = conn.execute(
        """SELECT a.name, COUNT(*) as cnt
           FROM tweets t JOIN authors a ON t.author = a.handle
           WHERE t.date >= ?
           GROUP BY t.author ORDER BY cnt DESC""",
        (cutoff,),
    ).fetchall()

    summaries = conn.execute(
        "SELECT COUNT(*) FROM summaries WHERE date >= ?",
        (datetime.now(timezone.utc).strftime("%Y-%m-%d"),),
    ).fetchone()[0]

    print(f"📊 过去 {days} 天统计：")
    print(f"   总内容数：{total}")
    print(f"   今日摘要数：{summaries}")
    print(f"   按人物：")
    for r in by_author:
        print(f"     {r['name']}: {r['cnt']} 条")


# ── Main ──────────────────────────────────────────────────────────────

def main():
    conn = get_db()
    init_db(conn)

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    args = sys.argv[2:]

    if cmd == "fetch":
        cmd_fetch(conn, args)
    elif cmd == "save-items":
        cmd_save_items(conn, args)
    elif cmd == "query":
        cmd_query(conn, args)
    elif cmd == "save-summary":
        cmd_save_summary(conn, args)
    elif cmd == "authors":
        cmd_authors(conn)
    elif cmd == "profiles":
        cmd_profiles(conn)
    elif cmd == "add-profile":
        cmd_add_profile(conn, args)
    elif cmd == "subscribers":
        cmd_subscribers(conn)
    elif cmd == "add-subscriber":
        cmd_add_subscriber(conn, args)
    elif cmd == "remove-subscriber":
        cmd_remove_subscriber(conn, args)
    elif cmd == "toggle-subscriber":
        cmd_toggle_subscriber(conn, args)
    elif cmd == "stats":
        cmd_stats(conn, args)
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)

    conn.close()


if __name__ == "__main__":
    main()
