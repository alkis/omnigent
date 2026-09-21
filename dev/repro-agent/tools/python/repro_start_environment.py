"""Bundled Omnigent session operation."""

from __future__ import annotations

import sys
from pathlib import Path

from omnigent_client.tools import tool

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from session_driver.environment import start_environment


@tool
def repro_start_environment(
    checkout: str, output: str, model_backend: str = "mock", lease_seconds: int = 1800
) -> dict:
    """Start an isolated local server, runner and mock model; return its environment path.

    Args:
        checkout: Absolute product checkout path with an already built SPA.
        output: Absolute directory for retained logs and session evidence.
        model_backend: Must be mock. Does not validate live model/provider behavior.
        lease_seconds: Automatic cleanup deadline, 60..3600 seconds.
    """
    return start_environment(checkout, output, model_backend, lease_seconds)
