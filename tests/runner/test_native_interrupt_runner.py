"""Unit tests for ``NativeInterruptRunner`` (PR 1.6 interrupt/stop seam).

These drive the runner class directly with lightweight fakes, complementing the
HTTP-path tests in ``test_app_sessions_native_events_lifecycle.py`` /
``test_app_sessions_native_supervision.py`` (which POST to ``/events`` and patch
the bridge-module control functions). The focus here is the registry dispatch
and the descriptor-collapsed uniform handlers: which harnesses route where, the
no-handler fall-through contract (antigravity/opencode), and the 503 mapping.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.responses import Response

from omnigent.runner.native.interrupt import NativeInterruptRunner


@dataclass
class _FakeAck:
    """Stand-in for ``_SubagentDeliveryAck``."""

    delivered: bool = True
    entry: object | None = None
    reason: str = "delivered"


class _FakeTerminalRegistry:
    def __init__(self) -> None:
        self.closed: list[str] = []

    def list_for_conversation(self, conv_id: str) -> list[Any]:
        return []


class _FakeResourceRegistry:
    def __init__(self) -> None:
        self.terminal_registry = _FakeTerminalRegistry()

    async def close_terminal(self, conv_id: str, terminal_id: str) -> bool:
        return True


def _make_runner(**overrides: Any) -> tuple[NativeInterruptRunner, dict[str, Any]]:
    """Build a runner with recording fakes; return it plus a capture dict."""
    captured: dict[str, Any] = {"published": [], "wakes": []}

    def _publish(conv_id: str, event: dict[str, Any]) -> None:
        captured["published"].append((conv_id, event))

    def _mark_and_wake(child_session_id: str, *, status: str, output: str | None) -> _FakeAck:
        captured["wakes"].append((child_session_id, status, output))
        return _FakeAck()

    async def _codex_bridge_state(conv_id: str, *, action: str, **_kw: Any) -> Any | None:
        return None

    def _client_safe(exc: BaseException, *, context: str) -> str:
        return f"safe:{context}"

    kwargs: dict[str, Any] = {
        "server_client": SimpleNamespace(),
        "resource_registry": _FakeResourceRegistry(),
        "publish_event": _publish,
        "mark_subagent_terminal_and_wake": _mark_and_wake,
        "session_sub_agent_names": {},
        "codex_bridge_state_for_session": _codex_bridge_state,
        "client_safe_error_detail": _client_safe,
        "logger": logging.getLogger("test.interrupt"),
    }
    kwargs.update(overrides)
    return NativeInterruptRunner(**kwargs), captured


def test_native_cancel_capability_follows_stop_registry() -> None:
    """Parent cancel capability must track ``_UNIFORM_STOP`` plus Claude."""
    from omnigent.native.native_coding_agents import NATIVE_CODING_AGENTS
    from omnigent.runner.native.interrupt import (
        _UNIFORM_STOP,
        native_cancel_capability,
    )

    for agent in NATIVE_CODING_AGENTS:
        capability = native_cancel_capability(agent.wrapper_label)
        if agent.key == "claude" or agent.key in _UNIFORM_STOP:
            assert capability == "stop", agent.key
        else:
            assert capability == "best_effort", agent.key
        if agent.subagent_wrapper_label:
            assert native_cancel_capability(agent.subagent_wrapper_label) == capability

    assert native_cancel_capability(None) == "inprocess"
    assert native_cancel_capability("not-a-native-wrapper") == "inprocess"


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["antigravity-native", "opencode-native", "claude-sdk", None])
async def test_no_handler_harnesses_return_none(harness: str | None) -> None:
    """Harnesses without an interrupt/stop handler return None (caller falls through)."""
    runner, _ = _make_runner()
    assert await runner.interrupt(harness, "conv_x") is None
    assert await runner.stop(harness, "conv_x") is None


@pytest.mark.asyncio
async def test_uniform_interrupt_injects_and_wakes_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A uniform interrupt calls the bridge inject fn and wakes the parent."""
    import omnigent.harnesses.goose_native.bridge as goose_bridge

    calls: list[Any] = []

    def _inject(bridge_dir: Any, *, timeout_s: float) -> None:
        calls.append((bridge_dir, timeout_s))

    monkeypatch.setattr(goose_bridge, "bridge_dir_for_session_id", lambda conv: f"dir/{conv}")
    monkeypatch.setattr(goose_bridge, "inject_interrupt", _inject)

    runner, captured = _make_runner()
    resp = await runner.interrupt("goose-native", "conv_g")

    assert isinstance(resp, Response) and resp.status_code == 204
    assert calls == [("dir/conv_g", 1.0)]
    assert captured["wakes"] == [("conv_g", "cancelled", "[System: sub-agent interrupted]")]


