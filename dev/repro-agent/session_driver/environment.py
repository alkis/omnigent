"""Own the lifetime of an isolated local reproduction server, runner and model.

Runs independently of pytest. The supervisor has a fixed lease, stops all its
process groups on exit, and preserves logs and the database for inspection.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx

try:
    from .sessions import write_json
except ImportError:  # launched as an installed script
    from sessions import write_json


_logger = logging.getLogger(__name__)


def isolated_env(environ: dict[str, str], output: Path) -> dict[str, str]:
    """Keep proxy/tool plumbing while removing parent session and model state."""
    remove = {
        "RUNNER_SERVER_URL",
        "OMNIGENT_REMOTE_AUTH_TOKEN",
        "LLM_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "PYTEST_ADDOPTS",
        "OMNIGENT_CONFIG_HOME",
        "OMNIGENT_AUTH_ENABLED",
        "OMNIGENT_AUTH_PROVIDER",
    }
    env = {
        key: value
        for key, value in environ.items()
        if key not in remove
        and not key.startswith(("OMNIGENT_RUNNER_", "OMNIGENT_HOST_", "OMNIGENT_COMPAT_"))
    }
    env.update(
        {
            "OMNIGENT_CONFIG_HOME": str(output / "config"),
            "CLAUDE_CONFIG_DIR": str(output / "claude-config"),
            "OMNIGENT_DATA_DIR": str(output / "data"),
            "OMNIGENT_REPRO_TURN_DIR": str(output),
            "OMNIGENT_E2E_RECORD_DIR": str(output / "video"),
            "OMNIGENT_AUTH_PROVIDER": "header",
            "OMNIGENT_LOCAL_SINGLE_USER": "1",
            "OMNIGENT_DISABLE_CATALOG_LOOKUP": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
    )
    for key in ("NO_PROXY", "no_proxy"):
        env[key] = ",".join(filter(None, (env.get(key), "localhost,127.0.0.1,::1")))
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(Path(__file__).resolve().parent), env.get("PYTHONPATH")))
    )
    return env


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write_model_config(
    config_home: Path, mock_url: str, claude_model: str, codex_model: str
) -> None:
    config_home.mkdir(parents=True, exist_ok=True)
    config = {
        "providers": {
            "repro-claude": {
                "kind": "key",
                "default": ["anthropic"],
                "anthropic": {
                    "base_url": mock_url,
                    "api_key": "mock-key",
                    "models": {"default": claude_model},
                },
            },
            "repro-openai": {
                "kind": "key",
                "default": ["openai"],
                "openai": {
                    "base_url": f"{mock_url}/v1",
                    "api_key": "mock-key",
                    "wire_api": "responses",
                    "models": {"default": codex_model},
                },
            },
        }
    }
    write_json(config_home / "config.yaml", config)


def start_environment(
    checkout: str, output: str, model_backend: str = "mock", lease_seconds: int = 1800
) -> dict:
    if model_backend != "mock":
        raise ValueError(
            "only explicit mock backend is currently supported; "
            "live-provider validation is separate"
        )
    if not 60 <= lease_seconds <= 3600:
        raise ValueError("lease_seconds must be 60..3600")
    root = Path(checkout).resolve()
    if not (root / "omnigent/server/static/web-ui/index.html").is_file():
        raise ValueError("build the product SPA first: pnpm --filter web run build")
    if not (root / "tests/server/integration/mock_llm_server.py").is_file():
        raise ValueError("checkout must contain the product's mock model server")
    models = json.loads((root / "tests/server/integration/repro_models.json").read_text())
    base = Path(output).resolve()
    base.mkdir(parents=True, exist_ok=True)
    attempt = Path(tempfile.mkdtemp(prefix="environment-", dir=base))
    state = {
        "status": "starting",
        "attempt_id": uuid.uuid4().hex,
        "workspace": str(root),
        "model_backend": model_backend,
        "models": models,
        "network_scope": "runner-local",
        "environment": str(attempt / "environment.json"),
        "expires_at": time.time() + lease_seconds,
        "logs": str(attempt),
    }
    write_json(attempt / "environment.json", state)
    env = isolated_env(dict(os.environ), attempt)
    env["PYTHONPATH"] = os.pathsep.join(
        (
            str(root),
            str(root / "sdks/python-client"),
            str(root / "sdks/ui"),
            env.get("PYTHONPATH", ""),
        )
    )
    with (attempt / "supervisor.log").open("w") as log:
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), str(attempt)],
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    deadline = time.monotonic() + 120
    try:
        while time.monotonic() < deadline:
            state = json.loads((attempt / "environment.json").read_text())
            if state["status"] == "ready":
                return state
            if state["status"] == "failed" or proc.poll() is not None:
                raise RuntimeError(
                    f"environment startup failed; inspect {attempt}: {state.get('error', '')}"
                )
            time.sleep(0.2)
        raise TimeoutError(f"environment startup timed out; inspect {attempt}")
    except BaseException:
        (attempt / "stop").touch()
        # The supervisor checks stop during startup and owns child cleanup.
        try:
            proc.wait(timeout=25)
        except subprocess.TimeoutExpired:
            proc.terminate()
        raise


def stop_environment(environment: str) -> dict:
    path = Path(environment).resolve()
    state = json.loads(path.read_text())
    if state["status"] in ("stopped", "failed"):
        return state
    (path.parent / "stop").touch()
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        state = json.loads(path.read_text())
        if state["status"] in ("stopped", "failed"):
            return state
        time.sleep(0.1)
    return {
        **state,
        "detail": "stop requested; supervisor cleanup has not yet completed",
    }


def supervise(output: Path) -> None:
    from omnigent.runner.identity import token_bound_runner_id

    state = json.loads((output / "environment.json").read_text())
    root = Path(state["workspace"])
    children = []
    logs = []
    stopping = False

    def on_signal(*_args):
        nonlocal stopping
        stopping = True

    old_term = signal.signal(signal.SIGTERM, on_signal)
    old_int = signal.signal(signal.SIGINT, on_signal)

    def cancelled():
        return stopping or (output / "stop").exists() or time.time() >= state["expires_at"]

    def spawn(name, command, env):
        handle = (output / f"{name}.log").open("w")
        logs.append(handle)
        process = subprocess.Popen(
            command,
            cwd=root,
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        children.append(process)
        return process

    def ready(client, url, predicate, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancelled():
                raise InterruptedError("environment stopped during startup")
            if any(child.poll() is not None for child in children):
                raise RuntimeError("environment process exited; inspect process logs")
            try:
                response = client.get(url)
                if response.status_code == 200 and predicate(response):
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.2)
        raise TimeoutError(f"not ready: {url}")

    try:
        env = dict(os.environ)
        claude_dir = output / "claude-config"
        claude_dir.mkdir(exist_ok=True)
        write_json(
            claude_dir / ".claude.json",
            {
                "hasCompletedOnboarding": True,
                "projects": {str(root): {"hasTrustDialogAccepted": True}},
            },
        )
        mock_port = _port()
        mock_url = f"http://127.0.0.1:{mock_port}"
        spawn(
            "model",
            [
                sys.executable,
                str(root / "tests/server/integration/mock_llm_server.py"),
                str(mock_port),
            ],
            env,
        )
        with httpx.Client(trust_env=False, timeout=2) as client:
            ready(client, f"{mock_url}/stats", lambda _: True, timeout=20)
            write_model_config(
                output / "config",
                mock_url,
                state["models"]["claude-native"],
                state["models"]["codex-native"],
            )
            response = client.post(
                f"{mock_url}/mock/set_fallback",
                json={"key": "_policy_llm_", "text": '{"action":"allow","reason":""}'},
            )
            response.raise_for_status()
            port = _port()
            base_url = f"http://127.0.0.1:{port}"
            token = secrets.token_urlsafe(32)
            runner_id = token_bound_runner_id(token)
            env.update(
                OPENAI_BASE_URL=f"{mock_url}/v1",
                OPENAI_API_KEY="mock-key",
                OMNIGENT_WEB_UI_DIST=str(root / "omnigent/server/static/web-ui"),
            )
            server_env = {**env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": token}
            spawn(
                "server",
                [
                    sys.executable,
                    "-m",
                    "omnigent",
                    "server",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--database-uri",
                    f"sqlite:///{output / 'sessions.db'}",
                    "--artifact-location",
                    str(output / "artifacts"),
                ],
                server_env,
            )
            runner_env = {
                **env,
                "RUNNER_SERVER_URL": base_url,
                "OMNIGENT_RUNNER_ID": runner_id,
                "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": token,
                "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
            }
            spawn("runner", [sys.executable, "-m", "omnigent.runner._entry"], runner_env)
            ready(
                client,
                f"{base_url}/v1/runners/{runner_id}/status",
                lambda r: r.json().get("online") is True,
            )
        state.update(status="ready", base_url=base_url, mock_url=mock_url, runner_id=runner_id)
        write_json(output / "environment.json", state)
        while not cancelled():
            if any(child.poll() is not None for child in children):
                raise RuntimeError("environment process exited; inspect process logs")
            time.sleep(0.2)
        state["status"] = "stopped"
    except Exception as exc:
        _logger.exception("Reproduction environment failed")
        state.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        # Signal groups, including descendants even if their direct parent exited.
        for child in reversed(children):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGTERM)
        deadline = time.monotonic() + 10
        for child in reversed(children):
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.wait(timeout=max(0.01, deadline - time.monotonic()))
        for child in reversed(children):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        for handle in logs:
            handle.close()
        write_json(output / "environment.json", state)
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    supervise(parser.parse_args().output)
