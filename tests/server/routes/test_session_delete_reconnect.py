"""Deletion intent survives unavailable runners, server restarts and tenant switches."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from omnigent.db.db_models import workspace_scope
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes import sessions
from omnigent.server.runner_session_cleanup import RunnerSessionCleanup
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.runner_session_cleanup_store import RunnerSessionCleanupStore


async def test_delete_offline_tree_replays_after_server_restart(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even idle descendants on another runner get full teardown after row deletion."""
    store = SqlAlchemyConversationStore(db_uri)
    root = store.create_conversation(runner_id="runner_parent")
    child = store.create_conversation(parent_conversation_id=root.id, runner_id="runner_child")
    grandchild = store.create_conversation(
        parent_conversation_id=child.id, runner_id="runner_child"
    )
    monkeypatch.setattr(
        sessions,
        "_get_runner_client_for_resource_access",
        AsyncMock(side_effect=OmnigentError("offline", code=ErrorCode.RUNNER_UNAVAILABLE)),
    )
    response = await client.delete(f"/v1/sessions/{root.id}")
    assert response.status_code == 200
    assert store.get_conversation(root.id) is None
    assert store.get_conversation(child.id) is None
    assert store.get_conversation(grandchild.id) is None

    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        deleted.append(request.url.path)
        return httpx.Response(200, json={"deleted": True})

    # New coordinator/store instances represent a different server process.
    journal = RunnerSessionCleanupStore(db_uri)
    coordinator = RunnerSessionCleanup(journal, app.state.runner_router)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://runner"
    ) as runner:
        monkeypatch.setattr(app.state.runner_router, "client_for_cleanup", lambda _id: runner)
        with workspace_scope(123):
            assert await coordinator.replay("runner_child")
            assert deleted == []
        assert await coordinator.replay("runner_child")
        assert set(deleted) == {f"/v1/sessions/{child.id}", f"/v1/sessions/{grandchild.id}"}
        assert await coordinator.replay("runner_parent")
        assert set(deleted) == {
            f"/v1/sessions/{sid}" for sid in (root.id, child.id, grandchild.id)
        }
        assert journal.pending("runner_child") == []
        assert journal.pending("runner_parent") == []


@pytest.mark.parametrize("status", [404, 500])
async def test_failed_cleanup_is_not_acknowledged(
    app: FastAPI,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    """An HTTP rejection of DELETE is not proof that local producers were stopped."""
    journal = RunnerSessionCleanupStore(db_uri)
    (command,) = journal.enqueue([("runner", "deleted-session")])
    journal.complete([command.command_id])
    cleanup = RunnerSessionCleanup(journal, app.state.runner_router)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status)),
        base_url="http://runner",
    ) as runner:
        monkeypatch.setattr(app.state.runner_router, "client_for_cleanup", lambda _id: runner)
        assert not await cleanup.replay("runner")
    assert len(journal.pending("runner")) == 1


async def test_reconnect_during_delete_cannot_forget_cleanup_intent(
    app: FastAPI,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup can happen early, but its fence lasts until server deletion completes."""
    journal = RunnerSessionCleanupStore(db_uri)
    (command,) = journal.enqueue([("runner", "being-deleted")])
    cleanup = RunnerSessionCleanup(journal, app.state.runner_router)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        base_url="http://runner",
    ) as runner:
        monkeypatch.setattr(app.state.runner_router, "client_for_cleanup", lambda _id: runner)
        assert await cleanup.replay("runner")
        assert await cleanup.pending_sessions("runner") == {"being-deleted"}
        journal.complete([command.command_id])
        assert await cleanup.replay("runner")
        assert journal.pending("runner") == []


def test_retrying_delete_completes_older_commands(db_uri: str) -> None:
    """A retry must not leave the previous attempt's initialization fence behind."""
    journal = RunnerSessionCleanupStore(db_uri)
    journal.enqueue([("runner", "being-deleted")])
    (command,) = journal.enqueue([("runner", "being-deleted")])
    (unrelated,) = journal.enqueue([("runner", "keep")])
    journal.complete([command.command_id])
    for pending in journal.pending("runner"):
        journal.acknowledge(pending.command_id)
    assert journal.pending("runner") == [unrelated]


async def test_rejected_branch_delete_does_not_leave_cleanup_intent(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(
        runner_id="offline-runner",
        host_id="00000000000000000000000000000001",
        workspace="/tmp/worktree",
        git_branch="test-branch",
    )
    monkeypatch.setattr(
        sessions,
        "_remove_session_worktree_best_effort",
        AsyncMock(side_effect=OmnigentError("Host is offline", code=ErrorCode.CONFLICT)),
    )
    response = await client.delete(f"/v1/sessions/{conv.id}?delete_branch=true")
    assert response.status_code == 409
    assert store.get_conversation(conv.id) is not None
    assert app.state.runner_session_cleanup.store.pending("offline-runner") == []