@pytest.mark.asyncio
async def test_pi_interrupt_uses_enqueue_without_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pi's uniform interrupt uses enqueue_interrupt with no timeout kwarg."""
    import omnigent.harnesses.pi_native.bridge as pi_bridge

    calls: list[Any] = []
    monkeypatch.setattr(pi_bridge, "bridge_dir_for_session_id", lambda conv: f"dir/{conv}")
    monkeypatch.setattr(
        pi_bridge, "enqueue_interrupt", lambda bridge_dir: calls.append(bridge_dir)
    )

    runner, _ = _make_runner()
    resp = await runner.interrupt("pi-native", "conv_p")

    assert isinstance(resp, Response) and resp.status_code == 204
    assert calls == ["dir/conv_p"]


@pytest.mark.asyncio
async def test_uniform_interrupt_bridge_error_returns_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A RuntimeError from the bridge inject maps to a 503 with the error code."""
    import json

    import omnigent.harnesses.qwen_native.bridge as qwen_bridge

    def _boom(bridge_dir: Any, *, timeout_s: float) -> None:
        raise RuntimeError("tmux target is not advertised")

    monkeypatch.setattr(qwen_bridge, "bridge_dir_for_session_id", lambda conv: "d")
    monkeypatch.setattr(qwen_bridge, "inject_interrupt", _boom)

    runner, captured = _make_runner()
    resp = await runner.interrupt("qwen-native", "conv_q")

    assert resp is not None and resp.status_code == 503
    body = json.loads(bytes(resp.body))
    assert body["error"] == "qwen_native_interrupt_failed"
    assert body["detail"] == "safe:qwen-native interrupt"
    # No parent wake on failure.
    assert captured["wakes"] == []


@pytest.mark.asyncio
async def test_uniform_stop_kills_tears_down_and_goes_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A uniform stop kills the bridge, publishes idle, and wakes the parent."""
    import omnigent.harnesses.cursor_native.bridge as cursor_bridge

    killed: list[Any] = []
    monkeypatch.setattr(cursor_bridge, "bridge_dir_for_session_id", lambda conv: f"dir/{conv}")
    monkeypatch.setattr(
        cursor_bridge,
        "kill_session",
        lambda bridge_dir, *, timeout_s: killed.append((bridge_dir, timeout_s)),
    )

    runner, captured = _make_runner()
    resp = await runner.stop("cursor-native", "conv_c")

    assert isinstance(resp, Response) and resp.status_code == 204
    assert killed == [("dir/conv_c", 1.0)]
    idle = [e for _, e in captured["published"] if e.get("status") == "idle"]
    assert idle == [{"type": "session.status", "status": "idle"}]
    assert captured["wakes"] == [("conv_c", "cancelled", "[System: sub-agent stopped]")]


