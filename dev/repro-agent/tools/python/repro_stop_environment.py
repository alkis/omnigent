"""Bundled Omnigent session operation."""

from __future__ import annotations

import sys
from pathlib import Path

from omnigent_client.tools import tool

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from session_driver.environment import stop_environment


@tool
def repro_stop_environment(environment: str) -> dict:
    """Stop the owned server, runner, model and their descendants; retain artifacts."""
    return stop_environment(environment)
