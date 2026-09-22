"""Local deletion fences shared by native forwarders and runner teardown.

Only an explicit teardown sets these fences. HTTP errors never do.
"""

from __future__ import annotations

_deleted_sessions: set[str] = set()


def mark_session_deleted(session_id: str) -> None:
    """Stop forwarding to a target removed through the runner's delete lifecycle."""
    _deleted_sessions.add(session_id)


def is_session_deleted(session_id: str) -> bool:
    """Check explicit local deletion, including children owned by a parent forwarder."""
    return session_id in _deleted_sessions


def allow_session_forwarding(session_id: str) -> None:
    """Allow an explicitly initialized replacement with the same session id."""
    _deleted_sessions.discard(session_id)
