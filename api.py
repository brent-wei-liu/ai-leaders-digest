"""ai-leaders-digest — FastAPI server for the web UI.

Run: python3 api.py
URL: http://127.0.0.1:8081 (local), http://<mac-ip>:8081 (LAN/phone)
"""
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

sys.path.insert(0, str(Path(__file__).parent))
from db import (
    get_conn, get_tweets, get_authors,
    star_tweet, unstar_tweet,
    get_summaries, get_summary, get_stats,
    mark_summary_read, mark_summary_unread,
)

PORT = 8081
HOST = "0.0.0.0"  # bind so phones on same Wi-Fi can reach it
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="ai-leaders-digest")


@app.get("/")
def root():
    index = STATIC_DIR / "index.html"
    if not index.exists():
        raise HTTPException(404, "static/index.html missing")
    return FileResponse(str(index))


@app.get("/api/stats")
def api_stats():
    conn = get_conn()
    try:
        return get_stats(conn)
    finally:
        conn.close()


@app.get("/api/authors")
def api_authors():
    conn = get_conn()
    try:
        return {"authors": get_authors(conn)}
    finally:
        conn.close()


@app.get("/api/tweets")
def api_tweets(
    author: str | None = None,
    q: str | None = None,
    starred: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    conn = get_conn()
    try:
        tweets, total = get_tweets(
            conn,
            author=author,
            query=q,
            starred_only=starred,
            page=page,
            page_size=page_size,
        )
        return {
            "tweets": tweets,
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_next": (page * page_size) < total,
        }
    finally:
        conn.close()


@app.post("/api/tweets/{tweet_id}/star")
def api_star(tweet_id: int):
    conn = get_conn()
    try:
        if not star_tweet(conn, tweet_id):
            raise HTTPException(404, "tweet not found")
        return {"ok": True, "id": tweet_id, "starred": True}
    finally:
        conn.close()


@app.post("/api/tweets/{tweet_id}/unstar")
def api_unstar(tweet_id: int):
    conn = get_conn()
    try:
        if not unstar_tweet(conn, tweet_id):
            raise HTTPException(404, "tweet not found")
        return {"ok": True, "id": tweet_id, "starred": False}
    finally:
        conn.close()


@app.get("/api/summaries")
def api_summaries():
    conn = get_conn()
    try:
        return {"summaries": get_summaries(conn)}
    finally:
        conn.close()


@app.get("/api/summaries/{summary_id}")
def api_summary(summary_id: int):
    conn = get_conn()
    try:
        s = get_summary(conn, summary_id)
        if not s:
            raise HTTPException(404, "summary not found")
        return s
    finally:
        conn.close()


@app.post("/api/summaries/{summary_id}/read")
def api_summary_read(summary_id: int):
    conn = get_conn()
    try:
        if not mark_summary_read(conn, summary_id):
            raise HTTPException(404, "summary not found")
        return {"ok": True, "is_read": True}
    finally:
        conn.close()


@app.post("/api/summaries/{summary_id}/unread")
def api_summary_unread(summary_id: int):
    conn = get_conn()
    try:
        if not mark_summary_unread(conn, summary_id):
            raise HTTPException(404, "summary not found")
        return {"ok": True, "is_read": False}
    finally:
        conn.close()


# Mount /static AFTER the route handlers so /api/* doesn't get shadowed.
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    print(f"ai-leaders-digest UI on http://127.0.0.1:{PORT}  (LAN: http://<mac-ip>:{PORT})")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
