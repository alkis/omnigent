"""Opening a dormant native session's model picker prepares its terminal."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import seed_committed_turn


@dataclass
class _NativeSession:
    running: bool = False
    mutations: list[tuple[str, dict]] = field(default_factory=list)
    pending_retries: list[Route] = field(default_factory=list)

    def finish_resume(self) -> None:
        """Complete the server's terminal-readiness acknowledgement."""
        self.running = True
        self.pending_retries.pop().fulfill(
            json={"queued": False, "recovered": True, "recovery": "runner_relaunched"},
        )


def _mock_native_session(
    page: Page,
    base_url: str,
    session_id: str,
    *,
    running: bool = False,
) -> _NativeSession:
    """Keep the real transcript and mock only the native runtime boundary."""
    state = _NativeSession(running=running)
    response = page.request.get(f"{base_url}/v1/sessions/{session_id}")
    assert response.ok
    snapshot = response.json()
    snapshot.update(
        harness="codex-native",
        labels={
            **snapshot.get("labels", {}),
            "omnigent.ui": "terminal",
            "omnigent.wrapper": "codex-native-ui",
        },
        host_id="host_model_picker_resume",
        host_online=True,
        created_at=1_700_000_000,
        llm_model="gpt-5.5",
        reasoning_effort="medium",
        model_options=[
            {
                "id": model,
                "model": model,
                "displayName": label,
                "isDefault": model == "gpt-5.5",
                "defaultReasoningEffort": "medium",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": effort, "description": effort}
                    for effort in ("low", "medium", "high")
                ],
            }
            for model, label in (("gpt-5.5", "GPT-5.5"), ("gpt-5.6-luna", "GPT-5.6-Luna"))
        ],
    )

    def session_route(route: Route) -> None:
        path = urlparse(route.request.url).path
        method = route.request.method
        if path == "/v1/sessions" and method == "GET":
            # The open session derives host binding from its own snapshot.
            route.fulfill(json={"object": "list", "data": [], "has_more": False})
        elif path == f"/v1/sessions/{session_id}" and method in {"GET", "PATCH"}:
            if method == "PATCH":
                body = route.request.post_data_json
                state.mutations.append(("patch", body))
                snapshot.update(body)
            route.fulfill(json={**snapshot, "runner_online": state.running})
        elif path == f"/v1/sessions/{session_id}/resources/terminals" and method == "GET":
            route.fulfill(
                json={
                    "object": "list",
                    "data": [
                        {
                            "id": "terminal_codex_main",
                            "object": "terminal",
                            "name": "codex",
                            "metadata": {
                                "terminal_name": "codex",
                                "session_key": "main",
                                "running": True,
                            },
                        }
                    ]
                    if state.running
                    else [],
                    "has_more": False,
                }
            )
        elif path == f"/v1/sessions/{session_id}/events" and method == "POST":
            body = route.request.post_data_json
            state.mutations.append(("event", body))
            if body.get("type") == "retry_session":
                state.pending_retries.append(route)
            else:
                route.fulfill(status=400, json={"error": "Unexpected session input"})
        else:
            route.continue_()

    def response_route(route: Route) -> None:
        state.mutations.append(("response", route.request.post_data_json))
        route.fulfill(status=400, json={"error": "Unexpected chat response"})

    page.route(re.compile(r"/v1/sessions(?:/|\?|$)"), session_route)
    page.route("**/v1/responses", response_route)
    page.route(
        re.compile(r"/health(?:\?|$)"),
        lambda route: route.fulfill(
            json={"sessions": {session_id: {"runner_online": state.running, "host_online": True}}}
        ),
    )
    page.route_web_socket("**/v1/sessions/updates*", lambda ws: None)
    # Keep the stream open without the seeded SDK runner's unrelated updates.
    page.add_init_script(
        """
        (() => {
          const sessionId = __SESSION_ID__;
          const originalFetch = window.fetch.bind(window);
          window.fetch = (input, init) => {
            const url = typeof input === "string" ? input : input.url;
            if (new URL(url, location.origin).pathname === `/v1/sessions/${sessionId}/stream`) {
              return Promise.resolve(new Response(new ReadableStream(), {
                headers: { "content-type": "text/event-stream" },
              }));
            }
            return originalFetch(input, init);
          };
        })();
        """.replace("__SESSION_ID__", json.dumps(session_id))
    )
    return state


