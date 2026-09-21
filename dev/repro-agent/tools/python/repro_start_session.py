"""Bundled Omnigent session operation."""

from __future__ import annotations

import sys
from pathlib import Path

from omnigent_client.tools import tool

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from session_driver.sessions import SessionDriver


@tool
def repro_start_session(environment: str, harness: str, journey: str) -> dict:
    """Create a real harness session; return its ID and browser URL, without claiming a turn.

    Args:
        environment: environment.json path returned by repro_start_environment.
        harness: claude-native, codex-native, or openai-agents.
        journey: Ticket and specific user journey being exercised.
    """
    with SessionDriver(Path(environment)) as driver:
        return driver.start(harness, journey)
