#!/usr/bin/env python3
"""Send a saved digest via Gmail SMTP — stdlib only.

Usage:
  python3 email_sender.py [--date YYYY-MM-DD] [--dry-run] [--html-out PATH]
  python3 email_sender.py --file data/orphan_digest_YYYY-MM-DD.md --orphan [--date YYYY-MM-DD]

Each digest date is sent once: successful sends are logged to data/email_sent.jsonl
and a later run for that date is skipped (exit 0, "skipped": true) unless --force.

Without --date, sends the latest saved summary. Settings come from env vars,
falling back to <project>/.env: DIGEST_RECIPIENT, GMAIL_SMTP_USER,
GMAIL_APP_PASSWORD. The App Password requires 2-Step Verification; create one
at https://myaccount.google.com/apppasswords.

Exit codes: 0 = ok, 1 = send or lookup failed, 2 = configuration missing.
"""

import argparse
import html
import json
import os
import re
import smtplib
import sqlite3
import ssl
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from pathlib import Path

from db import DB_PATH

ENV_PATH = Path(__file__).resolve().parent / ".env"
SENT_LOG_PATH = Path(__file__).resolve().parent / "data" / "email_sent.jsonl"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_TIMEOUT = 30


def load_settings():
    """Return settings from <project>/.env overlaid with real env vars."""
    settings = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            settings[key.strip()] = value.strip().strip('"').strip("'")
    for key in ("DIGEST_RECIPIENT", "GMAIL_SMTP_USER", "GMAIL_APP_PASSWORD"):
        if os.environ.get(key):
            settings[key] = os.environ[key]
    return settings


def load_summary(date=None):
    """Return (date, content) for the newest summary, optionally on one date."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        if date:
            row = conn.execute(
                "SELECT date, content FROM summaries WHERE date = ? ORDER BY id DESC LIMIT 1",
                (date,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT date, content FROM summaries ORDER BY id DESC LIMIT 1"
            ).fetchone()
    finally:
        conn.close()
    if not row:
        raise LookupError(f"no saved summary for {date or 'any date'}")
    return row


def find_sent(date):
    """Return the logged send for this digest date, or None."""
    if not SENT_LOG_PATH.exists():
        return None
    for line in SENT_LOG_PATH.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("date") == date:
            return entry
    return None


def log_sent(date, recipient, subject):
    entry = {
        "date": date,
        "to": recipient,
        "subject": subject,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    with SENT_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def md_to_html(md):
    """Minimal markdown → HTML for digest emails. Source is escaped first."""
    safe = html.escape(md)
    out, in_list, para = [], False, []
    bold = re.compile(r"\*\*(.+?)\*\*")
    code = re.compile(r"`([^`]+?)`")
    link = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")

    def inline(s):
        s = bold.sub(r"<strong>\1</strong>", s)
        s = code.sub(r"<code>\1</code>", s)
        s = link.sub(r'<a href="\2">\1</a>', s)
        return s

    def flush_para():
        nonlocal para
        if para:
            out.append("<p>" + inline(" ".join(para).strip()) + "</p>")
            para = []

    def close_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for line in safe.split("\n"):
        s = line.strip()
        m = re.match(r"^(#{1,3})\s+(.+)$", s)
        if m:
            flush_para()
            close_list()
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{inline(m.group(2))}</h{lvl}>")
        elif re.fullmatch(r"-{3,}", s):
            flush_para()
            close_list()
            out.append("<hr>")
        elif s.startswith("- ") or s.startswith("* "):
            flush_para()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(s[2:])}</li>")
        elif not s:
            flush_para()
            close_list()
        else:
            close_list()
            para.append(s)
    flush_para()
    close_list()
    return (
        '<!DOCTYPE html><html><body style="font-family:-apple-system,'
        "BlinkMacSystemFont,Segoe UI,sans-serif;max-width:680px;"
        'margin:auto;padding:20px;line-height:1.5;color:#222;">'
        + "\n".join(out)
        + "</body></html>"
    )


def build_message(sender, recipient, subject, body_md):
    """Build a multipart/alternative message (plain markdown + rendered HTML)."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="ai-leaders-digest.local")
    msg.attach(MIMEText(body_md, "plain", "utf-8"))
    msg.attach(MIMEText(md_to_html(body_md), "html", "utf-8"))
    return msg


