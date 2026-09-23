"""E2E: every message POST carries a stable_id for idempotent dispatch.

The client generates a 32-char hex ``stable_id`` at send time and
includes it in the POST body so the server can recognise a retry and
skip re-dispatching to the runner.  This test intercepts the
``/events`` POST and asserts the field is present with the right
format — a minimal guard that the wiring from ``send()``/
``enqueueMessage()`` through to the network layer is intact.

The server-side dedup (both dispatch paths answering a repeated
``stable_id`` without a second forward) is tested in
``tests/server/integration/test_sessions_endpoints.py``; the client-side
re-send of a message whose fetch threw is unit-tested in
``web/src/store/chatStore.test.ts``.  The tests here close the gap by
proving the field reaches the wire through the full SPA path, and that
a lost first POST is re-sent with the same id rather than shown as a
failure.
"""

from __future__ import annotations

import json
import re

from playwright.sync_api import Page, expect

_STABLE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SEND_TEXT = "sentinel-stable-id-e2e verify this goes through"
_COMPOSER_LABEL = "Message the agent"
_RETRY_TEXT = "sentinel-retry-e2e the first post never gets an answer"
_RELOAD_TEXT = "sentinel-reload-e2e this message must survive a refresh"


def test_message_post_carries_stable_id(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The events POST body includes a well-formed stable_id.

    Intercepts the first ``POST /v1/sessions/.../events`` call triggered
    by a user send and asserts:

    1. ``data.stable_id`` is present in the JSON body.
    2. It matches the 32-char lowercase hex format the server expects.

    A missing or malformed ``stable_id`` means the server-side
    idempotency check never fires, so a client POST retry would
    re-dispatch to the runner and create a duplicate turn.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    captured: list[str] = []

    def _intercept(route, request):  # type: ignore[no-untyped-def]
        if (
            f"/v1/sessions/{session_id}/events" in request.url
            and request.method == "POST"
            and not captured
        ):
            captured.append(request.post_data or "")
        route.continue_()

    page.route("**/v1/sessions/*/events", _intercept)

    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible()
    composer.fill(_SEND_TEXT)
    page.get_by_role("button", name="Send", exact=True).click()

    # Optimistic bubble confirms the send reached the client-side path.
    expect(
        page.locator('[data-testid="message-bubble"][data-role="user"]').filter(
            has_text=_SEND_TEXT
        )
    ).to_be_visible(timeout=10_000)

    assert captured, "No POST to /events was intercepted — send did not fire"
    body = json.loads(captured[0])
    stable_id = body.get("data", {}).get("stable_id")
    assert stable_id is not None, f"stable_id missing from POST body: {body}"
    assert _STABLE_ID_RE.match(stable_id), (
        f"stable_id {stable_id!r} is not a 32-char lowercase hex string"
    )


def test_lost_first_post_is_resent_with_the_same_stable_id(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A send whose first POST gets no response is re-sent, never failed.

    The first ``POST /events`` is aborted at the network layer, which the
    browser reports as a thrown fetch ("Failed to fetch"). The client must:

    1. Keep the message in the transcript as a pending bubble.
    2. Re-POST on its own with the *same* ``stable_id``, so the server can
       dedupe if the first request did land.
    3. Show no error pill and leave the composer empty.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    bodies: list[str] = []

    def _intercept(route, request):  # type: ignore[no-untyped-def]
        if f"/v1/sessions/{session_id}/events" in request.url and request.method == "POST":
            bodies.append(request.post_data or "")
            if len(bodies) == 1:
                route.abort("failed")
                return
        route.continue_()

    page.route("**/v1/sessions/*/events", _intercept)

    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible()
    composer.fill(_RETRY_TEXT)
    page.get_by_role("button", name="Send", exact=True).click()

    bubble = page.locator('[data-testid="message-bubble"][data-role="user"]').filter(
        has_text=_RETRY_TEXT
    )
    expect(bubble).to_be_visible(timeout=10_000)

    # The re-send is automatic and immediate; wait for it to reach the wire.
    for _ in range(50):
        if len(bodies) >= 2:
            break
        page.wait_for_timeout(200)
    assert len(bodies) >= 2, f"first POST was aborted but no re-send followed: {bodies}"
    stable_ids = {json.loads(body)["data"]["stable_id"] for body in bodies}
    assert len(stable_ids) == 1, f"re-send changed the stable_id: {stable_ids}"

    # Never surfaced as a failure: no error pill, the text stays in the
    # transcript (once, not duplicated), and the composer was left alone.
    expect(page.locator('[data-testid="error-pill"]')).to_have_count(0)
    expect(bubble).to_have_count(1)
    expect(composer).to_have_value("")


def test_parked_send_survives_a_reload(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A send that could not reach the server comes back after a reload.

    Every ``POST /events`` is aborted at the network layer until the page has
    been reloaded, so the send parks ("Sending · Retry · Cancel"). After the
    reload the message must still be in the transcript, and once the network
    is back it must be re-sent with the *same* ``stable_id`` — not lost, and
    not duplicated.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    stable_ids: list[str] = []
    state = {"block": True}

    def _intercept(route, request):  # type: ignore[no-untyped-def]
        if f"/v1/sessions/{session_id}/events" in request.url and request.method == "POST":
            body = json.loads(request.post_data or "{}")
            if body.get("type") == "message":
                stable_ids.append(body["data"]["stable_id"])
                if state["block"]:
                    route.abort("failed")
                    return
        route.continue_()

    page.route("**/v1/sessions/*/events", _intercept)

    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible()
    composer.fill(_RELOAD_TEXT)
    page.get_by_role("button", name="Send", exact=True).click()

    bubble = page.locator('[data-testid="message-bubble"][data-role="user"]').filter(
        has_text=_RELOAD_TEXT
    )
    expect(bubble).to_be_visible(timeout=10_000)
    expect(page.locator('[data-testid="send-delivery"][data-state="stalled"]')).to_be_visible(
        timeout=15_000
    )

    page.reload()
    # Still here after the reload, still parked (the network is still down).
    expect(bubble).to_be_visible(timeout=15_000)
    expect(page.locator('[data-testid="send-delivery"]')).to_be_visible(timeout=15_000)

    # Network back: the revived send goes through with the original id.
    state["block"] = False
    page.get_by_role("button", name="Retry").click()
    expect(page.locator('[data-testid="send-delivery"]')).to_have_count(0, timeout=15_000)
    expect(bubble).to_have_count(1)
    assert len(stable_ids) >= 2, f"expected the parked send to be re-sent: {stable_ids}"
    assert len(set(stable_ids)) == 1, f"re-send changed the stable_id: {set(stable_ids)}"
    expect(page.locator('[data-testid="error-pill"]')).to_have_count(0)
