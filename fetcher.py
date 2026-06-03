#!/usr/bin/env python3
"""
AI Leaders RSS Digest with SQLite persistence.

Usage:
  python3 rss_digest.py fetch              # Fetch RSS → store in DB
  python3 rss_digest.py query [days] [--author X] [--category Y] [--profile Z]
                                           # Query tweets from DB, output JSON
  python3 rss_digest.py save-summary       # Save summary text from stdin
  python3 rss_digest.py authors            # List all authors
  python3 rss_digest.py profiles           # List all focus profiles
  python3 rss_digest.py add-profile <name> <json>  # Add a focus profile
  python3 rss_digest.py subscribers         # List subscribers
  python3 rss_digest.py add-subscriber <phone> [name] [profile]
  python3 rss_digest.py remove-subscriber <phone>
  python3 rss_digest.py toggle-subscriber <phone>  # Enable/disable
  python3 rss_digest.py stats [days]       # Quick stats
"""

import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

DB_PATH = os.environ.get(
    "RSS_DB_PATH",
    str(Path(__file__).resolve().parent / "data" / "ai_leaders.db"),
)

FALLBACK_NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
]

DEFAULT_AUTHORS = [
    ("geoffreyhinton", "Geoffrey Hinton", "ai-pioneer", "https://nitter.net/geoffreyhinton/rss"),
    ("karpathy", "Andrej Karpathy", "ai-engineering", "https://nitter.net/karpathy/rss"),
    ("Yoshua_Bengio", "Yoshua Bengio", "ai-pioneer", "https://nitter.net/Yoshua_Bengio/rss"),
    ("elonmusk", "Elon Musk", "tech-leader", "https://nitter.net/elonmusk/rss"),
    ("sama", "Sam Altman", "ai-industry", "https://nitter.net/sama/rss"),
    ("AndrewYNg", "Andrew Ng", "ai-pioneer", "https://nitter.net/AndrewYNg/rss"),
    ("jensenhuang", "Jensen Huang", "tech-leader", "https://nitter.net/jensenhuang/rss"),
    ("paulg", "Paul Graham", "startup", "https://nitter.net/paulg/rss"),
    ("ylecun", "Yann LeCun", "ai-pioneer", "https://nitter.net/ylecun/rss"),
    ("gdb", "Greg Brockman", "ai-industry", "https://nitter.net/gdb/rss"),
    ("demishassabis", "Demis Hassabis", "ai-pioneer", "https://nitter.net/demishassabis/rss"),
    ("soumithchintala", "Soumith Chintala", "ai-engineering", "https://nitter.net/soumithchintala/rss"),
]

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
    conn.execute("PRAGMA journal_mode=WAL")
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
            tweet_id    TEXT UNIQUE,
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

    # Seed authors if empty
    if conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO authors (handle, name, category, rss_url) VALUES (?, ?, ?, ?)",
            DEFAULT_AUTHORS,
        )

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


# ── RSS Fetching ──────────────────────────────────────────────────────