def send_message(msg, user, password):
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as server:
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
        # smtplib retries the next mechanism after a 535, but Gmail has hung up
        # by then, so the real error would surface as SMTPServerDisconnected.
        server.esmtp_features["auth"] = "PLAIN"
        # Google displays App Passwords in space-separated groups.
        server.login(user, "".join(password.split()))
        server.send_message(msg)


def fail(message, code):
    print(json.dumps({"sent": False, "error": message}, ensure_ascii=False))
    return code


def main(argv=None):
    ap = argparse.ArgumentParser(description="Send a saved digest via Gmail SMTP.")
    ap.add_argument("--date", help="Summary date, YYYY-MM-DD (default: latest saved)")
    ap.add_argument("--file", help="Send this markdown file instead of reading the DB")
    ap.add_argument("--orphan", action="store_true",
                    help="Prepend [ORPHAN: not in DB] to the subject")
    ap.add_argument("--dry-run", action="store_true",
                    help="Render and print headers without connecting or sending")
    ap.add_argument("--force", action="store_true",
                    help="Send even if this date's digest was already sent")
    ap.add_argument("--html-out", help="Also write the rendered HTML to this path")
    args = ap.parse_args(argv)

    try:
        if args.file:
            date = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
            body = Path(args.file).read_text(encoding="utf-8")
        else:
            date, body = load_summary(args.date)
    except (LookupError, OSError, sqlite3.Error) as e:
        return fail(f"could not load digest: {e}", 1)
    if not body.strip():
        return fail("digest is empty", 1)

    settings = load_settings()
    recipient = settings.get("DIGEST_RECIPIENT")
    user = settings.get("GMAIL_SMTP_USER")
    password = settings.get("GMAIL_APP_PASSWORD")
    if not recipient:
        return fail(f"DIGEST_RECIPIENT is not set in the environment or {ENV_PATH}", 2)

    subject = f"AI Leaders Digest {date}"
    if args.orphan:
        subject = f"[ORPHAN: not in DB] {subject}"
    msg = build_message(user or "(GMAIL_SMTP_USER not set)", recipient, subject, body)
    rendered = md_to_html(body)
    if args.html_out:
        Path(args.html_out).write_text(rendered, encoding="utf-8")

    previous = find_sent(date)
    if args.dry_run:
        for key in ("Subject", "From", "To", "Date", "Message-ID", "Content-Type"):
            print(f"{key}: {msg[key]}")
        print(f"\n[dry-run] not sent. text={len(body)} chars, html={len(rendered)} chars")
        print(f"[dry-run] GMAIL_SMTP_USER: {'set' if user else 'MISSING'}, "
              f"GMAIL_APP_PASSWORD: {'set' if password else 'MISSING'}")
        if previous:
            print(f"[dry-run] already sent at {previous.get('sent_at')}; a real run would skip")
        return 0

    if previous and not args.force:
        print(json.dumps({"sent": False, "skipped": True, "date": date,
                          "sent_at": previous.get("sent_at")}, ensure_ascii=False))
        return 0

    missing = [k for k, v in (("GMAIL_SMTP_USER", user), ("GMAIL_APP_PASSWORD", password)) if not v]
    if missing:
        return fail(f"{' and '.join(missing)} not set in the environment or {ENV_PATH}", 2)

    try:
        send_message(msg, user, password)
    except (smtplib.SMTPException, OSError) as e:
        return fail(f"{type(e).__name__}: {e}", 1)
    result = {"sent": True, "to": recipient, "subject": subject}
    try:
        log_sent(date, recipient, subject)
    except OSError as e:
        result["warning"] = f"sent, but could not log it, so a re-run would resend: {e}"
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
