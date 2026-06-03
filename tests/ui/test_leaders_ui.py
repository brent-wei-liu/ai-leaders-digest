"""End-to-end UI regression tests for ai-leaders-digest."""
import re

import pytest
from playwright.sync_api import Page, expect


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _open(page: Page, base_url: str):
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_selector(".tweet", timeout=10_000)


def _starred_count(page: Page) -> int:
    """Read the 'Starred N' value from the header stats strip, or 0 if missing.
    The CSS uppercases the label so the rendered text is 'STARRED 1' — match
    case-insensitively."""
    stat = page.locator("#stats-strip .stat", has_text=re.compile("starred", re.I)).first
    if stat.count() == 0:
        return 0
    text = stat.inner_text()
    m = re.search(r"(?i)starred\s+(\d[\d,]*)", text)
    return int(m.group(1).replace(",", "")) if m else 0


# ---------------------------------------------------------------------
# A. Tab switching
# ---------------------------------------------------------------------

def test_tab_switch(page: Page, base_url: str):
    _open(page, base_url)

    # Default: tweets active, digests hidden
    expect(page.locator("#tab-tweets")).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator("#view-tweets")).to_be_visible()
    expect(page.locator("#view-digests")).to_be_hidden()

    page.locator("#tab-digests").click()
    expect(page.locator("#tab-digests")).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator("#view-digests")).to_be_visible()
    expect(page.locator("#view-tweets")).to_be_hidden()

    page.locator("#tab-tweets").click()
    expect(page.locator("#view-tweets")).to_be_visible()
    expect(page.locator("#view-digests")).to_be_hidden()


# ---------------------------------------------------------------------
# B. Star toggle persists across reload
# ---------------------------------------------------------------------

def test_star_toggle_persists(page: Page, base_url: str):
    _open(page, base_url)

    first = page.locator(".tweet").first
    tweet_id = first.get_attribute("data-tweet-id")
    star_btn = first.locator(".star-btn")

    # Start clean — if already starred, unstar first
    if "starred" in (star_btn.get_attribute("class") or ""):
        star_btn.click()
        page.wait_for_function(
            "(id) => !document.querySelector('[data-tweet-id=\"' + id + '\"] .star-btn')"
            ".classList.contains('starred')",
            arg=tweet_id, timeout=3_000,
        )

    before = _starred_count(page)

    star_btn.click()
    page.wait_for_function(
        "(id) => document.querySelector('[data-tweet-id=\"' + id + '\"] .star-btn')"
        ".classList.contains('starred')",
        arg=tweet_id, timeout=3_000,
    )

    # Reload — DB persistence
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector(f".tweet[data-tweet-id=\"{tweet_id}\"]", timeout=10_000)
    star_after = page.locator(f".tweet[data-tweet-id=\"{tweet_id}\"] .star-btn")
    assert "starred" in (star_after.get_attribute("class") or ""), (
        "star did not persist across reload"
    )
    after = _starred_count(page)
    assert after == before + 1, f"starred count {before} -> {after}, expected +1"

    # Cleanup
    star_after.click()
    page.wait_for_function(
        "(id) => !document.querySelector('[data-tweet-id=\"' + id + '\"] .star-btn')"
        ".classList.contains('starred')",
        arg=tweet_id, timeout=3_000,
    )


# ---------------------------------------------------------------------
# C. Starred-only filter
# ---------------------------------------------------------------------

def test_starred_only_filter(page: Page, base_url: str):
    _open(page, base_url)

    first = page.locator(".tweet").first
    tweet_id = first.get_attribute("data-tweet-id")

    # Star the first tweet
    star_btn = first.locator(".star-btn")
    if "starred" not in (star_btn.get_attribute("class") or ""):
        star_btn.click()
        page.wait_for_function(
            "(id) => document.querySelector('[data-tweet-id=\"' + id + '\"] .star-btn')"
            ".classList.contains('starred')",
            arg=tweet_id, timeout=3_000,
        )

    # Click ★ Starred only toggle
    toggle = page.locator("#starred-toggle")
    toggle.click()
    expect(toggle).to_have_class(re.compile(r"\bactive\b"), timeout=3_000)

    # Wait for the filtered list to settle (the starred tweet must be present)
    page.wait_for_function(
        f"() => document.querySelectorAll('.tweet[data-tweet-id=\"{tweet_id}\"]').length === 1",
        timeout=5_000,
    )
    # All visible tweets must be starred
    tweets = page.locator(".tweet").all()
    assert len(tweets) >= 1
    for t in tweets:
        cls = t.locator(".star-btn").get_attribute("class") or ""
        assert "starred" in cls, "non-starred tweet visible while filter is active"

    # Toggle off
    toggle.click()
    expect(toggle).not_to_have_class(re.compile(r"\bactive\b"), timeout=3_000)
    page.wait_for_function(
        "() => document.querySelectorAll('.tweet').length > 1",
        timeout=5_000,
    )

    # Cleanup
    page.locator(f".tweet[data-tweet-id=\"{tweet_id}\"] .star-btn").click()


