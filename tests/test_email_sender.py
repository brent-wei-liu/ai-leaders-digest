import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import email_sender

SECRET = "abcd efgh ijkl mnop"


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.address = (host, port)
        self.tls = False
        self.login_args = None
        self.sent = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.tls = True

    def ehlo(self):
        self.esmtp_features = {"auth": " LOGIN PLAIN XOAUTH2"}

    def login(self, user, password):
        assert self.tls, "must STARTTLS before login"
        self.login_args = (user, password)

    def send_message(self, msg):
        self.sent = msg


class GmailRejectingSMTP(email_sender.smtplib.SMTP):
    """Real smtplib auth logic against a server that hangs up after one 535, as Gmail does."""

    def __init__(self, host, port, timeout=None):
        super().__init__()
        self.auth_attempts = 0

    def starttls(self, context=None):
        pass

    def ehlo(self, name=""):
        self.ehlo_resp = b"ok"
        self.does_esmtp = True
        self.esmtp_features = {"auth": " LOGIN PLAIN XOAUTH2"}
        return (250, b"ok")

    def docmd(self, cmd, args=""):
        if cmd == "AUTH" and self.auth_attempts == 0:
            self.auth_attempts += 1
            return (535, b"5.7.8 Username and Password not accepted. BadCredentials")
        raise email_sender.smtplib.SMTPServerDisconnected("Connection unexpectedly closed")


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in ("DIGEST_RECIPIENT", "GMAIL_SMTP_USER", "GMAIL_APP_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("DIGEST_RECIPIENT=to@example.com\n")
    monkeypatch.setattr(email_sender, "ENV_PATH", env_file)

    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE summaries (id INTEGER PRIMARY KEY, date TEXT, content TEXT)")
    conn.executemany(
        "INSERT INTO summaries (date, content) VALUES (?, ?)",
        [("2026-01-01", "# old"), ("2026-01-02", "# first"), ("2026-01-02", "# 最新 **b**")],
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(email_sender, "DB_PATH", str(db))

    monkeypatch.setattr(email_sender, "SENT_LOG_PATH", tmp_path / "email_sent.jsonl")
    FakeSMTP.instances = []
    monkeypatch.setattr(email_sender.smtplib, "SMTP", FakeSMTP)
    return env_file


def add_creds(env_file):
    with env_file.open("a") as f:
        f.write(f'GMAIL_SMTP_USER=me@example.com\nGMAIL_APP_PASSWORD="{SECRET}"\n')


def test_renderer_escapes_and_formats():
    out = email_sender.md_to_html("## T\n\n- **b** `c` [l](https://x.y/?a=1&b=2)\n\n<script>")
    assert "<h2>T</h2>" in out
    assert '<li><strong>b</strong> <code>c</code> <a href="https://x.y/?a=1&amp;b=2">l</a></li>' in out
    assert "<script>" not in out


def test_dry_run_needs_no_creds_and_never_connects(env, capsys):
    assert email_sender.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Subject: AI Leaders Digest 2026-01-02" in out
    assert "GMAIL_APP_PASSWORD: MISSING" in out
    assert FakeSMTP.instances == []


def test_dry_run_does_not_print_password(env, capsys):
    add_creds(env)
    assert email_sender.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "GMAIL_APP_PASSWORD: set" in out
    assert SECRET not in out


def test_missing_creds_is_a_clear_error(env, capsys):
    assert email_sender.main([]) == 2
    assert "GMAIL_SMTP_USER and GMAIL_APP_PASSWORD" in json.loads(capsys.readouterr().out)["error"]
    assert FakeSMTP.instances == []


def test_send_uses_starttls_and_latest_row_for_date(env, capsys):
    add_creds(env)
    assert email_sender.main(["--date", "2026-01-02"]) == 0
    smtp = FakeSMTP.instances[0]
    assert smtp.address == ("smtp.gmail.com", 587)
    assert smtp.login_args == ("me@example.com", "".join(SECRET.split()))
    plain, rendered = smtp.sent.get_payload()
    assert plain.get_payload(decode=True).decode() == "# 最新 **b**"
    assert "<strong>b</strong>" in rendered.get_payload(decode=True).decode()
    assert SECRET not in capsys.readouterr().out


def test_orphan_file_gets_subject_prefix(env, tmp_path):
    add_creds(env)
    orphan = tmp_path / "orphan.md"
    orphan.write_text("# lost")
    assert email_sender.main(["--file", str(orphan), "--orphan", "--date", "2026-01-03"]) == 0
    assert FakeSMTP.instances[0].sent["Subject"] == "[ORPHAN: not in DB] AI Leaders Digest 2026-01-03"


def test_unknown_date_and_smtp_errors_exit_1(env, monkeypatch, capsys):
    add_creds(env)
    assert email_sender.main(["--date", "1999-01-01"]) == 1

    def boom(*a, **k):
        raise email_sender.smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(FakeSMTP, "login", boom)
    assert email_sender.main([]) == 1
    assert SECRET not in capsys.readouterr().out


def test_second_send_for_date_is_skipped_unless_forced(env, capsys):
    add_creds(env)
    assert email_sender.main(["--date", "2026-01-02"]) == 0
    capsys.readouterr()

    assert email_sender.main(["--date", "2026-01-02"]) == 0
    assert json.loads(capsys.readouterr().out)["skipped"] is True
    assert len(FakeSMTP.instances) == 1

    assert email_sender.main(["--date", "2026-01-01"]) == 0
    assert len(FakeSMTP.instances) == 2

    assert email_sender.main(["--date", "2026-01-02", "--force"]) == 0
    assert len(FakeSMTP.instances) == 3


def test_failed_send_is_not_logged(env, monkeypatch):
    add_creds(env)

    def boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(FakeSMTP, "login", boom)
    assert email_sender.main(["--date", "2026-01-02"]) == 1
    assert email_sender.find_sent("2026-01-02") is None


def test_bad_password_reports_535_not_disconnect(env, monkeypatch, capsys):
    add_creds(env)
    monkeypatch.setattr(email_sender.smtplib, "SMTP", GmailRejectingSMTP)
    assert email_sender.main(["--date", "2026-01-02"]) == 1
    out = capsys.readouterr().out
    error = json.loads(out)["error"]
    assert "SMTPAuthenticationError" in error
    assert "535" in error and "BadCredentials" in error
    assert "".join(SECRET.split()) not in out
    assert email_sender.find_sent("2026-01-02") is None


def test_renderer_handles_rules_and_only_links_http():
    out = email_sender.md_to_html("a\n\n---\n\n[ok](https://x.example/a) [bad](javascript:alert(1))")
    assert "<hr>" in out
    assert '<a href="https://x.example/a">ok</a>' in out
    assert "href=\"javascript" not in out
