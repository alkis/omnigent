"""Parent-owned child streams honor explicit teardown and retain unsent history."""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx

from omnigent.harnesses.claude_native import forwarder as claude
from omnigent.harnesses.codex_native import forwarder as codex
from omnigent.native.session_lifecycle import allow_session_forwarding, is_session_deleted
from omnigent.runner import create_runner_app


async def test_deleted_native_child_stops_posts_without_stopping_parent(tmp_path: Path) -> None:
    parent, child = uuid.uuid4().hex, uuid.uuid4().hex
    posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(request.url.path)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as server_client:
        app = create_runner_app(server_client=server_client)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://runner"
        ) as runner:
            assert (await runner.delete(f"/v1/sessions/{child}")).status_code == 200
        try:
            state = codex._CodexForwarderState(parent_session_id=parent)
            state.note_child_thread("thread_child", child)
            for session_id, thread in ((child, "thread_child"), (parent, "thread_parent")):
                await codex._handle_event(
                    server_client,
                    session_id=parent,
                    bridge_dir=tmp_path,
                    event={
                        "method": "turn/started",
                        "params": {"threadId": thread, "turn": {"id": "turn"}},
                    },
                    usage_coalescer=codex._SessionUsageCoalescer(server_client, session_id),
                    elicitation_tracker=codex._CodexElicitationTaskTracker(),
                    expected_thread_id="thread_parent",
                    forwarder_state=state,
                )
            await codex._post_collab_agent_statuses(
                server_client,
                item={"agentsStates": {"thread_child": {"status": "running"}}},
                forwarder_state=state,
            )
            # A previously queued event cannot restart an unbounded retry loop.
            result = await codex._post_session_event_inner(
                server_client,
                child,
                event_type="external_conversation_item",
                data={"source_id": "pending", "item_type": "message", "item_data": {}},
                max_attempts=None,
            )
            assert result.response is None
            assert posts and all(path == f"/v1/sessions/{parent}/events" for path in posts)

            entry = claude.SubagentEntry(
                subagent_id="worker",
                child_conversation_id=child,
                byte_offset=37,
                seen_source_ids=("already-sent",),
                last_activity_ts=1,
            )
            checkpoint = claude._SubagentStateCheckpoint(
                tmp_path, claude.SubagentForwardState(subagents={"worker": entry})
            )
            await claude._forward_one_subagent(
                client=server_client,
                parent_session_id=parent,
                bridge_dir=tmp_path,
                subagents_dir=tmp_path,
                entry=entry,
                agent_name="worker",
                checkpoint=checkpoint,
                item_retry_tracker=claude._PostRetryTracker(),
                status_retry_tracker=claude._PostRetryTracker(),
                batch_capability=claude._SessionEventBatchCapability(),
                status_capability=claude._SubagentStatusCapability(),
            )
            persisted = claude._read_subagent_forward_state(tmp_path).subagents["worker"]
            assert persisted.deleted
            assert persisted.byte_offset == 37
            assert persisted.seen_source_ids == ("already-sent",)
        finally:
            allow_session_forwarding(child)


async def test_transient_404_does_not_fence_native_forwarding() -> None:
    session_id = uuid.uuid4().hex
    responses = iter((404, 200))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(next(responses))),
        base_url="http://server",
    ) as client:
        first = await codex._post_session_event_inner(
            client, session_id, event_type="external_session_status", data={"status": "running"}
        )
        assert first.response is not None and first.response.status_code == 404
        assert not is_session_deleted(session_id)
        recovered = await codex._post_session_event_inner(
            client, session_id, event_type="external_session_status", data={"status": "running"}
        )
        assert recovered.response is not None and recovered.response.status_code == 200