# ---------------------------------------------------------------------
# D. Author filter dropdown
# ---------------------------------------------------------------------

def test_author_filter(page: Page, base_url: str):
    _open(page, base_url)

    initial_count = page.locator(".tweet").count()
    if initial_count < 2:
        pytest.skip("need at least 2 tweets to validate author filter")

    # Pick an author handle that has at least 2 tweets in the first page so
    # the filter result is meaningfully different from the unfiltered view.
    author_handles = page.evaluate(
        "() => Array.from(document.querySelectorAll('.tweet .author-name'))"
        ".map(el => el.dataset.handle)"
    )
    # Find first handle that appears more than once OR fall back to the first one
    counts: dict[str, int] = {}
    for h in author_handles:
        counts[h] = counts.get(h, 0) + 1
    target_handle = max(counts, key=counts.get)

    page.select_option("#author-filter", target_handle)
    # Wait for filter: every visible tweet must match
    page.wait_for_function(
        "(handle) => { "
        " const tweets = Array.from(document.querySelectorAll('.tweet .handle'));"
        " return tweets.length > 0 && tweets.every(t => t.innerText.replace('@', '').trim() === handle); "
        "}",
        arg=target_handle, timeout=5_000,
    )

    # Switch back to "All authors"
    page.select_option("#author-filter", "")
    page.wait_for_function(
        "() => { "
        " const handles = new Set(Array.from(document.querySelectorAll('.tweet .handle'))"
        "   .map(t => t.innerText.replace('@', '').trim())); "
        " return handles.size > 1; "
        "}",
        timeout=5_000,
    )


# ---------------------------------------------------------------------
# E. Search filter
# ---------------------------------------------------------------------

def test_search_filter(page: Page, base_url: str):
    _open(page, base_url)

    initial_count = page.locator(".tweet").count()
    if initial_count < 2:
        pytest.skip("need at least 2 tweets to validate search filter")

    # Pick a word from the first tweet's text — we know it'll match itself
    first_text = page.locator(".tweet-text").first.inner_text().strip()
    # Take the longest token >= 4 chars to maximize selectivity
    tokens = [t for t in re.split(r"\s+", first_text) if len(t) >= 4]
    if not tokens:
        pytest.skip("first tweet has no token >= 4 chars to search for")
    term = tokens[0]

    page.fill("#search", term)
    # Wait for debounce + fetch + render
    page.wait_for_function(
        f"(t) => {{ "
        f" const tweets = document.querySelectorAll('.tweet'); "
        f" return tweets.length > 0 && Array.from(tweets).every(tw => "
        f"   tw.querySelector('.tweet-text').innerText.toLowerCase().includes(t.toLowerCase())); "
        f"}}",
        arg=term, timeout=5_000,
    )
    after_count = page.locator(".tweet").count()
    assert after_count >= 1
    assert after_count <= initial_count

    # Clear search
    page.fill("#search", "")
    page.wait_for_function(
        f"() => document.querySelectorAll('.tweet').length >= {initial_count}",
        timeout=5_000,
    )


# ---------------------------------------------------------------------
# F. Digest drawer opens with rendered HTML content
# ---------------------------------------------------------------------

def test_digest_drawer_opens(page: Page, base_url: str):
    _open(page, base_url)

    page.locator("#tab-digests").click()
    expect(page.locator("#tab-digests")).to_have_class(re.compile(r"\bactive\b"))

    # Wait for the actual rows (container exists empty during loader)
    try:
        page.wait_for_selector(".digest-row", timeout=5_000)
    except Exception:
        pytest.skip("no digests in DB to open")

    page.locator(".digest-row .btn", has_text="Open").first.click()
    backdrop = page.locator("#modal-backdrop.open")
    expect(backdrop).to_be_visible(timeout=3_000)

    # Wait past the loader — modal-body initially shows "unfurling…"
    page.wait_for_function(
        "() => { const b = document.getElementById('modal-body'); "
        "return b && b.innerText && b.innerText.length > 100 "
        "&& !b.querySelector('.loader'); }",
        timeout=5_000,
    )
    body = page.locator("#modal-body")
    text = body.inner_text()
    assert len(text) >= 100, f"modal opened but content too short ({len(text)} chars)"

    # Content should be rendered HTML, not raw markdown
    rendered = body.evaluate(
        "(el) => Boolean(el.querySelector('h1, h2, h3, p, ul, strong'))"
    )
    assert rendered, "modal body has no rendered HTML elements — looks like raw markdown"

    # Close
    page.locator("#modal-close").click()
    expect(backdrop).not_to_be_visible(timeout=3_000)