@pytest.mark.asyncio
async def test_uniform_stop_kill_failure_returns_503_without_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed kill returns 503 and does NOT publish idle (no lie to the UI)."""
    import json

    import omnigent.harnesses.hermes_native.bridge as hermes_bridge

    def _boom(bridge_dir: Any, *, timeout_s: float) -> None:
        raise RuntimeError("tmux target is not advertised")

    monkeypatch.setattr(hermes_bridge, "bridge_dir_for_session_id", lambda conv: "d")
    monkeypatch.setattr(hermes_bridge, "kill_session", _boom)

    runner, captured = _make_runner()
    resp = await runner.stop("hermes-native", "conv_h")

    assert resp is not None and resp.status_code == 503
    assert json.loads(bytes(resp.body))["error"] == "hermes_native_stop_failed"
    assert [e for _, e in captured["published"] if e.get("status") == "idle"] == []


@pytest.mark.asyncio
async def test_codex_and_pi_stop_route_to_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex/pi have no distinct stop — stop() routes to their interrupt handler."""
    import omnigent.harnesses.pi_native.bridge as pi_bridge

    calls: list[str] = []
    monkeypatch.setattr(pi_bridge, "bridge_dir_for_session_id", lambda conv: conv)
    monkeypatch.setattr(
        pi_bridge, "enqueue_interrupt", lambda bridge_dir: calls.append(bridge_dir)
    )

    runner, _ = _make_runner()
    resp = await runner.stop("pi-native", "conv_p")

    assert isinstance(resp, Response) and resp.status_code == 204
    # The interrupt path ran (enqueue_interrupt), not a kill_session.
    assert calls == ["conv_p"]


@pytest.mark.asyncio
async def test_codex_interrupt_noop_when_no_bridge_state() -> None:
    """codex interrupt returns 204 when there is no live bridge state."""
    runner, _ = _make_runner()  # default codex_bridge_state returns None
    resp = await runner.interrupt("codex-native", "conv_cx")
    assert isinstance(resp, Response) and resp.status_code == 204


@pytest.mark.asyncio
async def test_claude_stop_is_idempotent_without_advertised_tmux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already-absent Claude pane still completes stop teardown."""
    import omnigent.harnesses.claude_native.bridge as claude_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    async def _fake_bridge_id(*, server_client: Any, session_id: str) -> str:
        del server_client, session_id
        return "bridge123"

    def _absent(bridge_dir: Any, *, timeout_s: float) -> None:
        del bridge_dir, timeout_s
        raise claude_bridge.TmuxSessionNotAdvertised("not advertised")

    monkeypatch.setattr(interrupt_mod, "_claude_native_bridge_id_for_session", _fake_bridge_id)
    monkeypatch.setattr(claude_bridge, "bridge_dir_for_bridge_id", lambda bridge_id: bridge_id)
    monkeypatch.setattr(claude_bridge, "kill_session", _absent)

    runner, captured = _make_runner()
    resp = await runner.stop("claude-native", "conv_cn")

    assert isinstance(resp, Response) and resp.status_code == 204
    assert captured["wakes"] == [("conv_cn", "cancelled", "[System: sub-agent stopped]")]


@pytest.mark.asyncio
async def test_claude_stop_kill_failure_returns_503_without_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Claude kill ``RuntimeError`` is 503; only ``TmuxSessionNotAdvertised`` is 204."""
    import json

    import omnigent.harnesses.claude_native.bridge as claude_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    async def _fake_bridge_id(*, server_client: Any, session_id: str) -> str:
        del server_client, session_id
        return "bridge123"

    def _boom(bridge_dir: Any, *, timeout_s: float) -> None:
        del bridge_dir, timeout_s
        raise RuntimeError("tmux kill-session failed: connection refused")

    monkeypatch.setattr(interrupt_mod, "_claude_native_bridge_id_for_session", _fake_bridge_id)
    monkeypatch.setattr(claude_bridge, "bridge_dir_for_bridge_id", lambda bridge_id: bridge_id)
    monkeypatch.setattr(claude_bridge, "kill_session", _boom)

    runner, captured = _make_runner()
    resp = await runner.stop("claude-native", "conv_cn")

    assert resp is not None and resp.status_code == 503
    assert json.loads(bytes(resp.body))["error"] == "claude_native_stop_failed"
    assert captured["wakes"] == []
    assert [e for _, e in captured["published"] if e.get("status") == "idle"] == []


