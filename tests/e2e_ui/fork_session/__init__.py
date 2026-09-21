"""Shared helpers for the async (non-blocking) fork e2e flow.

Fork materialization is a background server operation: submitting the dialog
returns 202 and CLOSES the dialog WITHOUT navigating — the clone appears in the
sidebar (and a "Cloning…" toast flips to success) once the copy finishes. These
helpers replace the old "submit → navigate into the fork" assumption: they
assert the no-navigation invariant, then resolve the fork id and open it so a
test's downstream assertions (transcript, labels, recall) run against the clone.
"""

from __future__ import annotations

import re

import httpx
from playwright.sync_api import Page, expect


def list_session_ids(base_url: str) -> set[str]:
    """Return the caller's current session ids (``GET /v1/sessions``)."""
    resp = httpx.get(f"{base_url}/v1/sessions", params={"limit": 100}, timeout=10.0)
    resp.raise_for_status()
    return {row["id"] for row in resp.json().get("data", [])}


def submit_fork_and_open_clone(
    page: Page,
    base_url: str,
    source_id: str,
    *,
    before_ids: set[str],
    timeout_ms: int = 30_000,
) -> str:
    """Click the fork dialog's submit, assert async no-nav, then open the clone.

    The submit returns 202 and the dialog closes without navigation, so the
    page must STAY on the source. The clone is then discovered from the session
    list (the deterministic mirror of the sidebar's discovery push — the seeded
    sources are untitled, so a title-based sidebar lookup would be ambiguous)
    and opened by navigating to it.

    :param page: The Playwright page, on the source session's URL.
    :param base_url: Live server base URL, e.g. ``"http://127.0.0.1:51234"``.
    :param source_id: The session being forked (stays the URL after submit).
    :param before_ids: Session ids that existed BEFORE submit — the fork is the
        one id that appears on top of this set. Capture via
        :func:`list_session_ids` right before opening/submitting the dialog.
    :param timeout_ms: How long to wait for the background fork to appear.
    :returns: The new fork/conversation id.
    """
    dialog = page.get_by_test_id("fork-session-dialog")
    page.get_by_test_id("fork-session-submit").click()

    # Dialog closes on accept and the URL stays on the source — the whole
    # point of the non-blocking change (no navigation into the clone).
    expect(dialog).not_to_be_visible(timeout=timeout_ms)
    expect(page).to_have_url(re.compile(rf".*/c/{re.escape(source_id)}(\?.*)?$"))

    fork_id = _wait_for_new_session_id(base_url, before_ids, timeout_ms=timeout_ms)
    page.goto(f"{base_url}/c/{fork_id}")
    return fork_id


def _wait_for_new_session_id(base_url: str, before_ids: set[str], *, timeout_ms: int) -> str:
    """Poll the session list until a new id (the committed fork) appears."""
    import time

    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        new = list_session_ids(base_url) - before_ids
        if new:
            # A single fork adds exactly one row; if more than one appears
            # (shouldn't in these single-fork tests), take any — they're all
            # post-baseline and the caller forked once.
            return sorted(new)[0]
        time.sleep(0.25)
    raise AssertionError(
        f"fork never appeared as a new session within {timeout_ms}ms "
        f"(baseline had {len(before_ids)} sessions)"
    )
