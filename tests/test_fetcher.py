import io
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import digest_generate
import fetcher

NOW = datetime.now(timezone.utc)
RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><link>https://bsky.app/profile/a/post/1</link><description>Hello &amp;amp; &lt;b&gt;world&lt;/b&gt;</description>
<pubDate>21 Oct 2026 13:28 +0000</pubDate><guid isPermaLink="false">at://x/1</guid></item>
<item><link>https://example.com/undated</link><description>no date</description></item>
</channel></rss>"""
ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>tag:1</id><title>-</title>
<link rel="self" href="https://example.com/self"/><link rel="alternate" href="https://example.com/post"/>
<published>2026-08-16T05:00:31-07:00</published><updated>2026-11-02T01:00:00Z</updated>
<content type="html">&lt;p&gt;Body&lt;/p&gt;</content></entry></feed>"""
BSKY = json.dumps({"feed": [
    {"post": {"uri": "at://did/app.bsky.feed.post/abc", "author": {"handle": "m.example"},
              "record": {"text": "own post", "createdAt": "2026-11-01T19:46:19.000Z"}}},
    {"reason": {"$type": "app.bsky.feed.defs#reasonRepost"},
     "post": {"uri": "at://other/app.bsky.feed.post/def", "author": {"handle": "o.example"},
              "record": {"text": "reposted", "createdAt": "2026-11-01T10:00:00.000Z"}}},
]}).encode()
CONFIG = [
    {"handle": "alice", "name": "Alice", "category": "c", "sources": [
        {"type": "feed", "url": "https://bad.example/rss", "platform": "blog", "label": "Bad"},
        {"type": "feed", "url": "https://good.example/rss", "platform": "bluesky", "label": "Good"},
    ]},
    {"handle": "bob", "name": "Bob", "category": "c", "sources": []},
]


@pytest.fixture
def conn(tmp_path, monkeypatch):
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps(CONFIG))
    monkeypatch.setattr(fetcher, "SOURCES_PATH", str(sources))
    monkeypatch.setattr(fetcher, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(digest_generate, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(fetcher.time, "sleep", lambda s: None)
    c = fetcher.get_db()
    fetcher.init_db(c)
    yield c
    c.close()


def save(conn, monkeypatch, capsys, items, *args):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(items)))
    fetcher.cmd_save_items(conn, list(args))
    return json.loads(capsys.readouterr().out)


def web_item(**kw):
    item = {"author": "alice", "date": NOW.strftime("%Y-%m-%d"), "url": "https://news.example/a",
            "source_name": "News", "text": "Alice said something specific about agents."}
    item.update(kw)
    return item


def test_parse_rss_with_bluesky_date_and_entities():
    items = fetcher.parse_feed(RSS)
    assert items[0]["date"] == datetime(2026, 10, 21, 13, 28, tzinfo=timezone.utc)
    assert items[0]["text"] == "Hello & world"
    assert items[1]["date"] is None


def test_parse_atom_prefers_published_alternate_and_drops_noise_title():
    item = fetcher.parse_feed(ATOM)[0]
    assert item["url"] == "https://example.com/post"
    assert item["date"].isoformat() == "2026-08-16T12:00:31+00:00"
    assert item["text"] == "Body"


def test_feed_filter_and_link_rewrite(monkeypatch):
    rss = (b'<rss><channel><item><link>https://h.example/issue-1/</link><description>a</description>'
           b'<pubDate>Fri, 30 Oct 2026 14:00:00 GMT</pubDate></item>'
           b'<item><link>https://h.example/story/</link><description>b</description>'
           b'<pubDate>Fri, 30 Oct 2026 14:00:00 GMT</pubDate></item></channel></rss>')
    monkeypatch.setattr(fetcher, "http_get", lambda url: rss)
    items = fetcher.fetch_feed_source({
        "url": "u", "include_url": r"/issue-\d+/",
        "link_rewrite": {"from": "https://h.example/", "to": "https://pub.example/b/"},
    })
    assert [i["url"] for i in items] == ["https://pub.example/b/issue-1/"]


