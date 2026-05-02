#!/usr/bin/env python3
"""Send email via Gmail SMTP — stdlib only.

Credentials:
  1. Env vars GMAIL_USER and GMAIL_APP_PASSWORD (preferred for shells/cron)
  2. Fallback to <project_root>/.env file with KEY=VALUE lines

The Gmail App Password requires 2FA enabled on the Google Account; generate
one at https://myaccount.google.com/apppasswords. Never commit credentials.
"""
import html
import os
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path


SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_TIMEOUT = 30


def _load_env_file(path):
    """Parse KEY=VALUE lines from a .env file. Quotes stripped, comments ignored."""
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_credentials():
    """Return (user, app_password). Raises RuntimeError if neither source has both."""
    user = os.environ.get("GMAIL_USER")
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    if user and pw:
        return user, pw

    project_root = Path(__file__).resolve().parent
    env_data = _load_env_file(project_root / ".env")
    user = user or env_data.get("GMAIL_USER")
    pw = pw or env_data.get("GMAIL_APP_PASSWORD")

    if not user or not pw:
        raise RuntimeError(
            "Email credentials missing. Set GMAIL_USER and GMAIL_APP_PASSWORD "
            "as env vars or in <project>/.env. See README → Email Setup."
        )
    return user, pw


def markdown_to_html(text):
    """Minimal markdown → HTML for digest emails. Stdlib only.

    Supports: # / ## / ### headings, **bold**, - bullets, paragraphs, blank lines.
    Escapes user content first to prevent HTML injection.
    """
    safe = html.escape(text)
    lines = safe.split("\n")
    out = []
    para_buf = []
    in_list = False

    bold_re = re.compile(r"\*\*(.+?)\*\*")

    def inline(s):
        return bold_re.sub(r"<strong>\1</strong>", s)

    def flush_para():
        nonlocal para_buf
        if para_buf:
            content = " ".join(para_buf).strip()
            if content:
                out.append(f"<p>{inline(content)}</p>")
            para_buf = []

    def close_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for raw in lines:
        line = raw.strip()
        m = re.match(r"^(#{1,3})\s+(.+)$", line)
        if m:
            flush_para()
            close_list()
            level = len(m.group(1))
            out.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
            continue
        if line.startswith("- "):
            flush_para()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(line[2:])}</li>")
            continue
        if not line:
            flush_para()
            close_list()
            continue
        close_list()
        para_buf.append(line)

    flush_para()
    close_list()

    return (
        '<!DOCTYPE html><html><body style="font-family:-apple-system,'
        'BlinkMacSystemFont,Segoe UI,sans-serif;max-width:680px;'
        'margin:auto;padding:20px;line-height:1.5;color:#222;">'
        + "\n".join(out)
        + "</body></html>"
    )


def send_email(to, subject, body_md, from_addr=None):
    """Send a multipart/alternative (text + HTML) email via Gmail SMTP.

    Args:
        to: recipient address
        subject: subject line
        body_md: markdown body (sent as text part raw, HTML part rendered)
        from_addr: optional From override; defaults to authenticated GMAIL_USER

    Returns dict with sent=True; raises on failure.
    """
    user, pw = load_credentials()
    sender = from_addr or user

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to

    msg.attach(MIMEText(body_md, "plain", "utf-8"))
    msg.attach(MIMEText(markdown_to_html(body_md), "html", "utf-8"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as server:
        server.starttls()
        server.login(user, pw)
        server.send_message(msg)

    return {"sent": True, "to": to, "subject": subject}
