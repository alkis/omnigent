"""UI journey: archiving a working session then Undoing must keep it live.

The post-archive Undo pill promises a buffer during which archiving is
reversible without side effects. On the buggy build the server tore the
session's runner down *synchronously* the moment the archive flag committed --
no delay, no coupling to the ~3s toast -- so Undo restored the archived
row/flag while the agent that was running was already gone (the client-side
undo restores the flag only; it cannot revive a stopped turn/runner).

Journey (all against the real SPA + live server):

1. open a runner-bound session and send a turn that PAUSES mid-flight on the
   mock-LLM gate, so the session is genuinely working -- ``status`` is
   ``running``/``waiting`` and nothing but a stop can end it,
2. archive the session from its sidebar row (the active session redirects home
   and pops the Undo pill),
3. click Undo well within the 3s window -- the row returns and the store row
   flips back to ``archived: false``,
4. assert the session is STILL working: the turn held on the gate can only be
   ended by a stop, so if it is no longer running the archive tore the runner
   down before Undo could preserve it.

On a build with the bug, step 4 FAILS: the detached archive-stop already
interrupted the held turn, so the restored session is idle/stopped rather than
the live session Undo promised. A soft, reversible archive keeps it running for
the whole window.
"""

from __future__ import annotations

import time

import httpx
from playwright.sync_api import Locator, Page, expect

# Statuses that mean the agent is still doing work on the runner. A turn held
# on the mock gate stays in one of these until something stops it.
_WORKING_STATUSES = frozenset({"running", "waiting"})

# Composer placeholder when the local send lifecycle is idle.
_COMPOSER_PLACEHOLDER = "Send a message…"

# How long to keep checking that the restored session stays live. Comfortably
# past the 3s undo window so a synchronous archive-stop has certainly landed,
# while a soft archive keeps the gate-held turn running the whole time.
_STILL_LIVE_WATCH_S = 8.0


def _snapshot(base_url: str, session_id: str) -> dict:
    """Return the owner-view session snapshot."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json()


def _wait_for(predicate, *, timeout_s: float, interval_s: float = 0.25) -> None:
    """Poll *predicate* until truthy or the deadline passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise AssertionError("condition not met within timeout")


def _row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row (``<li>``) for *session_id* by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _archive_from_row(page: Page, session_id: str) -> None:
    """Open a sidebar row's kebab and click Archive."""
    row = _row(page, session_id)
    expect(row).to_be_visible()
    # Hover so the desktop hover-revealed kebab trigger is interactable.
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("archive-conversation").click()


def test_undo_after_archiving_working_session_preserves_running_runner(
    page: Page,
    paused_mid_turn_session: tuple[str, str, str],
) -> None:
    """Undo within the window must restore a still-live session, not a dead one.

    :param page: Playwright page fixture (fresh context per test).
    :param paused_mid_turn_session: ``(base_url, session_id, mock_url)`` for a
        runner-bound session whose turn blocks on the mock-LLM gate.
    """
    base_url, session_id, mock_url = paused_mid_turn_session

    # Open the session and drive a turn that stalls mid-flight on the gate.
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder(_COMPOSER_PLACEHOLDER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Inspect the workspace.")
    page.get_by_role("button", name="Send", exact=True).click()

    # The turn is genuinely in flight: blocked on the gate between its two tool
    # calls, so it stays working until something stops it.
    _wait_for(
        lambda: httpx.get(f"{mock_url}/gate/pending", timeout=5.0).json()["pending"],
        timeout_s=60.0,
    )
    _wait_for(
        lambda: _snapshot(base_url, session_id).get("status") in _WORKING_STATUSES,
        timeout_s=30.0,
    )

    # Archive the (active) session from its sidebar row: it redirects home and
    # pops the Undo pill.
    _archive_from_row(page, session_id)
    expect(page.locator(f'a[href="/c/{session_id}"]')).to_have_count(0)
    pill = page.get_by_test_id("archive-undo-toast")
    expect(pill).to_be_visible()

    # Undo promptly, well within the 3s window.
    pill.get_by_test_id("archive-undo-button").click()

    # The row returns and the un-archive is durable (store row, not just cache).
    expect(page.locator(f'a[href="/c/{session_id}"]')).to_have_count(1)
    _wait_for(
        lambda: _snapshot(base_url, session_id).get("archived") is False,
        timeout_s=15.0,
    )

    # The turn was held on the gate, so it can only leave a working state if a
    # stop tore it down. Watch past the undo window: a soft archive keeps it
    # running; the bug's synchronous archive-stop has already ended it.
    deadline = time.monotonic() + _STILL_LIVE_WATCH_S
    dead_status: str | None = None
    while time.monotonic() < deadline:
        status = _snapshot(base_url, session_id).get("status")
        if status not in _WORKING_STATUSES:
            dead_status = status
            break
        time.sleep(0.5)

    assert dead_status is None, (
        "archive tore the runner down before Undo could preserve it: the "
        f"restored session is {dead_status!r}, not still running -- the held "
        "turn was interrupted synchronously on archive, so Undo returned a "
        "dead session instead of the live one it promised"
    )