def test_model_picker_resumes_before_editing_without_sending_a_message(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A gear click starts one terminal and preserves the user's draft/view."""
    base_url, session_id = seeded_session
    seed_committed_turn(session_id, prompt="An earlier prompt.", reply="An earlier answer.")
    state = _mock_native_session(page, base_url, session_id)
    with page.expect_response(lambda response: urlparse(response.url).path == "/health"):
        page.goto(f"{base_url}/c/{session_id}?view=chat")

    gear = page.get_by_test_id("composer-config-gear")
    expect(gear).to_be_visible(timeout=15_000)
    expect(page.get_by_test_id("view-mode-chat")).to_have_attribute("aria-pressed", "true")
    composer = page.get_by_role("textbox", name="Message the agent")
    composer.fill("Keep this unsent draft.")
    assert state.mutations == []

    with page.expect_request(
        lambda request: (
            request.method == "POST"
            and urlparse(request.url).path == f"/v1/sessions/{session_id}/events"
        )
    ):
        gear.click()

    expect(page.get_by_text("Starting terminal…", exact=True)).to_be_visible()
    model_trigger = page.get_by_test_id("composer-agent-edit")
    expect(model_trigger).to_be_disabled()
    expect(page.get_by_test_id("composer-agent-effort-select")).to_be_disabled()
    expect(page.get_by_test_id("view-mode-chat")).to_have_attribute("aria-pressed", "true")
    expect(composer).to_have_value("Keep this unsent draft.")
    assert state.mutations == [("event", {"type": "retry_session", "data": {}})]

    # Reopening the picker while startup is pending must reuse that launch.
    page.keyboard.press("Escape")
    expect(page.get_by_test_id("composer-agent-menu")).to_have_count(0)
    gear.click()
    expect(model_trigger).to_be_disabled()
    assert len(state.pending_retries) == 1

    state.finish_resume()
    expect(model_trigger).to_be_enabled()
    expect(page.get_by_text("Starting terminal…", exact=True)).to_have_count(0)
    model_trigger.click()
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and urlparse(response.url).path == f"/v1/sessions/{session_id}"
        )
    ):
        page.locator('[role="menuitemcheckbox"][data-model-id="gpt-5.6-luna"]').click()

    assert state.mutations == [
        ("event", {"type": "retry_session", "data": {}}),
        ("patch", {"model_override": "gpt-5.6-luna"}),
    ]
    expect(page.get_by_test_id("view-mode-chat")).to_have_attribute("aria-pressed", "true")
    expect(composer).to_have_value("Keep this unsent draft.")
    expect(page.locator('[data-testid="message-bubble"][data-role="user"]')).to_have_count(1)
    expect(page.get_by_text("An earlier answer.", exact=True)).to_be_visible()


def test_model_picker_does_not_resume_a_running_terminal(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """An existing native terminal keeps the picker immediately editable."""
    base_url, session_id = seeded_session
    state = _mock_native_session(page, base_url, session_id, running=True)
    with page.expect_response(
        lambda response: (
            urlparse(response.url).path == f"/v1/sessions/{session_id}/resources/terminals"
        )
    ):
        page.goto(f"{base_url}/c/{session_id}?view=chat")

    terminal_toggle = page.get_by_test_id("view-mode-terminal")
    expect(terminal_toggle).to_have_attribute("aria-label", "Terminal view", timeout=15_000)
    page.get_by_test_id("composer-config-gear").click()
    model_trigger = page.get_by_test_id("composer-agent-edit")
    expect(model_trigger).to_be_enabled()
    model_trigger.click()
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and urlparse(response.url).path == f"/v1/sessions/{session_id}"
        )
    ):
        page.locator('[role="menuitemcheckbox"][data-model-id="gpt-5.6-luna"]').click()

    assert state.mutations == [("patch", {"model_override": "gpt-5.6-luna"})]
    expect(page.get_by_test_id("view-mode-chat")).to_have_attribute("aria-pressed", "true")