@pytest.mark.asyncio
async def test_claude_interrupt_resolves_bridge_id_and_injects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """claude interrupt resolves the bridge id, injects, and wakes the parent."""
    import omnigent.harnesses.claude_native.bridge as claude_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    async def _fake_bridge_id(*, server_client: Any, session_id: str) -> str:
        return f"bid-{session_id}"

    injected: list[Any] = []
    monkeypatch.setattr(interrupt_mod, "_claude_native_bridge_id_for_session", _fake_bridge_id)
    monkeypatch.setattr(claude_bridge, "bridge_dir_for_bridge_id", lambda bid: f"dir/{bid}")
    monkeypatch.setattr(
        claude_bridge,
        "inject_interrupt",
        lambda bridge_dir, *, timeout_s: injected.append((bridge_dir, timeout_s)),
    )

    runner, captured = _make_runner()
    resp = await runner.interrupt("claude-native", "conv_cl")

    assert isinstance(resp, Response) and resp.status_code == 204
    assert injected == [("dir/bid-conv_cl", 1.0)]
    assert captured["wakes"] == [("conv_cl", "cancelled", "[System: sub-agent interrupted]")]


# ---------------------------------------------------------------------------
# Codex interrupt: finished-turn handling (capacity-retry sentinel)
# ---------------------------------------------------------------------------


def _make_codex_bridge_state(
    *,
    active_turn_id: str | None,
    socket_path: str = "ws://127.0.0.1:43210",
    thread_id: str = "thread_cx",
) -> Any:
    """Build a minimal fake CodexNativeBridgeState."""
    from omnigent.harnesses.codex_native.bridge import CodexNativeBridgeState

    return CodexNativeBridgeState(
        session_id="conv_cx",
        socket_path=socket_path,
        thread_id=thread_id,
        codex_home="/tmp/codex-home",
        active_turn_id=active_turn_id,
    )


def _patch_codex_modules(
    monkeypatch: pytest.MonkeyPatch,
    *,
    codex_requests: list[Any],
    interrupt_raises: BaseException | None = None,
    cleared: list[str],
) -> None:
    """
    Patch the codex bridge and app-server module attributes used by ``_codex_interrupt``.

    :param codex_requests: Receives ``(method, params)`` pairs.
    :param interrupt_raises: If set, the fake client raises this on ``turn/interrupt``.
    :param cleared: Receives turn ids passed to ``clear_active_turn_id_if_matches``.
    :returns: None.
    """
    from omnigent.harnesses.codex_native import app_server as codex_app_server
    from omnigent.harnesses.codex_native import bridge as codex_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    class _FakeCodexClient:
        async def connect(self) -> None:
            pass

        async def request(self, method: str, params: dict) -> dict:
            codex_requests.append((method, params))
            if method == "turn/interrupt" and interrupt_raises is not None:
                raise interrupt_raises
            return {"result": {}}

        async def close(self) -> None:
            pass

    monkeypatch.setattr(
        codex_app_server, "client_for_transport", lambda *a, **kw: _FakeCodexClient()
    )
    monkeypatch.setattr(codex_bridge, "bridge_dir_for_bridge_id", lambda bid: f"/bridge/{bid}")
    monkeypatch.setattr(codex_bridge, "cancel_pending_mcp_startup", lambda _: [])
    monkeypatch.setattr(
        codex_bridge,
        "clear_active_turn_id_if_matches",
        lambda _dir, turn_id: cleared.append(turn_id) or True,
    )

    # _session_labels_for_runner_spawn is imported at module level in interrupt.py;
    # patch the interrupt module's bound name directly.
    async def _fake_labels(*, server_client: Any, session_id: str) -> dict:
        return {}

    monkeypatch.setattr(interrupt_mod, "_session_labels_for_runner_spawn", _fake_labels)


