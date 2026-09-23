"""Failed turns must log a usable, non-prose reason.

Every server-originated ``failed`` turn funnels through one ERROR line in
``_publish_status``::

    session turn failed for <id> (origin=... code=... prev=...): <detail>

In production ``<detail>`` degraded into three undebuggable shapes, each
counted against the mid-session error KPI and none triageable by a human:

1. the literal ``no detail`` -- the ``failed`` edge carried no error at all;
2. ``turn setup failed:`` with an empty reason -- the runner built the message
   from an exception whose ``str()`` was empty and the relay republished it;
3. the assistant's *own successful* final message, verbatim, as the "error" --
   a turn that produced output was labelled failed and the reason was
   backfilled from that output.

Each test drives the real wire path that reaches ``_publish_status`` -- the
native-forwarder ``external_session_status`` POST (shapes 1 and 3) and the
runner stream relay (shape 2) -- and captures the ERROR record from the
``omnigent.server.routes.sessions`` logger. The assertions encode the desired
behaviour (a non-empty, non-prose, diagnosable reason), so on the current build
they FAIL, reproducing the bug; a fix that always populates a usable detail
turns them green.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import pytest

from omnigent.server.routes import sessions as sessions_module
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests.server.helpers import create_test_agent
from tests.server.routes.test_sessions_runner_relay import (
    _TASK_TIMEOUT_S,
    _ScriptedRunnerClient,
)

pytestmark = pytest.mark.asyncio

_SESSIONS_LOGGER = "omnigent.server.routes.sessions"
_FAILED_PREFIX = "session turn failed for "


def _failed_turn_details(caplog: pytest.LogCaptureFixture, session_id: str) -> list[str]:
    """Return the ``<detail>`` from every ``session turn failed`` ERROR row.

    The line is ``session turn failed for <id> (origin=... code=... prev=...):
    <detail>``, so the detail is everything after the last ``): ``.
    """
    marker = f"{_FAILED_PREFIX}{session_id}"
    details: list[str] = []
    for record in caplog.records:
        if record.name != _SESSIONS_LOGGER or record.levelno != logging.ERROR:
            continue
        message = record.getMessage()
        if message.startswith(marker) and "): " in message:
            details.append(message.split("): ", 1)[1])
    return details


async def _create_top_level_session(client: httpx.AsyncClient) -> str:
    """Create a plain top-level session and return its id."""
    agent = await create_test_agent(client, name="failed-turn-detail")
    resp = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _post_status(
    client: httpx.AsyncClient,
    session_id: str,
    status: str,
    *,
    response_id: str,
) -> None:
    """POST an ``external_session_status`` edge (the native-forwarder wire)."""
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_session_status",
            "data": {"status": status, "response_id": response_id},
        },
    )
    assert resp.status_code == 202, resp.text


async def test_failed_status_with_no_reason_logs_no_detail(
    client: httpx.AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Shape 1: a ``failed`` edge carrying no error must log a usable reason."""
    session_id = await _create_top_level_session(client)

    with caplog.at_level(logging.ERROR, logger=_SESSIONS_LOGGER):
        await _post_status(client, session_id, "running", response_id="turn_no_detail")
        await _post_status(client, session_id, "failed", response_id="turn_no_detail")

    details = _failed_turn_details(caplog, session_id)
    assert details, "expected a 'session turn failed' ERROR row for the failed turn"
    detail = details[-1]
    assert detail not in ("", "no detail"), (
        "the failed turn was logged with an undebuggable reason "
        f"({detail!r}); a failed turn must carry a usable, non-empty detail"
    )


async def test_failed_status_reuses_the_assistants_own_output_as_error(
    client: httpx.AsyncClient,
    db_uri: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Shape 3: a turn that produced output must not be failed with its own prose."""
    session_id = await _create_top_level_session(client)
    success_prose = "All conflicts resolved. Continue the sync:"

    with caplog.at_level(logging.ERROR, logger=_SESSIONS_LOGGER):
        await _post_status(client, session_id, "running", response_id="turn_output_as_error")
        seed = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "external_assistant_message",
                "data": {
                    "agent": "assistant",
                    "text": success_prose,
                    "response_id": "turn_output_as_error",
                },
            },
        )
        assert seed.status_code == 202, seed.text
        await _post_status(client, session_id, "failed", response_id="turn_output_as_error")

    store = SqlAlchemyConversationStore(db_uri)
    messages = [item for item in store.list_items(session_id).data if item.type == "message"]
    assert any(success_prose in str(getattr(m.data, "content", "")) for m in messages), (
        "the successful assistant message should be persisted for this session"
    )

    details = _failed_turn_details(caplog, session_id)
    assert details, "expected a 'session turn failed' ERROR row for the failed turn"
    detail = details[-1]
    assert detail != success_prose, (
        "the failed turn's logged detail is the assistant's own successful "
        f"message ({detail!r}); a successful turn's output must not be "
        "published/logged as the failure reason"
    )


async def test_relay_failed_status_drops_the_setup_failure_reason(
    db_uri: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Shape 2: a relayed ``turn setup failed:`` must carry a non-empty reason."""
    from omnigent.runtime import session_stream

    sessions_module._runner_relay_tasks.clear()
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation()
    session_id = conv.id
    sessions_module._session_status_cache[session_id] = "running"

    release = asyncio.Event()
    # The runner's empty-reason setup failure, verbatim off the wire: the runner
    # builds f"turn setup failed: {exc}" from an exception whose str() is empty.
    events: list[dict[str, Any]] = [
        {
            "type": "session.status",
            "status": "failed",
            "error": {"code": "runner_error", "message": "turn setup failed: "},
        },
    ]
    fake_runner = _ScriptedRunnerClient(release, events)

    try:
        with caplog.at_level(logging.ERROR, logger=_SESSIONS_LOGGER):
            handle = await sessions_module._ensure_runner_relay_ready(
                session_id,
                "runner_relay_setup_failed",
                fake_runner,  # type: ignore[arg-type]
                conversation_store=store,
            )
            assert handle is not None
            release.set()
            await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)

        details = _failed_turn_details(caplog, session_id)
        assert details, "expected a 'session turn failed' ERROR row from the relay"
        detail = details[-1]
        reason = detail.split("turn setup failed:", 1)[-1].strip() if ":" in detail else detail
        assert reason, (
            "the failed turn's logged detail dropped the actual cause "
            f"({detail!r}); 'turn setup failed:' must carry a non-empty reason"
        )
    finally:
        release.set()
        handle = sessions_module._runner_relay_tasks.get(session_id)
        if handle is not None:
            await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)
        sessions_module._runner_relay_tasks.clear()
        sessions_module._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)