def test_bluesky_marks_reposts_and_uses_post_author_url(monkeypatch):
    monkeypatch.setattr(fetcher, "http_get", lambda url: BSKY)
    own, repost = fetcher.fetch_bluesky_source({"actor": "m.example"})
    assert own["url"] == "https://bsky.app/profile/m.example/post/abc" and own["is_retweet"] == 0
    assert repost["url"] == "https://bsky.app/profile/o.example/post/def" and repost["is_retweet"] == 1


def test_fetch_fails_soft_per_source_and_dedupes(conn, monkeypatch, capsys):
    def get(url):
        if "bad" in url:
            raise OSError("boom")
        return RSS
    monkeypatch.setattr(fetcher, "http_get", get)
    fetcher.cmd_fetch(conn)
    stats = json.loads(capsys.readouterr().out)
    assert (stats["total"], stats["ok"], stats["failed"], stats["new_items"]) == (2, 1, 1, 1)
    assert stats["failed_names"] == ["Alice: Bad"]
    row = conn.execute("SELECT platform, source_name, url FROM tweets").fetchone()
    assert tuple(row) == ("bluesky", "Good", "https://bsky.app/profile/a/post/1")
    fetcher.cmd_fetch(conn)
    assert json.loads(capsys.readouterr().out)["dupes"] == 1


def test_fetch_exits_nonzero_when_every_source_fails(conn, monkeypatch, capsys):
    def get(url):
        raise OSError("boom")
    monkeypatch.setattr(fetcher, "http_get", get)
    with pytest.raises(SystemExit) as e:
        fetcher.cmd_fetch(conn)
    assert e.value.code == 1


def test_migration_upgrades_legacy_db_and_clears_rss_url(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE authors (handle TEXT PRIMARY KEY, name TEXT, category TEXT, rss_url TEXT, enabled INTEGER DEFAULT 1);
        CREATE TABLE tweets (id INTEGER PRIMARY KEY AUTOINCREMENT, author TEXT NOT NULL, tweet_id TEXT UNIQUE,
            date TEXT NOT NULL, text TEXT NOT NULL, url TEXT, is_retweet INTEGER DEFAULT 0, fetched_at TEXT NOT NULL);
        INSERT INTO authors VALUES ('alice', 'A', 'c', 'https://dead.example/alice/rss', 0);
        INSERT INTO tweets (author, tweet_id, date, text, fetched_at) VALUES ('alice', '1', '2026-01-01', 'x', 'n');
    """)
    old.commit()
    old.close()
    sources = tmp_path / "s.json"
    sources.write_text(json.dumps(CONFIG))
    monkeypatch.setattr(fetcher, "SOURCES_PATH", str(sources))
    monkeypatch.setattr(fetcher, "DB_PATH", str(path))
    c = fetcher.get_db()
    fetcher.init_db(c)
    assert c.execute("SELECT platform FROM tweets").fetchone()[0] == "x"
    a = c.execute("SELECT rss_url, enabled FROM authors WHERE handle='alice'").fetchone()
    assert a["rss_url"] is None and a["enabled"] == 0  # enabled flag is preserved
    c.close()


def test_fetch_dedupes_without_unique_constraint(tmp_path, monkeypatch, capsys):
    """Restored copies of the live DB have no UNIQUE(tweet_id); re-fetching must not duplicate."""
    path = tmp_path / "restored.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE authors (handle TEXT PRIMARY KEY, name TEXT, category TEXT, rss_url TEXT, enabled INTEGER DEFAULT 1);
        CREATE TABLE tweets (id INTEGER PRIMARY KEY AUTOINCREMENT, author TEXT, date TEXT, text TEXT, url TEXT,
            is_retweet INTEGER DEFAULT 0, tweet_id TEXT, fetched_at TEXT);
    """)
    old.close()
    sources = tmp_path / "s.json"
    sources.write_text(json.dumps(CONFIG))
    monkeypatch.setattr(fetcher, "SOURCES_PATH", str(sources))
    monkeypatch.setattr(fetcher, "DB_PATH", str(path))
    monkeypatch.setattr(fetcher.time, "sleep", lambda s: None)
    monkeypatch.setattr(fetcher, "http_get", lambda url: RSS)
    c = fetcher.get_db()
    fetcher.init_db(c)
    fetcher.cmd_fetch(c)
    fetcher.cmd_fetch(c)
    second = json.loads(capsys.readouterr().out.split("\n}\n")[1] + "\n}")
    assert (second["new_items"], second["dupes"]) == (0, 2)
    # both sources serve the same feed, so the shared item is stored once
    assert c.execute("SELECT COUNT(*) FROM tweets").fetchone()[0] == 1
    c.close()


