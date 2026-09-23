"""A native turn that fails must not die silently or blame its own reply.

A native harness reports a turn ``failed`` via
the ``external_session_status`` forwarder wire. Two user-visible shapes:

* **Shape 1 (detail-less failure):** the forwarder attaches no ``output`` and
  nothing is persisted to enrich it from. The server publishes ``failed`` with
  ``error: null``; the web appends an error block only when the status edge
  carries one (``chatStore.ts``), so the session flips to failed and the
  transcript shows nothing explaining why -- a silent failure.
* **Shape 3 (assistant prose as error):** the turn first produced a normal,
  successful assistant reply, then ends ``failed`` with no reason of its own.
  The server backfills the "error" from the latest persisted assistant text
  (``_enrich_terminal_status_with_subagent_output``), so the error pill's
  message is the assistant's own successful sentence -- a false failure whose
  "reason" is the reply itself.

Both are driven through the real ``/v1/sessions/{id}/events`` route the codex
native forwarder posts to, on a genuine ``codex-native`` session; only the
codex process's own terminal edge (``external_session_status``) is injected,
exactly the payload ``_post_turn_status_edge`` derives from an error-less
rejection (``output=None``). The assertions encode the fixed contract, so on
the current build they FAIL -- reproducing the silent failure and the
prose-as-error pill -- and a fix that surfaces a readable, non-prose reason
turns them green.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _create_native_codex_session,
    _ensure_runner_online,
    _server_state,
)

_WORKING = '[data-testid="working-indicator"]'
_ERROR_PILL = '[data-testid="error-pill"]'
_ERROR_MESSAGE = '[data-testid="error-message-content"]'

# The server stamps this code on a native failure surfaced from the store /
# fallback; its pill headline is unique to a surfaced native turn error, so an
# ambient error pill (e.g. a codex CLI failing its own launch during adoption)
# cannot satisfy the assertion.
_NATIVE_FAILURE_HEADLINE = "The agent ran into an error during this turn."

_SUCCESS_PROSE = "All conflicts resolved. Continue the sync:"


def _publish_native_status(
    base_url: str,
    session_id: str,
    status: str,
    *,
    response_id: str,
    output: str | None = None,
) -> None:
    """Post the status payload the codex-native forwarder sends.

    A codex rejection that ends the turn with a bare failed status carries no
    ``output`` -- the detail-less shape driven here.
    """
    data: dict[str, str] = {"status": status, "response_id": response_id}
    if output is not None:
        data["output"] = output
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": data},
        timeout=15.0,
    )
    resp.raise_for_status()


def _publish_assistant_message(
    base_url: str, session_id: str, text: str, *, response_id: str
) -> None:
    """Persist a normal, successful assistant reply for the in-flight turn."""
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {"agent": "assistant", "text": text, "response_id": response_id},
        },
        timeout=15.0,
    )
    resp.raise_for_status()


def test_detail_less_native_failure_surfaces_readable_error(
    page: Page,
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Shape 1: a native turn that fails with no detail must not fail silently."""
    _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    session_id = _create_native_codex_session(live_server, runner_id)
    try:
        page.goto(f"{live_server}/c/{session_id}")
        expect(page.get_by_role("textbox", name="Message the agent")).to_be_visible(timeout=20_000)

        working = page.locator(_WORKING)
        pills = page.locator(_ERROR_PILL)

        _publish_native_status(live_server, session_id, "running", response_id="turn_no_detail")
        expect(working).to_be_visible(timeout=15_000)

        # Codex ends the turn failed with NO detail: a bare failed edge with no
        # output -- the payload the forwarder derives from an error-less reject.
        _publish_native_status(live_server, session_id, "failed", response_id="turn_no_detail")
        expect(working).to_have_count(0, timeout=15_000)

        # The failure must be SURFACED, not swallowed. While the bug is live the
        # published status edge's ``error`` is null and the web appends an error
        # block only when the edge carries one, so no pill renders and this
        # times out -- the silent failure. After a fix a readable fallback pill
        # appears and this passes.
        native_failure_pill = pills.filter(has_text=_NATIVE_FAILURE_HEADLINE)
        expect(native_failure_pill.first).to_be_visible(timeout=15_000)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)


def test_native_failure_does_not_show_assistant_reply_as_the_error(
    page: Page,
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Shape 3: a successful reply must never be published as the failure reason."""
    _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    session_id = _create_native_codex_session(live_server, runner_id)
    try:
        page.goto(f"{live_server}/c/{session_id}")
        expect(page.get_by_role("textbox", name="Message the agent")).to_be_visible(timeout=20_000)

        working = page.locator(_WORKING)
        pills = page.locator(_ERROR_PILL)

        _publish_native_status(live_server, session_id, "running", response_id="turn_prose")
        expect(working).to_be_visible(timeout=15_000)

        # The turn produces a normal, successful assistant reply...
        _publish_assistant_message(
            live_server, session_id, _SUCCESS_PROSE, response_id="turn_prose"
        )
        # ...and is then labelled failed with no reason of its own.
        _publish_native_status(live_server, session_id, "failed", response_id="turn_prose")
        expect(working).to_have_count(0, timeout=15_000)

        # An error pill appears because the server backfilled the "error" from
        # the assistant's own text. Expand it and read the reason.
        error_pill = pills.filter(has_text=_NATIVE_FAILURE_HEADLINE).first
        expect(error_pill).to_be_visible(timeout=15_000)
        error_pill.click()
        message = error_pill.locator(_ERROR_MESSAGE)
        expect(message).to_be_visible(timeout=10_000)

        # THIS IS THE BUG: while it is live the pill's reason IS the assistant's
        # own successful sentence, verbatim. A successful reply must not read as
        # the failure reason, so the surfaced reason must not be exactly that
        # sentence. Fails on the current build (reason == the reply); passes
        # once the store-enriched reason is labelled rather than presented raw.
        assert message.inner_text().strip() != _SUCCESS_PROSE, (
            "the failed turn's surfaced reason is the assistant's own successful "
            "message verbatim; a successful reply must not be shown as the error"
        )
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
