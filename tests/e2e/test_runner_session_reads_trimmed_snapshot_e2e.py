"""E2E: runner-owned session reads must not pay for the full page snapshot.

The runner reads ``GET /v1/sessions/{id}`` on the server for a handful of
stored-row fields (the native launch-config read, the host-spawn check, the
runner's own session lookup, the model-override re-read). With no query
parameters the server builds the full snapshot: the last 100 transcript items,
the liveness lookup, model options, usage totals, and -- on a status-cache
miss -- a live-status probe of the session's bound runner. That probe is
circular for these callers: the runner asks the server about a session it
owns, and the server turns around and probes that same runner while the
runner is waiting on the answer. Under load the launch-config budget expires
and the native terminal fails to start.

Two facets of the reported bug are exercised:

* ``test_runner_session_reads_request_trimmed_snapshot`` -- every
  runner-originated ``GET /v1/sessions/{id}`` read must carry
  ``include_items=false&include_liveness=false&include_live_status=false``.
  Driven through the real journey: a real ``omnigent server`` subprocess, a
  real runner subprocess bound over the tunnel, a real codex-native wrapper
  session whose bind auto-creates the Codex terminal. The runner's server
  HTTP client is wrapped with an observation-only request hook that records
  every outgoing request line; no product behavior is altered.

* ``test_get_session_accepts_include_live_status`` -- the server route must
  declare ``include_live_status`` as a validated boolean query parameter (the
  precondition for skipping the probe). On the reported build the route knows
  only ``include_items`` / ``include_liveness``, so an invalid value for
  those returns 422 while an invalid ``include_live_status`` is silently
  ignored (200) -- the flag does not exist.

Run::

    .venv/bin/python -m pytest \
        tests/e2e/test_runner_session_reads_trimmed_snapshot_e2e.py -v
"""

from __future__ import annotations

import io
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Every HTTP call targets 127.0.0.1; bypass any CI egress proxy.
_http = httpx.Client(trust_env=False)

_PYTHONPATH = os.pathsep.join(
    [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
        os.environ.get("PYTHONPATH", ""),
    ]
)

# The flags the ticket's expected behavior requires on runner-owned reads.
_REQUIRED_TRIM_PARAMS = (
    "include_items=false",
    "include_liveness=false",
    "include_live_status=false",
)

# Observation-only wiretap on the runner's server client: records every
# outgoing runner->server request line via an httpx request event hook, then
# delegates to the real request.
_RUNNER_BOOTSTRAP = """
import os

_WIRETAP = os.environ["OMNIGENT_E2E_WIRETAP_FILE"]


def _tap(line):
    with open(_WIRETAP, "a") as f:
        f.write(line + "\\n")


import omnigent.cli_auth as _cli_auth

_orig_open_server_client = _cli_auth.open_server_client


def _open_server_client_tapped(*args, **kwargs):
    client = _orig_open_server_client(*args, **kwargs)

    async def _record(request):
        _tap("OUT " + request.method + " " + request.url.raw_path.decode())

    hooks = dict(client.event_hooks)
    hooks["request"] = list(hooks.get("request", ())) + [_record]
    client.event_hooks = hooks
    return client


_cli_auth.open_server_client = _open_server_client_tapped

from omnigent.runner._entry import main

main()
"""

_HEALTH_TIMEOUT_S = 120.0
_POLL_S = 0.5
_LAUNCH_TIMEOUT_S = 180.0

_NEEDS_CODEX = [
    pytest.mark.skipif(
        shutil.which("tmux") is None,
        reason="codex-native terminals run inside tmux; tmux not installed",
    ),
    pytest.mark.skipif(
        shutil.which("codex") is None,
        reason="the codex-native launch starts the codex CLI; codex not installed",
    ),
]


