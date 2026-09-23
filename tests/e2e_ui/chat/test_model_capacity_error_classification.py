"""A real server, runner and SPA preserve rate-limit details from a scripted HTTP 429."""

from __future__ import annotations

import time
from typing import Any

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm

_CAPACITY_MESSAGE = "Selected model is at capacity. Please try a different model."
_CAPACITY_SIGNATURE = "Selected model is at capacity"
# Content matching routes retries to the same scripted outage.
_TRIGGER = "Summarize the release notes for me"
# Cover SDK retries and concurrent title generation with the same outage.
_CAPACITY_RESPONSES = 16

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_TURN_SETTLE_TIMEOUT_S = 90.0


def _settled_turn_outcome(base_url: str, session_id: str) -> dict[str, Any] | None:
    """Return the persisted failure, or None after an assistant reply.

    Ignore informational notices; raise AssertionError if the turn never settles."""
    deadline = time.monotonic() + _TURN_SETTLE_TIMEOUT_S
    while time.monotonic() < deadline:
        resp = httpx.get(
            f"{base_url}/v1/sessions/{session_id}/items",
            params={"limit": 200, "order": "asc"},
            timeout=10.0,
        )
        resp.raise_for_status()
        items = resp.json().get("data", [])
        for item in items:
            if item.get("type") != "error":
                continue
            data = item.get("data") or {}
            if str(data.get("level") or item.get("level") or "") == "info":
                continue
            return item
        for item in items:
            if item.get("type") != "message":
                continue
            role = item.get("role") or (item.get("data") or {}).get("role")
            if role == "assistant":
                return None
        time.sleep(0.5)
    raise AssertionError(
        f"turn never settled within {_TURN_SETTLE_TIMEOUT_S:.0f}s: no error item "
        "and no assistant reply were persisted"
    )


def test_model_capacity_429_preserves_structured_error_reason(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Provider capacity failures retain their semantic code and message."""
    base_url, session_id = seeded_session
    configure_mock_llm(
        mock_llm_server_url,
        [{"error": _CAPACITY_MESSAGE, "status_code": 429}] * _CAPACITY_RESPONSES,
        key="model-at-capacity",
        match=_TRIGGER,
    )

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_role("textbox", name="Message the agent")
    expect(composer).to_be_visible(timeout=15_000)

    composer.fill(_TRIGGER)
    page.get_by_role("button", name="Send", exact=True).click()

    error_item = _settled_turn_outcome(base_url, session_id)

    if error_item is None:
        # A recovered turn has no error to classify.
        expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=15_000)
        return

    # The visible error must correspond to the injected capacity failure.
    data = error_item.get("data") or {}
    message = str(data.get("message") or error_item.get("message") or "")
    code = str(data.get("code") or error_item.get("code") or "")
    assert _CAPACITY_SIGNATURE in message, (
        f"the failed turn's error does not carry the upstream capacity reason; "
        f"got code={code!r} message={message!r}"
    )
    expect(page.get_by_test_id("error-pill").first).to_be_visible(timeout=15_000)

    assert code == "rate_limit_exceeded", (
        "a model-capacity 429 must fail the turn with the structured "
        "provider-throttle code 'rate_limit_exceeded'; got unclassified "
        f"code={code!r} (message={message!r})"
    )
