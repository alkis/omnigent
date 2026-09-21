"""Bundled Omnigent session operation."""

from __future__ import annotations

import sys
from pathlib import Path

from omnigent_client.tools import tool

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from session_driver.sessions import SessionDriver


@tool
def repro_wait_for_turn(
    environment: str, session_id: str, turn_id: str, timeout: float = 30
) -> dict:
    """Observe fresh persisted user/assistant evidence for this turn, or report pending/failed.

    Completion does not establish the bug verdict or prove UI actions. For UI
    journeys retain browser assertions and footage separately.
    Args:
        environment: Environment descriptor path.
        session_id: Session created by repro_start_session.
        turn_id: ID returned by prepare_browser_turn or send_message.
        timeout: Seconds to poll, 0..60. Repeat waits for pending turns.
    """
    with SessionDriver(Path(environment)) as driver:
        return driver.wait(session_id, turn_id, timeout)
