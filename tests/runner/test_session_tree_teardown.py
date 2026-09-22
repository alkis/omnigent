"""Explicit deletion must reach every local producer without a server round trip."""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import httpx
import pytest

from omnigent.runner import app as runner_app
from omnigent.runner.native import orchestration
from omnigent.runner.tool_dispatch import _teardown_failed_child
from tests.runner.helpers import NullServerClient


@pytest.mark.parametrize("failed_spawn", [False, True])
async def test_teardown_cancels_descendant_forwarders_without_server_cleanup(
    failed_spawn: bool,
) -> None:
    """A successful server DELETE may have skipped the disconnected runner."""
    root, child, grandchild, unrelated = [uuid.uuid4().hex for _ in range(4)]
    stopped: set[str] = set()
    remote_deletes: list[str] = []

    async def forward(session_id: str) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            stopped.add(session_id)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            # The local producers must already be stopped before remote deletion.
            assert stopped == {root, child, grandchild}
            remote_deletes.append(request.url.path)
            return httpx.Response(200, json={"deleted": True})
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as server_client:
        app = runner_app.create_runner_app(server_client=server_client)
        for parent_id, child_id in [(root, child), (child, grandchild)]:
            runner_app.register_child_session(
                child_id,
                parent_session_id=parent_id,
                title="worker",
                tool="worker",
                session_name="task",
            )
            runner_app.register_subagent_work(
                parent_session_id=parent_id,
                child_session_id=child_id,
                agent="worker",
                title="task",
            )
        tasks = {
            sid: asyncio.create_task(forward(sid)) for sid in (root, child, grandchild, unrelated)
        }
        orchestration._AUTO_FORWARDER_TASKS.update(tasks)
        await asyncio.sleep(0)
        try:
            if failed_spawn:
                assert (
                    await _teardown_failed_child(server_client, root, created_child=True) is None
                )
                assert remote_deletes == [f"/v1/sessions/{root}"]
            else:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://runner"
                ) as client:
                    response = await client.delete(f"/v1/sessions/{root}")
                assert response.status_code == 200
            assert stopped == {root, child, grandchild}
            assert not tasks[unrelated].done()
            for sid in (root, child, grandchild):
                assert sid not in orchestration._AUTO_FORWARDER_TASKS
                assert sid not in runner_app._child_session_parents
                assert runner_app.get_subagent_work(sid) is None
        finally:
            for sid, task in tasks.items():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                orchestration._AUTO_FORWARDER_TASKS.pop(sid, None)
                runner_app.unregister_child_session(sid)
                runner_app.unregister_subagent_work_for_session(sid)


async def test_cancelled_delete_caller_still_reaps_the_whole_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reverse-tunnel disconnect must not cancel cleanup halfway through a tree."""
    root, child = uuid.uuid4().hex, uuid.uuid4().hex
    started: set[str] = set()
    all_started = asyncio.Event()
    release = asyncio.Event()

    async def cleanup_bridge(*, server_client: object, session_id: str) -> None:
        started.add(session_id)
        if started == {root, child}:
            all_started.set()
        await release.wait()

    monkeypatch.setattr(runner_app, "_delete_native_bridge_dirs", cleanup_bridge)
    app = runner_app.create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    runner_app.register_child_session(
        child, parent_session_id=root, title="worker", tool="worker", session_name="task"
    )
    runner_app._session_agent_ids_ref.update({root: "agent", child: "agent"})
    request = asyncio.create_task(app.state.teardown_session(root))
    try:
        await asyncio.wait_for(all_started.wait(), timeout=5)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        release.set()

        async def reaped() -> None:
            while any(sid in runner_app._session_agent_ids_ref for sid in (root, child)):
                await asyncio.sleep(0)

        await asyncio.wait_for(reaped(), timeout=5)
        assert child not in runner_app._child_session_parents
    finally:
        release.set()
        await app.state.teardown_session(root)
        await app.state.teardown_session(child)


async def test_slow_forwarder_remains_tracked_for_cleanup_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hitting the cancellation deadline must not lose the only producer handle."""
    session_id = uuid.uuid4().hex
    release = asyncio.Event()

    async def slow_forwarder() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    task = asyncio.create_task(slow_forwarder())
    orchestration._AUTO_FORWARDER_TASKS[session_id] = task
    monkeypatch.setattr(orchestration, "_AUTO_FORWARDER_CANCEL_TIMEOUT_S", 0.001)
    await asyncio.sleep(0)
    try:
        await orchestration._cancel_auto_forwarder_task(session_id)
        assert orchestration._AUTO_FORWARDER_TASKS.get(session_id) is task
        release.set()
        await task
        await orchestration._cancel_auto_forwarder_task(session_id)
        assert session_id not in orchestration._AUTO_FORWARDER_TASKS
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        orchestration._AUTO_FORWARDER_TASKS.pop(session_id, None)
