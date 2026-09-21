"""Resolve routing affinity without granting a child ownership of its host."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omnigent.entities import Conversation
    from omnigent.stores import ConversationStore


def session_routing_hosts(
    conversations: Sequence[Conversation], conversation_store: ConversationStore
) -> dict[str, str | None]:
    """Find each session's host, following parents that share its runner.

    Colocated children deliberately have no ownership ``host_id``. Read their
    ancestors in batches so list projections need at most one read per depth,
    and stop at a changed runner binding, missing ancestor, or cycle.

    :param conversations: Already-loaded sessions to project.
    :param conversation_store: Store scoped to the request's workspace.
    :returns: Session id to routing host id; ``None`` if none can be resolved.
    """
    known: dict[str, Conversation | None] = {conv.id: conv for conv in conversations}
    result = {conv.id: conv.host_id for conv in conversations}
    pending = {
        conv.id: conv
        for conv in conversations
        if conv.host_id is None and conv.runner_id is not None and conv.parent_conversation_id
    }
    visited = {session_id: {session_id} for session_id in pending}
    while pending:
        parent_ids = {
            parent_id
            for conv in pending.values()
            if (parent_id := conv.parent_conversation_id) is not None and parent_id not in known
        }
        if parent_ids:
            found = conversation_store.get_conversations(list(parent_ids))
            known.update({parent_id: found.get(parent_id) for parent_id in parent_ids})
        next_pending = {}
        for session_id, conv in pending.items():
            parent_id = conv.parent_conversation_id
            if parent_id is None or parent_id in visited[session_id]:
                continue
            visited[session_id].add(parent_id)
            parent = known.get(parent_id)
            if parent is None or parent.runner_id != conv.runner_id:
                continue
            if parent.host_id is not None:
                result[session_id] = parent.host_id
            elif parent.parent_conversation_id is not None:
                next_pending[session_id] = parent
        pending = next_pending
    return result
