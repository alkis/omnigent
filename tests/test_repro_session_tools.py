"""Turn attribution, lifecycle and real Omnigent tool packaging regressions."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import httpx
import pytest

_AGENT_DIR = Path(__file__).resolve().parents[1] / "dev/repro-agent"
sys.path.insert(0, str(_AGENT_DIR))

sessions = importlib.import_module("session_driver.sessions")
environment = importlib.import_module("session_driver.environment")
SessionDriver = sessions.SessionDriver
write_json = sessions.write_json
isolated_env = environment.isolated_env
start_environment = environment.start_environment
write_model_config = environment.write_model_config


def message(identifier, role, text):
    return {
        "id": identifier,
        "type": "message",
        "status": "completed",
        "role": role,
        "content": [
            {
                "type": "output_text" if role == "assistant" else "input_text",
                "text": text,
            }
        ],
        "response_id": "native-output" if role == "assistant" else "input",
    }


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    state = {
        "status": "ready",
        "attempt_id": "attempt",
        "model_backend": "mock",
        "base_url": "http://local",
        "mock_url": "http://model",
        "runner_id": "runner",
        "workspace": str(tmp_path),
        "models": {"codex-native": "mock-codex"},
    }
    write_json(tmp_path / "environment.json", state)
    wire = {"items": [], "status": "idle", "requests": [], "fail_send": False}

    def handle(request):
        wire["requests"].append(request)
        path = request.url.path
        if path == "/v1/sessions" and request.method == "POST":
            return httpx.Response(200, json={"session_id": "session"})
        if path.endswith("/items"):
            return httpx.Response(200, json={"data": wire["items"], "has_more": False})
        if path.endswith("/events"):
            wire["items"].append(
                message(
                    "new-user",
                    "user",
                    json.loads(request.content)["data"]["content"][0]["text"],
                )
            )
            if wire["fail_send"]:
                raise httpx.ReadTimeout("lost ack")
            return httpx.Response(202, json={"queued": True, "item_id": "new-user"})
        return httpx.Response(200, json={"status": wire["status"]})

    monkeypatch.setattr(sessions, "session_bundle", lambda *_: (b"bundle", {}))
    with SessionDriver(
        tmp_path / "environment.json", transport=httpx.MockTransport(handle)
    ) as driver:
        driver.start("codex-native", "actual reproduction journey")
        yield driver, wire


def test_initial_idle_stale_reply_and_partial_turn_do_not_pass(runtime):
    driver, wire = runtime
    wire["items"] = [message("old", "assistant", "answer")]
    turn = driver.send("session", "prompt", "answer")
    assert driver.wait("session", turn["turn_id"], 0)["status"] == "pending"
    wire["items"].append(message("reply", "assistant", "answer"))
    wire["status"] = "running"
    assert driver.wait("session", turn["turn_id"], 0)["status"] == "pending"
    wire["status"] = "idle"
    result = driver.wait("session", turn["turn_id"], 0)
    assert result["status"] == "completed"
    assert result["user"]["id"] == "new-user"
    assert result["assistants"][0]["response_id"] != result["user"]["response_id"]
    record = json.loads(driver._file("session").read_text())
    assert record["journey"] == "actual reproduction journey"
    assert record["turns"][0]["status"] == "completed"


def test_browser_baseline_does_not_submit_input(runtime):
    driver, wire = runtime
    turn = driver.begin("session", "prompt", "answer")
    assert not any(r.url.path.endswith("/events") for r in wire["requests"])
    assert driver.wait("session", turn["turn_id"], 0)["status"] == "pending"
    wire["items"] = [
        message("browser-user", "user", "prompt"),
        message("reply", "assistant", "answer"),
    ]
    result = driver.wait("session", turn["turn_id"], 0)
    assert result["status"] == "completed"
    assert result["surface"] == "browser"


@pytest.mark.parametrize("defect", ["other-input", "two-users", "wrong-ack", "failed-session"])
def test_unrelated_or_failed_turn_cannot_pass(runtime, defect):
    driver, wire = runtime
    turn = driver.send("session", "prompt", "answer")
    wire["items"].append(message("reply", "assistant", "answer"))
    if defect == "other-input":
        wire["items"][0] = message("new-user", "user", "other")
    elif defect == "two-users":
        wire["items"].append(message("another", "user", "prompt"))
    elif defect == "wrong-ack":
        wire["items"][0]["id"] = "not-ack"
    else:
        wire["status"] = "failed"
    assert driver.wait("session", turn["turn_id"], 0)["status"] == "failed"


def test_lost_send_ack_stays_pending_and_is_not_retried(runtime):
    driver, wire = runtime
    wire["fail_send"] = True
    with pytest.raises(httpx.ReadTimeout):
        driver.send("session", "prompt", "answer")
    with pytest.raises(ValueError, match="pending"):
        driver.send("session", "prompt", "answer")
    assert sum(r.url.path.endswith("/events") for r in wire["requests"]) == 1
    driver.close("session")
    record = json.loads(driver._file("session").read_text())
    assert record["turns"][0]["status"] == "cancelled"
    assert driver.close("session")["closed"]


def test_pending_turn_excludes_other_sessions(runtime):
    driver, _wire = runtime
    other = driver._load("session")
    other["session_id"] = "other"
    write_json(driver._file("other"), other)
    driver.begin("session", "prompt", "answer")
    with pytest.raises(ValueError, match="another turn is pending"):
        driver.begin("other", "prompt", "answer")


def test_wait_for_unknown_turn_and_unowned_session_fail(runtime):
    driver, _ = runtime
    with pytest.raises(ValueError, match="unknown turn"):
        driver.wait("session", "missing", 0)
    with pytest.raises(FileNotFoundError):
        driver.inspect("other-session")
    with pytest.raises(ValueError, match="invalid session"):
        driver.inspect("../escape")


def test_pagination_reads_beyond_first_hundred_items(tmp_path):
    write_json(
        tmp_path / "environment.json",
        {"status": "ready", "model_backend": "mock", "base_url": "http://local"},
    )
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.params.get("after") == "old":
            return httpx.Response(
                200,
                json={
                    "data": [message("new", "assistant", "answer")],
                    "has_more": False,
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [message("old", "user", "prompt")],
                "has_more": True,
                "last_id": "old",
            },
        )

    with SessionDriver(
        tmp_path / "environment.json", transport=httpx.MockTransport(handle)
    ) as driver:
        assert [i["id"] for i in driver.items("session")] == ["old", "new"]
        assert len(requests) == 2


def test_reject_live_backend_and_missing_build(tmp_path):
    with pytest.raises(ValueError, match="only explicit mock"):
        start_environment(str(tmp_path), str(tmp_path), "live")
    with pytest.raises(ValueError, match="build the product SPA"):
        start_environment(str(tmp_path), str(tmp_path))


def test_actual_tool_loader_and_runner_dispatch_agree_on_names(tmp_path):
    import shutil

    from omnigent.runner.tool_dispatch import _is_spec_local_python_tool
    from omnigent.spec import load, materialize_bundle
    from omnigent.tools.local import load_local_python_tools

    source = _AGENT_DIR
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "config.yaml").write_text(
        "spec_version: 1\nname: session-test\ninstructions: test\n"
        "executor:\n  type: omnigent\n  config:\n    harness: openai-agents\n"
    )
    for directory in ("session_driver", "tools"):
        shutil.copytree(
            source / directory,
            agent / directory,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
    unpacked = materialize_bundle(agent, tmp_path / "unpacked")
    spec = load(unpacked)
    loaded = load_local_python_tools(
        spec.local_tools, unpacked, srt_available=False, uv_available=False
    )
    names = {tool.name() for tool in loaded}
    assert names == {
        "repro_start_environment",
        "repro_stop_environment",
        "repro_start_session",
        "repro_prepare_browser_turn",
        "repro_send_message",
        "repro_wait_for_turn",
        "repro_inspect_session",
        "repro_close_session",
    }
    assert all(_is_spec_local_python_tool(name, spec) for name in names)
    for tool in loaded:
        assert tool.get_schema()["function"]["parameters"]["properties"]


def test_partial_assistant_item_is_not_completion(runtime):
    driver, wire = runtime
    turn = driver.send("session", "prompt", "answer")
    partial = message("reply", "assistant", "answer")
    partial["status"] = "in_progress"
    wire["items"].append(partial)
    assert driver.wait("session", turn["turn_id"], 0)["status"] == "pending"


def test_supervisor_cleans_up_on_startup_cancellation(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys

    if os.name != "posix":
        pytest.skip("process-group lifecycle is POSIX-only")
    write_json(
        tmp_path / "environment.json",
        {"status": "starting", "workspace": str(tmp_path), "expires_at": 0},
    )
    processes = []
    real_popen = subprocess.Popen

    def spawn(command, **kwargs):
        process = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(environment.subprocess, "Popen", spawn)
    environment.supervise(tmp_path)
    assert processes and all(process.poll() is not None for process in processes)
    assert json.loads((tmp_path / "environment.json").read_text())["status"] == "failed"


def test_nested_runner_and_model_state_cannot_select_live_lane(tmp_path: Path) -> None:
    ambient = {
        "OMNIGENT_RUNNER_ID": "parent",
        "OMNIGENT_RUNNER_ZYGOTE_CONTROL_FD": "8",
        "OMNIGENT_RUNNER_ENV_PASSTHROUGH": "LLM_API_KEY",
        "OMNIGENT_HOST_TOKEN": "parent-token",
        "RUNNER_SERVER_URL": "https://shared-app",
        "OMNIGENT_CONFIG_HOME": "/read-only-provider-config",
        "LLM_API_KEY": "oa_cred_placeholder",
        "CLAUDE_CODE_OAUTH_TOKEN": "ambient-token",
        "OPENAI_BASE_URL": "https://live-provider",
        "PYTEST_ADDOPTS": "-m not_native",
        "HTTPS_PROXY": "http://credential-proxy",
        "NO_PROXY": "existing.local",
        "PATH": "/pinned-clis",
        "PLAYWRIGHT_BROWSERS_PATH": "/browsers",
    }
    env = isolated_env(ambient, tmp_path)
    assert not any(key.startswith(("OMNIGENT_RUNNER_", "OMNIGENT_HOST_")) for key in env)
    for key in (
        "LLM_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENAI_BASE_URL",
        "RUNNER_SERVER_URL",
        "PYTEST_ADDOPTS",
    ):
        assert key not in env
    assert env["OMNIGENT_CONFIG_HOME"] == str(tmp_path / "config")
    assert env["HTTPS_PROXY"] == ambient["HTTPS_PROXY"]
    assert env["PATH"] == ambient["PATH"]
    assert "existing.local" in env["NO_PROXY"]
    assert "127.0.0.1" in env["NO_PROXY"]
    assert ambient["LLM_API_KEY"] == "oa_cred_placeholder"


def test_model_config_routes_both_families_to_the_local_mock(tmp_path: Path) -> None:
    config = tmp_path / "isolated-config"
    write_model_config(config, "http://127.0.0.1:51235", "mock-claude", "mock-codex")
    providers = json.loads((config / "config.yaml").read_text())["providers"]
    assert providers["repro-claude"]["anthropic"]["base_url"] == "http://127.0.0.1:51235"
    assert providers["repro-openai"]["openai"]["base_url"] == "http://127.0.0.1:51235/v1"
    assert providers["repro-openai"]["openai"]["wire_api"] == "responses"