@pytest.mark.asyncio
async def test_codex_interrupt_finished_turn_returns_204_clears_id_and_publishes_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a) 'no active turn to interrupt' → 204, stale id cleared, idle published.

    The capacity-retry backoff keeps the failed turn id as bridge's active_turn_id.
    When Stop arrives, Codex rejects the interrupt (turn already ended); the runner
    must clear the stale record, publish idle so the session stops "running", and
    return 204.
    """
    import json

    from omnigent.harnesses.codex_native.app_server import CodexAppServerResponseError

    requests: list[Any] = []
    cleared: list[Any] = []

    async def _bridge_state(conv_id: str, *, action: str, **_kw: Any) -> Any:
        return _make_codex_bridge_state(active_turn_id="turn_cx")

    _patch_codex_modules(
        monkeypatch,
        codex_requests=requests,
        interrupt_raises=CodexAppServerResponseError(
            {"code": -32600, "message": "no active turn to interrupt"}
        ),
        cleared=cleared,
    )
    runner, captured = _make_runner(codex_bridge_state_for_session=_bridge_state)

    resp = await runner.interrupt("codex-native", "conv_cx")

    assert resp is not None and resp.status_code == 204, (
        f"expected 204, got {resp.status_code}: "
        f"{json.loads(bytes(resp.body)) if resp.status_code != 204 else ''}"
    )
    # The stale turn id was cleared from bridge state.
    assert "turn_cx" in cleared
    # turn/interrupt was attempted.
    assert any(m == "turn/interrupt" and p.get("turnId") == "turn_cx" for m, p in requests)
    # Idle was published so the session stops "running".
    assert any(e.get("status") == "idle" for _, e in captured["published"])


@pytest.mark.asyncio
async def test_codex_interrupt_other_error_returns_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) Any other CodexAppServerResponseError is still surfaced as 503."""
    import json

    from omnigent.harnesses.codex_native.app_server import CodexAppServerResponseError

    requests: list[Any] = []
    cleared: list[Any] = []

    async def _bridge_state(conv_id: str, *, action: str, **_kw: Any) -> Any:
        return _make_codex_bridge_state(active_turn_id="turn_cx")

    _patch_codex_modules(
        monkeypatch,
        codex_requests=requests,
        interrupt_raises=CodexAppServerResponseError(
            {"code": -32600, "message": "thread not found"}
        ),
        cleared=cleared,
    )
    runner, captured = _make_runner(codex_bridge_state_for_session=_bridge_state)

    resp = await runner.interrupt("codex-native", "conv_cx")

    assert resp is not None and resp.status_code == 503
    assert json.loads(bytes(resp.body))["error"] == "codex_native_interrupt_failed"
    # Nothing was cleared, no idle published.
    assert cleared == []
    assert not any(e.get("status") == "idle" for _, e in captured["published"])


@pytest.mark.asyncio
async def test_codex_interrupt_active_turn_mismatch_returns_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Active-turn mismatch ('expected … but found …') still returns 503.

    A mismatch means a newer turn is live and was not interrupted; reporting
    success would hide a failed Stop. Only 'no active turn to interrupt' gets
    the finished-turn treatment.
    """
    import json

    from omnigent.harnesses.codex_native.app_server import CodexAppServerResponseError

    requests: list[Any] = []
    cleared: list[Any] = []

    async def _bridge_state(conv_id: str, *, action: str, **_kw: Any) -> Any:
        return _make_codex_bridge_state(active_turn_id="turn_cx")

    _patch_codex_modules(
        monkeypatch,
        codex_requests=requests,
        interrupt_raises=CodexAppServerResponseError(
            {
                "code": -32600,
                "message": "expected active turn id `turn_cx` but found `turn_new`",
            }
        ),
        cleared=cleared,
    )
    runner, captured = _make_runner(codex_bridge_state_for_session=_bridge_state)

    resp = await runner.interrupt("codex-native", "conv_cx")

    assert resp is not None and resp.status_code == 503
    assert json.loads(bytes(resp.body))["error"] == "codex_native_interrupt_failed"
    # Nothing cleared; no idle published.
    assert cleared == []
    assert not any(e.get("status") == "idle" for _, e in captured["published"])
