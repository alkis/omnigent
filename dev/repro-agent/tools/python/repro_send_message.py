"""Bundled Omnigent session operation."""

from __future__ import annotations

import sys
from pathlib import Path

from omnigent_client.tools import tool

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from session_driver.sessions import SessionDriver


@tool
def repro_send_message(
    environment: str, session_id: str, prompt: str, scripted_reply: str
) -> dict:
    """Send a message through the production API and return a pending turn ID.

    Use for backend journeys; this bypasses the browser composer. Never retry
    a failed send blindly: inspect first because delivery may have succeeded.
    Args:
        environment: Environment descriptor path.
        session_id: Session created by repro_start_session.
        prompt: User input to send.
        scripted_reply: Model response to serve; must be absent from prompt.
    """
    with SessionDriver(Path(environment)) as driver:
        return driver.send(session_id, prompt, scripted_reply)
