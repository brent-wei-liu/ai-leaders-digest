"""ai-leaders-digest — shared SQLite data layer for the web UI.

Reads the same DB that fetcher.py / digest_generate.py write to.
Adds two columns to `tweets` for star-marking, plus a partial index for
fast "starred only" queries. Migrations are additive ALTER TABLE — safe
to run repeatedly against an existing DB.
"""
import sqlite3
import os
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get(
    "RSS_DB_PATH",
    str(Path(__file__).resolve().parent / "data" / "ai_leaders.db"),
))


def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # Without busy_timeout the default is 0 — any concurrent writer hitting a
    # lock fails immediately with SQLITE_BUSY. ai-leaders-digest runs an
    # always-on api.py alongside a 4x/day fetcher, and the resulting
    # contention churn is the most plausible culprit behind the recurring
    # page-level index corruption (see git history for repeated dump+restore
    # cycles). 5 seconds is well under any HTTP request timeout but long
    # enough to absorb a fetcher commit burst.
    conn.execute("PRAGMA busy_timeout=5000")
    _migrate(conn)
    return conn


def _migrate(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tweets)").fetchall()}
    for col, typedef in [
        ("starred", "INTEGER DEFAULT 0"),
        ("starred_at", "TEXT DEFAULT NULL"),
    ]:
        if col not in cols:
            conn.execute(f"ALTER TABLE tweets ADD COLUMN {col} {typedef}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tweets_starred "
        "ON tweets(starred) WHERE starred = 1"
    )
    # summaries: is_read / read_at — additive ALTERs, idempotent
    s_cols = {r[1] for r in conn.execute("PRAGMA table_info(summaries)").fetchall()}
    for col, typedef in [
        ("is_read", "INTEGER NOT NULL DEFAULT 0"),
        ("read_at", "TEXT DEFAULT NULL"),
    ]:
        if col not in s_cols:
            conn.execute(f"ALTER TABLE summaries ADD COLUMN {col} {typedef}")
    conn.commit()


def get_tweets(conn, *, author=None, query=None, starred_only=False,
               page=1, page_size=50):
    """Return (rows, total_matching). Tweets ordered by date desc."""
    where = []
    params = []
    if author:
        where.append("t.author = ?")
        params.append(author)
    if query:
        where.append("t.text LIKE ?")
        params.append(f"%{query}%")
    if starred_only:
        where.append("t.starred = 1")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    total = conn.execute(
        f"SELECT COUNT(*) FROM tweets t{where_sql}", params
    ).fetchone()[0]

    offset = max(0, (page - 1) * page_size)
    rows = conn.execute(
        f"""SELECT t.id, t.author, a.name AS author_name, a.category,
                   t.tweet_id, t.date, t.text, t.url, t.is_retweet,
                   t.starred, t.starred_at
            FROM tweets t
            JOIN authors a ON t.author = a.handle
            {where_sql}
            ORDER BY t.date DESC
            LIMIT ? OFFSET ?""",
        params + [page_size, offset],
    ).fetchall()
    return [dict(r) for r in rows], total


def get_authors(conn):
    """Return all enabled authors with their tweet counts."""
    rows = conn.execute(
        """SELECT a.handle, a.name, a.category, a.enabled,
                  (SELECT COUNT(*) FROM tweets t WHERE t.author = a.handle) AS tweet_count
           FROM authors a
           ORDER BY a.category, a.name"""
    ).fetchall()
    return [dict(r) for r in rows]


def star_tweet(conn, tweet_id):
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "UPDATE tweets SET starred = 1, starred_at = ? WHERE id = ?",
        (now, tweet_id),
    )
    conn.commit()
    return cur.rowcount > 0


def unstar_tweet(conn, tweet_id):
    cur = conn.execute(
        "UPDATE tweets SET starred = 0, starred_at = NULL WHERE id = ?",
        (tweet_id,),
    )
    conn.commit()
    return cur.rowcount > 0


def get_summaries(conn):
    rows = conn.execute(
        """SELECT id, date, days_back, tweet_count, sources_ok, sources_total,
                  focus_profile, created_at, is_read, read_at
           FROM summaries
           ORDER BY date DESC, id DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def get_summary(conn, summary_id):
    row = conn.execute(
        """SELECT id, date, days_back, tweet_count, sources_ok, sources_total,
                  focus_profile, content, created_at, is_read, read_at
           FROM summaries WHERE id = ?""",
        (summary_id,),
    ).fetchone()
    return dict(row) if row else None


def mark_summary_read(conn, summary_id):
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "UPDATE summaries SET is_read = 1, read_at = ? WHERE id = ?",
        (now, summary_id),
    )
    conn.commit()
    return cur.rowcount > 0


def mark_summary_unread(conn, summary_id):
    cur = conn.execute(
        "UPDATE summaries SET is_read = 0, read_at = NULL WHERE id = ?",
        (summary_id,),
    )
    conn.commit()
    return cur.rowcount > 0


def get_stats(conn):
    return {
        "tweets_total": conn.execute("SELECT COUNT(*) FROM tweets").fetchone()[0],
        "tweets_starred": conn.execute(
            "SELECT COUNT(*) FROM tweets WHERE starred = 1"
        ).fetchone()[0],
        "authors_enabled": conn.execute(
            "SELECT COUNT(*) FROM authors WHERE enabled = 1"
        ).fetchone()[0],
        "summaries_total": conn.execute(
            "SELECT COUNT(*) FROM summaries"
        ).fetchone()[0],
        "latest_fetch": conn.execute(
            "SELECT MAX(fetched_at) FROM tweets"
        ).fetchone()[0],
        "latest_tweet": conn.execute(
            "SELECT MAX(date) FROM tweets"
        ).fetchone()[0],
    }