def _find_free_port() -> int:
    """Grab an ephemeral port for the spawned server."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _localhost_env(extra: dict[str, str]) -> dict[str, str]:
    """Subprocess env with worktree imports and no proxy in the way.

    :param extra: Overrides/additions applied after the base env.
    :returns: Environment mapping for ``subprocess.Popen``.
    """
    env = {
        **os.environ,
        "PYTHONPATH": _PYTHONPATH,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(name, None)
    env.update(extra)
    return env


def _terminate(proc: subprocess.Popen[bytes] | None) -> None:
    """Best-effort SIGTERM -> SIGKILL teardown for a spawned process."""
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _wait_http_ok(url: str, deadline: float) -> None:
    """Poll *url* until it returns 200 or *deadline* (monotonic) passes."""
    last = "not polled"
    while time.monotonic() < deadline:
        try:
            if _http.get(url, timeout=2.0).status_code == 200:
                return
            last = "non-200"
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(_POLL_S)
    raise AssertionError(f"{url} never became healthy: {last}")


def _create_codex_native_session(base_url: str) -> str:
    """Create a codex-native wrapper session exactly like ``omnigent codex``.

    :param base_url: Spawned server base URL.
    :returns: The new session/conversation id.
    """
    from omnigent._wrapper_labels import (
        CODEX_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.harnesses.codex_native.main import _materialize_codex_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        yaml_text = _materialize_codex_agent_spec(Path(tmp), model=None).read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        info = tarfile.TarInfo("codex-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: CODEX_NATIVE_WRAPPER_VALUE,
    }
    create = _http.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"labels": labels})},
        files={
            "bundle": (
                "codex-native-ui.tar.gz",
                buf.getvalue(),
                "application/gzip",
            )
        },
        timeout=30.0,
    )
    create.raise_for_status()
    return str(create.json()["session_id"])


def _create_plain_session(base_url: str) -> str:
    """Create a plain (non-wrapper) session from a minimal agent bundle.

    :param base_url: Spawned server base URL.
    :returns: The new session/conversation id.
    """
    config = yaml.dump(
        {
            "spec_version": 1,
            "name": "snapshot-probe-test",
            "executor": {
                "type": "omnigent",
                "config": {"harness": "openai-agents"},
            },
            "llm": {
                "model": "snapshot-probe-test",
                "connection": {"api_key": "test-key"},
            },
        }
    ).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="config.yaml")
        info.size = len(config)
        tar.addfile(info, io.BytesIO(config))
    create = _http.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"labels": {}})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    return str(create.json()["session_id"])


def _start_server(tmp_path: Path) -> tuple[subprocess.Popen[bytes], str, str]:
    """Start a real ``omnigent server`` subprocess, wait for health.

    :param tmp_path: Scratch dir for the DB, artifacts, and logs.
    :returns: ``(process, base_url, binding_token)``.
    """
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    binding_token = secrets.token_urlsafe(32)
    server_log = (tmp_path / "server.log").open("w")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent.cli",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{tmp_path / 'chat.db'}",
            "--artifact-location",
            str(tmp_path / "artifacts"),
        ],
        env=_localhost_env({"OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}),
        stdout=server_log,
        stderr=subprocess.STDOUT,
    )
    _wait_http_ok(f"{base_url}/health", time.monotonic() + _HEALTH_TIMEOUT_S)
    return proc, base_url, binding_token


@dataclass
class _Stack:
    """Handles to the booted server + wiretapped runner."""

    base_url: str
    runner_id: str
    wiretap_file: Path
    runner_log_file: Path

    def wiretap_lines(self) -> list[str]:
        if not self.wiretap_file.exists():
            return []
        return self.wiretap_file.read_text().splitlines()

    def runner_log(self) -> str:
        return self.runner_log_file.read_text() if self.runner_log_file.exists() else ""


def _session_read_lines(lines: list[str], session_id: str) -> list[str]:
    """Filter wiretap lines to GET reads of exactly ``/v1/sessions/{session_id}``.

    :param lines: Raw wiretap lines, e.g. ``"OUT GET /v1/sessions/abc?x=1"``.
    :param session_id: The session whose reads to keep.
    :returns: The matching lines, sub-resource paths excluded.
    """
    pattern = re.compile(rf"^OUT GET /v1/sessions/{re.escape(session_id)}(\?.*)?$")
    return [line for line in lines if pattern.match(line)]


@pytest.fixture(scope="module")
def contract_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Boot a real server subprocess (no runner) for route-contract checks.

    :param tmp_path_factory: Pytest factory for the module's scratch dir.
    :yields: The server base URL.
    """
    tmp_path = tmp_path_factory.mktemp("session-read-contract")
    proc, base_url, _token = _start_server(tmp_path)
    try:
        yield base_url
    finally:
        _terminate(proc)


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Stack]:
    """Boot a real server + a wiretapped real runner bound over the tunnel.

    :param tmp_path_factory: Pytest factory for the module's scratch dir.
    :yields: Handles to the running stack.
    """
    tmp_path = tmp_path_factory.mktemp("runner-session-reads")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner_home = tmp_path / "home"
    runner_home.mkdir()
    wiretap_file = tmp_path / "wiretap.log"
    runner_log_file = tmp_path / "runner-process.log"

    server_proc, base_url, binding_token = _start_server(tmp_path)
    from omnigent.runner.identity import token_bound_runner_id

    runner_id = token_bound_runner_id(binding_token)

    runner_stdout = (tmp_path / "runner.stdout.log").open("w")
    runner_proc: subprocess.Popen[bytes] | None = None
    try:
        runner_proc = subprocess.Popen(
            [sys.executable, "-c", _RUNNER_BOOTSTRAP],
            env=_localhost_env(
                {
                    "OMNIGENT_RUNNER_ID": runner_id,
                    "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
                    "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                    "RUNNER_SERVER_URL": base_url,
                    "OMNIGENT_RUNNER_WORKSPACE": str(workspace),
                    "HOME": str(runner_home),
                    "OMNIGENT_PROCESS_LOG_FILE": str(runner_log_file),
                    "OMNIGENT_LOG_LEVEL": "INFO",
                    "OMNIGENT_E2E_WIRETAP_FILE": str(wiretap_file),
                }
            ),
            stdout=runner_stdout,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        online = False
        while time.monotonic() < deadline:
            try:
                status = _http.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2.0)
                if status.status_code == 200 and status.json().get("online") is True:
                    online = True
                    break
            except httpx.HTTPError:
                pass
            time.sleep(_POLL_S)
        runner_log = runner_log_file.read_text() if runner_log_file.exists() else ""
        assert online, f"runner never came online; log:\n{runner_log[-3000:]}"

        yield _Stack(
            base_url=base_url,
            runner_id=runner_id,
            wiretap_file=wiretap_file,
            runner_log_file=runner_log_file,
        )
    finally:
        _terminate(runner_proc)
        _terminate(server_proc)
        runner_stdout.close()


