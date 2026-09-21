"""Bundled Omnigent session operation."""

from __future__ import annotations

import sys
from pathlib import Path

from omnigent_client.tools import tool

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from session_driver.sessions import SessionDriver


@tool
def repro_inspect_session(environment: str, session_id: str) -> dict:
    """Retrieve this session's snapshot, transcript, journey receipts and log directory."""
    with SessionDriver(Path(environment)) as driver:
        return driver.inspect(session_id)
