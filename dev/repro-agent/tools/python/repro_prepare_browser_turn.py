"""Bundled Omnigent session operation."""

from __future__ import annotations

import sys
from pathlib import Path

from omnigent_client.tools import tool

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from session_driver.sessions import SessionDriver


@tool
def repro_prepare_browser_turn(
    environment: str, session_id: str, prompt: str, scripted_reply: str
) -> dict:
    """Capture evidence baseline before sending the exact prompt in the browser.

    After this call, drive the actual UI actions and call repro_wait_for_turn.
    This call does not send input and does not prove browser interaction.
    Args:
        environment: Environment descriptor path.
        session_id: Session created by repro_start_session.
        prompt: Exact message you will submit through the UI.
        scripted_reply: Model response to serve; must be absent from prompt.
    """
    with SessionDriver(Path(environment)) as driver:
        return driver.begin(session_id, prompt, scripted_reply)
