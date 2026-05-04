"""Shared fixtures for the Playwright UI test suite.

Assumes `python3 api.py` is already running on port 8081. Tests do NOT
spawn the server themselves (port-binding races + zombie-process pain).
If the server isn't reachable, the suite is skipped with a clear reason.
"""
import os

import pytest
import urllib.request
import urllib.error


def _server_reachable(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url + "/api/stats", timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


@pytest.fixture(scope="session")
def base_url() -> str:
    return os.environ.get("AI_LEADERS_URL", "http://127.0.0.1:8081")


@pytest.fixture(scope="session", autouse=True)
def _require_server(base_url: str):
    if not _server_reachable(base_url):
        pytest.skip(
            f"ai-leaders-digest API not reachable at {base_url} — "
            "run `python3 api.py` in another terminal first.",
            allow_module_level=True,
        )
