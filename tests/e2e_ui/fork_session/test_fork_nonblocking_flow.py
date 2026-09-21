"""Browser e2e: forking is non-blocking (OMNI-7212).

The core UX the change guarantees: clicking Clone accepts the fork immediately
(HTTP 202) and CLOSES the dialog WITHOUT navigating — the user stays on the
source session while the copy runs server-side — and the finished clone then
appears in the sidebar on its own (no reload, no navigation). A non-modal
"Cloning…" toast covers the wait.

This is the regression guard for the whole feature: the old flow synchronously
copied the transcript and navigated into the clone, so the two invariants under
test here (stay on source + clone self-appears in the sidebar) both failed.

Runs against the seeded ``hello_world`` (openai-agents SDK) session, so no host
or native CLI is needed.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.e2e_ui.fork_session import list_session_ids

_SIDEBAR = '[data-testid="sidebar-conversation-list"]'


def test_fork_is_nonblocking_and_appears_in_sidebar(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Submit a fork → dialog closes, no navigation, clone self-appears.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a runner-bound
        ``hello_world`` source session.
    """
    base_url, session_id = seeded_session
    fork_title = "Non-blocking clone probe"

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder("Send a message…")).to_be_visible()

    before_ids = list_session_ids(base_url)

    # Open the full-clone dialog from the header menu and give the fork a
    # distinct title so its sidebar row is unambiguous.
    page.get_by_test_id("header-conversation-actions").click()
    page.get_by_role("menuitem", name="Fork", exact=True).click()
    dialog = page.get_by_test_id("fork-session-dialog")
    expect(dialog).to_be_visible()
    page.get_by_test_id("fork-session-advanced-toggle").click()
    title_input = page.get_by_test_id("fork-session-title-input")
    title_input.fill(fork_title)

    page.get_by_test_id("fork-session-submit").click()

    # (1) Accept is async: the dialog closes and the URL STAYS on the source.
    # This is the regression guard — the old flow navigated into the clone.
    expect(dialog).not_to_be_visible(timeout=30_000)
    expect(page).to_have_url(re.compile(rf".*/c/{re.escape(session_id)}(\?.*)?$"))

    # (2) The clone self-appears in the sidebar once the background copy
    # finishes — without any navigation or reload. Waiting on the titled row
    # naturally waits out the async materialization.
    sidebar = page.locator(_SIDEBAR)
    fork_row = sidebar.get_by_role("link", name=fork_title, exact=True)
    expect(fork_row).to_be_visible(timeout=30_000)

    # Still on the source: appearance came from the discovery push, not a nav.
    expect(page).to_have_url(re.compile(rf".*/c/{re.escape(session_id)}(\?.*)?$"))

    # A genuinely new session id was created (not just a re-render of source).
    new_ids = list_session_ids(base_url) - before_ids
    assert len(new_ids) == 1, f"expected exactly one new session, got {new_ids}"

    # (3) Clicking the row opens the clone — the destination link works once ready.
    fork_row.click()
    expect(page).to_have_url(re.compile(rf".*/c/(?!{re.escape(session_id)})[0-9a-f]{{32}}"))