# ---------------------------------------------------------------------
# G. Stats panel updates on star/unstar without reload
# ---------------------------------------------------------------------

def test_stats_panel_updates(page: Page, base_url: str):
    _open(page, base_url)

    first = page.locator(".tweet").first
    tweet_id = first.get_attribute("data-tweet-id")
    star_btn = first.locator(".star-btn")

    # Start clean
    if "starred" in (star_btn.get_attribute("class") or ""):
        star_btn.click()
        page.wait_for_function(
            "(id) => !document.querySelector('[data-tweet-id=\"' + id + '\"] .star-btn')"
            ".classList.contains('starred')",
            arg=tweet_id, timeout=3_000,
        )
        # Wait for stats refetch to settle before snapshotting baseline
        page.wait_for_timeout(400)

    initial = _starred_count(page)

    star_btn.click()
    # Stats refetch is fire-and-forget after toggleStar; poll for the bump.
    page.wait_for_function(
        "(prev) => { "
        " const stat = Array.from(document.querySelectorAll('#stats-strip .stat'))"
        "   .find(el => /Starred/i.test(el.innerText));"
        " if (!stat) return false; "
        " const m = stat.innerText.match(/Starred\\s+(\\d+)/i); "
        " return m && parseInt(m[1]) === prev + 1; "
        "}",
        arg=initial, timeout=5_000,
    )

    # Now unstar — stat should decrement
    page.locator(f".tweet[data-tweet-id=\"{tweet_id}\"] .star-btn").click()
    page.wait_for_function(
        "(prev) => { "
        " const stat = Array.from(document.querySelectorAll('#stats-strip .stat'))"
        "   .find(el => /Starred/i.test(el.innerText));"
        " if (!stat) return false; "
        " const m = stat.innerText.match(/Starred\\s+(\\d+)/i); "
        " return m && parseInt(m[1]) === prev; "
        "}",
        arg=initial, timeout=5_000,
    )


# ---------------------------------------------------------------------
# H. Digest read/unread — opening a digest marks it read; toggle works
# ---------------------------------------------------------------------

def test_digest_mark_read_persists(page: Page, base_url: str):
    """Opening a digest drawer should auto-mark it read (via the POST
    /api/summaries/{id}/read endpoint). After reload, the same digest
    row must render with .is-read class. Tested end-to-end against the
    real API + DB."""
    _open(page, base_url)
    page.locator(".tab[data-tab='digests']").click()
    try:
        page.wait_for_selector(".digest-row", timeout=5_000)
    except Exception:
        pytest.skip("no digests in DB to test")

    # Pick an unread digest if present; otherwise mark the first one
    # unread via the API so the test has a known starting state.
    first = page.locator(".digest-row").first
    digest_id = first.get_attribute("data-id")
    # Reset to unread via API (idempotent)
    page.evaluate(
        "(id) => fetch('/api/summaries/' + id + '/unread', {method:'POST'})",
        arg=digest_id,
    )
    page.reload(wait_until="domcontentloaded")
    page.locator(".tab[data-tab='digests']").click()
    page.wait_for_selector(".digest-row", timeout=5_000)
    row = page.locator(f".digest-row[data-id='{digest_id}']")
    expect(row).to_have_class(re.compile(r"\bis-unread\b"), timeout=3_000)

    # Open the drawer — auto-mark-read fires
    row.locator("[data-action='open']").click()
    page.wait_for_function(
        "(id) => document.querySelector(`.digest-row[data-id='${id}']`)?.dataset.isRead === '1'",
        arg=digest_id, timeout=3_000,
    )
    expect(row).to_have_class(re.compile(r"\bis-read\b"), timeout=2_000)

    # Close the drawer
    page.locator("#modal-close").click()
    page.wait_for_function(
        "() => !document.querySelector('.modal-backdrop.open')",
        timeout=2_000,
    )

    # Reload — DB persistence check
    page.reload(wait_until="domcontentloaded")
    page.locator(".tab[data-tab='digests']").click()
    page.wait_for_selector(f".digest-row[data-id='{digest_id}']", timeout=5_000)
    row_after = page.locator(f".digest-row[data-id='{digest_id}']")
    expect(row_after).to_have_class(re.compile(r"\bis-read\b"), timeout=3_000)


