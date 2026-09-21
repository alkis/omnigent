"""Bundled Omnigent session operation."""

from __future__ import annotations

import sys
from pathlib import Path

from omnigent_client.tools import tool

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from session_driver.sessions import SessionDriver


@tool
def repro_close_session(environment: str, session_id: str) -> dict:
    """Delete a session owned by this environment, retaining its evidence."""
    with SessionDriver(Path(environment)) as driver:
        return driver.close(session_id)
