"""A session without a bound runner must persist and display its startup failure."""

from __future__ import annotations

import io
import json
import tarfile
import tempfile
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect

_RUNNER_UNAVAILABLE_MESSAGE = (
    "The runner for this session is not available — "
    "it may have failed to start. See the host logs."
)

_RUNNER_UNAVAILABLE_HEADLINE = "The session's runner failed to start on the host."

_MESSAGE = "hello from the creation window"


def _create_native_claude_session_without_runner(base_url: str) -> str:
    """Create a native wrapper session without binding a runner."""
    from omnigent._wrapper_labels import (
        CLAUDE_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.harnesses.claude_native.main import _materialize_claude_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        spec_path = _materialize_claude_agent_spec(Path(tmp))
        yaml_text = spec_path.read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        # Non-config.yaml arcname → omnigent compat translator (the spec has
        # no spec_version), matching the conftest native session factories.
        info = tarfile.TarInfo("claude-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE,
    }
    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"labels": labels})},
        files={"bundle": ("claude-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    return str(create.json()["session_id"])


def _ensure_chat_view(page: Page) -> None:
    """Select chat when the session offers a view toggle."""
    if page.get_by_test_id("view-mode-toggle").count() == 0:
        return
    segment = page.get_by_test_id("view-mode-chat")
    expect(segment).to_be_enabled(timeout=30_000)
    segment.click()


def test_first_message_fails_loudly_when_runner_never_becomes_available(
    page: Page,
    live_server: str,
) -> None:
    """A creation-window message settles as a visible, durable failed turn."""
    session_id = _create_native_claude_session_without_runner(live_server)
    try:
        page.goto(f"{live_server}/c/{session_id}")
        _ensure_chat_view(page)

        composer = page.get_by_label("Message the agent")
        expect(composer).to_be_visible(timeout=30_000)
        expect(composer).to_be_enabled(timeout=30_000)
        composer.fill(_MESSAGE)
        page.get_by_role("button", name="Send", exact=True).click()

        expect(page.get_by_text(_MESSAGE).first).to_be_visible(timeout=15_000)

        # Verify the live failure before reloading its persisted history.
        pill = page.get_by_test_id("error-pill").first
        expect(pill).to_be_visible(timeout=30_000)
        expect(page.get_by_test_id("error-headline").first).to_contain_text(
            _RUNNER_UNAVAILABLE_HEADLINE
        )
        pill.click()
        expect(page.get_by_text(_RUNNER_UNAVAILABLE_MESSAGE).first).to_be_visible(timeout=10_000)

        snap = httpx.get(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        snap.raise_for_status()
        assert snap.json()["status"] == "failed", snap.text
        items_resp = httpx.get(
            f"{live_server}/v1/sessions/{session_id}/items",
            params={"limit": 100},
            timeout=10.0,
        )
        items_resp.raise_for_status()
        items = items_resp.json()["data"]
        error_items = [i for i in items if i["type"] == "error"]
        assert [i.get("code") for i in error_items] == ["runner_failed_to_start"], items
        assert _RUNNER_UNAVAILABLE_MESSAGE in (error_items[0].get("message") or ""), error_items

        page.reload()
        expect(page.get_by_text(_MESSAGE).first).to_be_visible(timeout=30_000)
        expect(page.get_by_test_id("error-pill").first).to_be_visible(timeout=30_000)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