@pytest.mark.parametrize("_marker", [pytest.param(None, marks=_NEEDS_CODEX)])
def test_runner_session_reads_request_trimmed_snapshot(stack: _Stack, _marker: None) -> None:
    """Runner-owned session reads must carry the snapshot-trimming flags.

    Journey: a codex-native session is bound to the runner, which auto-creates
    the Codex terminal -- performing the launch-config read and the host-spawn
    check against ``GET /v1/sessions/{id}``. Each such read must ask the
    server to skip the transcript items, the liveness lookup, and the
    runner-status probe; a bare read makes the server build the full page and
    probe the runner that is itself waiting on the answer.

    :param stack: The booted server + wiretapped runner.
    :param _marker: Parametrize placeholder carrying the tmux/codex skips.
    """
    session_id = _create_codex_native_session(stack.base_url)

    _http.patch(
        f"{stack.base_url}/v1/sessions/{session_id}",
        json={"runner_id": stack.runner_id},
        timeout=30.0,
    ).raise_for_status()

    deadline = time.monotonic() + _LAUNCH_TIMEOUT_S
    launch_settled = False
    while time.monotonic() < deadline:
        log = stack.runner_log()
        if f"Auto-created codex terminal + forwarder for session {session_id}" in log or (
            f"Failed to auto-create codex terminal for {session_id}" in log
        ):
            launch_settled = True
            break
        time.sleep(_POLL_S)
    reads = _session_read_lines(stack.wiretap_lines(), session_id)
    assert launch_settled and reads, (
        "the codex-native launch never performed a runner->server session read; "
        f"launch_settled={launch_settled}; runner log:\n{stack.runner_log()[-4000:]}"
    )

    untrimmed = [
        line for line in reads if not all(param in line for param in _REQUIRED_TRIM_PARAMS)
    ]
    assert not untrimmed, (
        "runner-owned session reads fetched the full page snapshot instead of "
        f"passing {'&'.join(_REQUIRED_TRIM_PARAMS)}; untrimmed reads observed "
        "on the wire:\n" + "\n".join(untrimmed)
    )


def test_get_session_accepts_include_live_status(contract_server: str) -> None:
    """The route must declare ``include_live_status`` as a validated bool.

    Skipping the runner probe requires a query flag the route understands.
    The server validates its declared boolean query params, so an invalid
    value for a *declared* flag is rejected with 422 while an invalid value
    for an *undeclared* one is silently ignored (200). On the reported build
    the route knows only ``include_items`` / ``include_liveness`` -- an
    invalid ``include_live_status`` sails through as 200, proving the flag
    does not exist and the probe cannot be skipped.

    :param contract_server: Base URL of a real server subprocess.
    """
    session_id = _create_plain_session(contract_server)
    base = f"{contract_server}/v1/sessions/{session_id}"

    # Positive controls: the server DOES validate its declared bool flags, so
    # an invalid value is a 422 -- silence for include_live_status below is
    # therefore "undeclared", not "leniently parsed".
    assert _http.get(f"{base}?include_items=notabool", timeout=30.0).status_code == 422
    assert _http.get(f"{base}?include_liveness=notabool", timeout=30.0).status_code == 422

    invalid = _http.get(f"{base}?include_live_status=notabool", timeout=30.0)
    assert invalid.status_code == 422, (
        "GET /v1/sessions/{id} accepted an invalid include_live_status value as "
        f"HTTP {invalid.status_code}; the route does not declare the flag, so the "
        "runner-status probe cannot be skipped (declared bool flags return 422)."
    )

    valid = _http.get(f"{base}?include_live_status=false", timeout=30.0)
    assert valid.status_code == 200, valid.text[:500]
    assert "status" in valid.json(), "snapshot must still report a status field"