def parse_rss_date(date_str):
    if not date_str:
        return None
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S GMT",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def fetch_feed(handle, rss_url):
    """Fetch RSS feed, trying fallback instances."""
    username = handle
    errors = []

    for base in FALLBACK_NITTER_INSTANCES:
        url = f"{base}/{username}/rss"
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; RSS-Reader/1.0)"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read()

            root = ET.fromstring(content)
            channel = root.find("channel")
            if channel is None:
                errors.append(f"{base}: no channel element")
                continue

            items = []
            for item in channel.findall("item"):
                title = item.findtext("title", "").strip()
                link = item.findtext("link", "").strip()
                pub_date_str = item.findtext("pubDate", "")
                description = item.findtext("description", "").strip()

                pub_date = parse_rss_date(pub_date_str)

                # Clean HTML
                description = re.sub(r"<[^>]+>", "", description).strip()
                if len(description) > 500:
                    description = description[:500]

                # Detect retweet
                is_rt = 1 if title.startswith("RT by") or title.startswith("R to") else 0

                # Extract tweet ID from link
                tweet_id = None
                if link:
                    m = re.search(r"/status/(\d+)", link)
                    if m:
                        tweet_id = m.group(1)

                items.append({
                    "tweet_id": tweet_id,
                    "date": pub_date.isoformat() if pub_date else None,
                    "text": description or title,
                    "url": link,
                    "is_retweet": is_rt,
                })

            return {"status": "ok", "items": items, "source_url": url}

        except urllib.error.HTTPError as e:
            errors.append(f"{base}: HTTP {e.code}")
        except urllib.error.URLError as e:
            errors.append(f"{base}: {e.reason}")
        except ET.ParseError as e:
            errors.append(f"{base}: XML parse error - {e}")
        except Exception as e:
            errors.append(f"{base}: {e}")

        time.sleep(0.5)

    return {"status": "failed", "items": [], "errors": errors}


def cmd_fetch(conn, args=None):
    """Fetch all enabled authors' RSS and store tweets.

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

    authors = conn.execute(
        "SELECT handle, name, rss_url FROM authors WHERE enabled = 1"
    ).fetchall()

    now = datetime.now(timezone.utc).isoformat()
    stats = {"total": len(authors), "ok": 0, "failed": 0, "new_tweets": 0, "dupes": 0}
    failed_names = []

    for a in authors:
        result = fetch_feed(a["handle"], a["rss_url"])
        if result["status"] == "ok":
            stats["ok"] += 1
            for item in result["items"]:
                if not item["tweet_id"] or not item["date"]:
                    continue
                try:
                    conn.execute(
                        """INSERT INTO tweets (author, tweet_id, date, text, url, is_retweet, fetched_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (a["handle"], item["tweet_id"], item["date"],
                         item["text"], item["url"], item["is_retweet"], now),
                    )
                    stats["new_tweets"] += 1
                except sqlite3.IntegrityError:
                    stats["dupes"] += 1
            # Commit per author so the write lock + WAL pages are released
            # promptly — the prior all-authors-one-transaction held the lock
            # for 12+ seconds across the full RSS sweep, which appears to be
            # what the recurring index corruption was tracking with. Any
            # author whose feed parses successfully now lands its rows in
            # SQLite immediately; a later author failing or the process
            # being killed mid-loop leaves earlier authors' rows intact.
            try:
                conn.commit()
            except sqlite3.OperationalError as e:
                # Treat per-author commit failure as a soft fault on this
                # author only; subsequent authors still get a chance.
                sys.stderr.write(
                    f"commit failed for {a['handle']}: {e}; continuing\n"
                )
                stats["failed"] += 1
                stats["ok"] -= 1
                failed_names.append(a["name"])
        else:
            stats["failed"] += 1
            failed_names.append(a["name"])

        time.sleep(1)

    # Final mass cleanup — once all per-author commits are in, truncate the
    # WAL so it doesn't accumulate from one fetch to the next. wal_checkpoint
    # silently degrades to PASSIVE when readers are active; that's fine.
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:
        pass
    stats["failed_names"] = failed_names

    # Determine if we should output a full report
    import zoneinfo
    local_hour = datetime.now(zoneinfo.ZoneInfo("America/Los_Angeles")).hour
    
    if report_hour is not None:
        stats["report"] = (local_hour == report_hour)
    else:
        stats["report"] = True  # always report if no --report-hour flag

    print(json.dumps(stats, ensure_ascii=False, indent=2))


# ── Querying ──────────────────────────────────────────────────────────

def cmd_query(conn, args):
    """Query tweets and output JSON for LLM summarization."""
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
        SELECT t.author, a.name, a.category, t.tweet_id, t.date, t.text, t.url, t.is_retweet
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
    print(f"   总推文数：{total}")
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