def test_digest_unread_toggle(page: Page, base_url: str):
    """The modal carries a 'mark unread' button that flips state back.
    Verify both directions: open (auto-read) → click 'mark unread' →
    row reverts to is-unread, API confirms is_read=0."""
    _open(page, base_url)
    page.locator(".tab[data-tab='digests']").click()
    try:
        page.wait_for_selector(".digest-row", timeout=5_000)
    except Exception:
        pytest.skip("no digests in DB to test")

    first = page.locator(".digest-row").first
    digest_id = first.get_attribute("data-id")
    # Force unread starting state
    page.evaluate(
        "(id) => fetch('/api/summaries/' + id + '/unread', {method:'POST'})",
        arg=digest_id,
    )
    page.reload(wait_until="domcontentloaded")
    page.locator(".tab[data-tab='digests']").click()
    page.wait_for_selector(f".digest-row[data-id='{digest_id}']", timeout=5_000)

    # Open → auto-read
    page.locator(f".digest-row[data-id='{digest_id}'] [data-action='open']").click()
    btn = page.locator("#modal-read-toggle")
    expect(btn).to_be_visible(timeout=3_000)
    expect(btn).to_have_text(re.compile(r"mark unread", re.I), timeout=3_000)

    # Click the toggle → mark unread
    btn.click()
    expect(btn).to_have_text(re.compile(r"mark read", re.I), timeout=3_000)
    # And API now reports is_read=0
    final_state = page.evaluate(
        "async (id) => (await (await fetch('/api/summaries/' + id)).json()).is_read",
        arg=digest_id,
    )
    assert final_state == 0, f"after toggle the API should report is_read=0, got {final_state}"

    # Close the drawer cleanly
    page.locator("#modal-close").click()


# ---------------------------------------------------------------------
# J. Back-to-top button — appears on scroll, smooth-scrolls to top
# ---------------------------------------------------------------------

def test_digest_back_to_top(page: Page, base_url: str):
    """Open a digest, scroll the modal-backdrop past the threshold,
    assert the back-to-top button becomes visible. Click it → scrollTop
    returns to 0 and the button hides again."""
    _open(page, base_url)
    page.locator(".tab[data-tab='digests']").click()
    try:
        page.wait_for_selector(".digest-row", timeout=5_000)
    except Exception:
        pytest.skip("no digests in DB to test")

    page.locator(".digest-row").first.locator("[data-action='open']").click()
    page.wait_for_selector(".modal-backdrop.open", timeout=3_000)
    # Wait for the rendered digest body (not the loader) so the modal
    # actually has scrollable content
    page.wait_for_function(
        "() => { const b = document.getElementById('modal-body');"
        " return b && b.innerText && b.innerText.length > 100; }",
        timeout=5_000,
    )

    btn = page.locator("#modal-top-btn")
    # Pre-scroll: button is in DOM but NOT visible (no .visible class).
    # If the digest happens to be too short to scroll past 300px, skip.
    backdrop = page.locator("#modal-backdrop")
    max_scroll = page.evaluate(
        "const b = document.getElementById('modal-backdrop'); b.scrollHeight - b.clientHeight"
    )
    if max_scroll < 400:
        pytest.skip(f"digest too short to test back-to-top (max scroll = {max_scroll}px)")

    expect(btn).not_to_have_class(re.compile(r"\bvisible\b"))

    # Scroll past the 300px threshold
    page.evaluate(
        "const b = document.getElementById('modal-backdrop'); "
        "b.scrollTo({top: Math.min(b.scrollHeight - b.clientHeight, 600), behavior: 'instant'});"
    )
    page.wait_for_function(
        "() => document.getElementById('modal-top-btn').classList.contains('visible')",
        timeout=2_000,
    )
    expect(btn).to_have_class(re.compile(r"\bvisible\b"))

    # Click → smooth-scroll back to 0
    btn.click()
    page.wait_for_function(
        "() => document.getElementById('modal-backdrop').scrollTop < 10",
        timeout=2_000,
    )
    # And the button should hide once we're back near the top
    page.wait_for_function(
        "() => !document.getElementById('modal-top-btn').classList.contains('visible')",
        timeout=2_000,
    )

    # Cleanup
    page.locator("#modal-close").click()
