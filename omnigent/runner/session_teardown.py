"""Reach the local runner's teardown without depending on its server tunnel."""

from __future__ import annotations

from weakref import ReferenceType, WeakKeyDictionary, ref

import httpx
from fastapi import FastAPI

_apps: WeakKeyDictionary[httpx.AsyncClient, ReferenceType[FastAPI]] = WeakKeyDictionary()


def register_local_teardown(client: httpx.AsyncClient, app: FastAPI) -> None:
    """Associate a runner's server client with its local session lifecycle."""
    _apps[client] = ref(app)


async def teardown_local_session(client: httpx.AsyncClient, session_id: str) -> None:
    """Delete locally when dispatch runs inside a runner; standalone clients skip."""
    app_ref = _apps.get(client)
    app = app_ref() if app_ref is not None else None
    if app is not None:
        await app.state.teardown_session(session_id)
