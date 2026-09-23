"""Opt-in pytest capture for the three prepared-runtime smoke recipes."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
import pytest

from .runtime import write_json

_SESSION_FIXTURES = (
    "native_claude_mock_session",
    "native_codex_mock_session",
    "custom_agent_session",
)


def pytest_addoption(parser):
    parser.addoption("--repro-evidence", type=Path, required=True)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item):
    outcome = yield
    report = outcome.get_result()
    directory = item.config.getoption("--repro-evidence")
    record = {
        "nodeid": item.nodeid,
        "phase": report.when,
        "outcome": report.outcome,
        "captured_at": time.time(),
        "duration_s": report.duration,
        "details": str(report.longrepr) if report.longrepr else "",
        "sections": report.sections,
        "purpose": "connectivity_smoke_only",
    }
    write_json(directory / f"{report.when}.json", record)
    if report.when != "call":
        return
    with httpx.Client(trust_env=False, timeout=5) as client:
        for name in _SESSION_FIXTURES:
            if name not in item.funcargs:
                continue
            base_url, session_id = item.funcargs[name]
            write_json(directory / "session.json", {"session_id": session_id, "fixture": name})
            _capture(
                client, f"{base_url}/v1/sessions/{session_id}/items", directory / "items.json"
            )
        model_url = os.environ["OMNIGENT_REPRO_MODEL_URL"]
        _capture(client, f"{model_url}/mock/requests", directory / "model-requests.json")


def _capture(client: httpx.Client, url: str, path: Path) -> None:
    try:
        response = client.get(url)
        response.raise_for_status()
        write_json(path, {"captured_at": time.time(), "data": response.json()})
    except (httpx.HTTPError, ValueError) as exc:
        write_json(path, {"captured_at": time.time(), "error": f"{type(exc).__name__}: {exc}"})


def pytest_sessionfinish(session, exitstatus):
    directory = session.config.getoption("--repro-evidence")
    failures = []
    call = directory / "call.json"
    if not call.exists() or json.loads(call.read_text())["outcome"] != "passed":
        failures.append("Recipe did not complete a passing interaction (skips are not success).")
    for name, key in (("items.json", "data"), ("model-requests.json", "requests")):
        path = directory / name
        if not path.exists() or not json.loads(path.read_text()).get("data", {}).get(key):
            failures.append(f"Missing, failed, or empty capture: {name}")
    if exitstatus == 0 and failures:
        session.exitstatus = 1
    write_json(
        directory / "result.json",
        {
            "purpose": "connectivity_smoke_only",
            "status": "passed" if session.exitstatus == 0 else "failed",
            "exit_code": int(session.exitstatus),
            "evidence_failures": failures,
        },
    )