def test_save_items_accepts_valid_and_labels_web(conn, monkeypatch, capsys):
    result = save(conn, monkeypatch, capsys, [web_item(author="@ALICE")])
    assert result == {"accepted": 1, "duplicates": 0, "rejected": []}
    row = conn.execute("SELECT author, platform, source_name FROM tweets").fetchone()
    assert tuple(row) == ("alice", "web", "News")


@pytest.mark.parametrize("patch,reason", [
    ({"url": ""}, "missing or invalid source url"),
    ({"url": "javascript:alert(1)"}, "missing or invalid source url"),
    ({"date": ""}, "missing or unparseable date"),
    ({"date": "recently"}, "missing or unparseable date"),
    ({"date": (NOW - timedelta(days=5)).strftime("%Y-%m-%d")}, "older than window"),
    ({"date": (NOW + timedelta(days=3)).strftime("%Y-%m-%d")}, "date in the future"),
    ({"author": "nobody"}, "unknown author"),
    ({"text": " "}, "empty text"),
])
def test_save_items_rejects_invalid(conn, monkeypatch, capsys, patch, reason):
    result = save(conn, monkeypatch, capsys, [web_item(**patch)])
    assert result["accepted"] == 0
    assert result["rejected"] == [{"index": 0, "reason": reason}]
    assert conn.execute("SELECT COUNT(*) FROM tweets").fetchone()[0] == 0


def test_save_items_window_is_configurable(conn, monkeypatch, capsys):
    old = web_item(date=(NOW - timedelta(days=5)).strftime("%Y-%m-%d"))
    assert save(conn, monkeypatch, capsys, [old], "--days", "7")["accepted"] == 1


def test_save_items_dedupes_by_url_and_content(conn, monkeypatch, capsys):
    save(conn, monkeypatch, capsys, [web_item()])
    same_url = web_item(url="https://www.news.example/a/?utm_source=x#frag", text="Totally different words here.")
    same_text = web_item(url="https://other.example/b")
    contained = web_item(url="https://third.example/c", text="Reports say: Alice said something specific about agents. More.")
    fresh = web_item(url="https://fourth.example/d", text="Bob-free unrelated announcement of a new paper on robotics.")
    result = save(conn, monkeypatch, capsys, [same_url, same_text, contained, fresh])
    assert (result["accepted"], result["duplicates"]) == (1, 3)


def test_same_article_may_cover_two_authors(conn, monkeypatch, capsys):
    result = save(conn, monkeypatch, capsys, [web_item(), web_item(author="bob", text="Bob answered with a separate claim.")])
    assert result["accepted"] == 2


def test_save_items_rejects_bad_payload(conn, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    with pytest.raises(SystemExit):
        fetcher.cmd_save_items(conn, [])


def test_digest_query_carries_source_fields(conn, monkeypatch, capsys):
    save(conn, monkeypatch, capsys, [web_item()])
    data = digest_generate.load_tweets(days=5)
    item = data["data"]["alice"]["tweets"][0]
    assert (item["platform"], item["source"], item["url"]) == ("web", "News", "https://news.example/a")
    assert data["tweet_count"] == 1 and data["sources_total"] == 2


def test_research_prompt_lists_authors_and_existing_items(conn, monkeypatch, capsys):
    save(conn, monkeypatch, capsys, [web_item(date=NOW.isoformat())])
    digest_generate.cmd_research(2)
    out = json.loads(capsys.readouterr().out)
    assert "- bob — Bob" in out["prompt"]
    assert "https://news.example/a" in out["prompt"]
    assert "绝不编造引语" in out["prompt"]


def test_repo_config_is_well_formed():
    config = json.loads((Path(fetcher.__file__).parent / "sources.json").read_text())
    assert len(config) == 13
    for author in config:
        for source in author["sources"]:
            assert source["type"] in fetcher.SOURCE_FETCHERS and source["platform"] and source["label"]
