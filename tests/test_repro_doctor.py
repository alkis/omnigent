"""Environment fidelity must not be inferred from a connected mock runtime."""

import json
import subprocess
import time
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from dev.repro_env import doctor, evidence
from dev.repro_env.__main__ import smoke
from dev.repro_env.runtime import write_json


@pytest.mark.parametrize(
    "observed,expected,status",
    [
        ("Linux", "Linux", "match"),
        ("Linux", "Darwin", "mismatch"),
        (None, "Linux", "unknown"),
        (False, False, "match"),
        (False, 0, "mismatch"),
    ],
)
def test_exact_requirement_comparison(observed, expected, status):
    result = doctor.compare_requirements(
        {"field": observed}, [{"field": "field", "expected": expected, "source": "report"}]
    )
    assert result[0]["status"] == status
    assert result[0]["actual"] == observed


def test_unsupported_requirement_is_unknown():
    assert (
        doctor.compare_requirements(
            {}, [{"field": "mobile.keyboard", "expected": "iOS", "source": "comment"}]
        )[0]["status"]
        == "unknown"
    )


@pytest.mark.parametrize(
    "requirements",
    [
        {},
        ["Linux"],
        [{"field": "os.system", "expected": "Linux"}],
        [{"field": "os.system", "expected": None, "source": "report"}],
        [{"field": "os.system", "expected": "Linux", "source": " "}],
    ],
)
def test_invalid_requirements_fail_explicitly(requirements):
    with pytest.raises(ValueError):
        doctor.compare_requirements({}, requirements)


def _runtime(tmp_path, monkeypatch, *, online=True, expired=False):
    write_json(
        tmp_path / "environment.json",
        {
            "status": "ready",
            "runner_id": "runner",
            "expires_at": time.time() + (-1 if expired else 60),
        },
    )
    write_json(
        tmp_path / "launch-observations.json",
        {
            "captured_at": 100,
            "observed": {
                "claude-native.version": "test-cli",
                "claude-native.machine_policy_files": [],
                "model_backend": "mock",
            },
        },
    )
    monkeypatch.setattr(doctor, "Relay", lambda **kwargs: nullcontext(SimpleNamespace(port=1234)))
    client = Mock()
    client.get.return_value = httpx.Response(
        200, json={"online": online}, request=httpx.Request("GET", "http://localhost")
    )
    monkeypatch.setattr(doctor.httpx, "Client", lambda **kwargs: nullcontext(client))
    return client


def test_connected_runtime_does_not_imply_reported_environment(tmp_path, monkeypatch):
    _runtime(tmp_path, monkeypatch)
    requirements = [{"field": "model_backend", "expected": "live", "source": "report"}]
    path = tmp_path / "doctor.json"
    assert doctor.doctor(tmp_path, "claude-native", requirements, path) == 1
    report = json.loads(path.read_text())
    assert report["ready_for_smoke"]
    assert report["requirements_status"] == "mismatch"


def test_missing_manifest_cannot_use_callers_cli_version(tmp_path, monkeypatch):
    _runtime(tmp_path, monkeypatch)
    (tmp_path / "launch-observations.json").unlink()
    monkeypatch.setattr(doctor, "launch_observations", Mock(side_effect=AssertionError))
    report = doctor.inspect_environment(tmp_path, "claude-native", [])
    assert not report["ready_for_smoke"]
    assert report["requirements_status"] == "not_assessed"
    assert report["observed_at_launch"] == {}


@pytest.mark.parametrize("expired,online", [(True, True), (False, False)])
def test_ready_file_alone_is_not_readiness(tmp_path, monkeypatch, expired, online):
    client = _runtime(tmp_path, monkeypatch, expired=expired, online=online)
    report = doctor.inspect_environment(tmp_path, "claude-native", [])
    assert not report["ready_for_smoke"]
    if expired:
        client.get.assert_not_called()


def test_connection_failure_is_preserved(tmp_path, monkeypatch):
    client = _runtime(tmp_path, monkeypatch)
    client.get.side_effect = httpx.ConnectError("runner unavailable")
    report = doctor.inspect_environment(tmp_path, "claude-native", [])
    assert not report["ready_for_smoke"]
    assert report["errors"] == ["ConnectError: runner unavailable"]


def test_machine_policy_blocks_native_mock_recipe(tmp_path, monkeypatch):
    _runtime(tmp_path, monkeypatch)
    manifest = tmp_path / "launch-observations.json"
    data = json.loads(manifest.read_text())
    data["observed"]["claude-native.machine_policy_files"] = ["/etc/managed-settings.json"]
    write_json(manifest, data)
    report = doctor.inspect_environment(tmp_path, "claude-native", [])
    assert report["live_checks"]["runner_online"]
    assert not report["ready_for_smoke"]
    assert "Machine CLI policy" in report["errors"][0]


def test_launch_records_policy_presence_without_copying_contents(tmp_path, monkeypatch):
    policy = tmp_path / "managed.json"
    policy.write_text('{"private": "not-for-evidence"}')
    monkeypatch.setattr(
        doctor,
        "_MACHINE_POLICIES",
        {"claude-native": (policy,), "codex-native": (tmp_path / "missing",)},
    )
    monkeypatch.setattr(doctor, "_command", lambda *args: None)
    observed = doctor.launch_observations(tmp_path)["observed"]
    assert observed["claude-native.machine_policy_files"] == [str(policy)]
    assert observed["codex-native.machine_policy_files"] == []
    assert "not-for-evidence" not in json.dumps(observed)


def test_failed_version_probe_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        doctor.subprocess, "run", Mock(side_effect=subprocess.TimeoutExpired("version", 5))
    )
    observed = doctor.launch_observations(tmp_path)["observed"]
    assert observed["claude-native.version"] is None
    assert observed["build.commit"] is None
    assert observed["build.dirty"] is None
    assert observed["session.harness"] is None


def test_smoke_does_not_launch_after_failed_doctor(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "doctor", lambda *args: 1)
    execute = Mock(side_effect=AssertionError("must not execute"))
    monkeypatch.setattr("dev.repro_env.__main__.execute", execute)
    assert smoke(tmp_path, "claude-native") == 1
    execute.assert_not_called()


def test_failed_journey_captures_before_fixture_teardown(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_REPRO_MODEL_URL", "http://model")
    client = Mock()
    client.get.return_value = httpx.Response(
        200, json={"items": ["observed"]}, request=httpx.Request("GET", "http://session")
    )
    monkeypatch.setattr(evidence.httpx, "Client", lambda **kwargs: nullcontext(client))
    report = SimpleNamespace(
        when="call", outcome="failed", duration=1, longrepr="assertion failed", sections=[]
    )
    item = SimpleNamespace(
        nodeid="test_smoke",
        config=SimpleNamespace(getoption=lambda name: tmp_path),
        funcargs={"native_codex_mock_session": ("http://server", "session")},
    )
    hook = evidence.pytest_runtest_makereport(item)
    next(hook)
    with pytest.raises(StopIteration):
        hook.send(SimpleNamespace(get_result=lambda: report))
    assert json.loads((tmp_path / "call.json").read_text())["outcome"] == "failed"
    assert json.loads((tmp_path / "session.json").read_text())["session_id"] == "session"
    assert json.loads((tmp_path / "items.json").read_text())["data"] == {"items": ["observed"]}
    assert client.get.call_args_list[0].args == ("http://server/v1/sessions/session/items",)


@pytest.mark.parametrize("outcome", [None, "skipped", "passed"])
def test_missing_or_skipped_evidence_never_passes(tmp_path, outcome):
    if outcome:
        write_json(tmp_path / "call.json", {"outcome": outcome})
    session = SimpleNamespace(
        config=SimpleNamespace(getoption=lambda name: tmp_path), exitstatus=0
    )
    evidence.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 1


def test_complete_smoke_evidence_can_pass(tmp_path):
    write_json(tmp_path / "call.json", {"outcome": "passed"})
    write_json(tmp_path / "items.json", {"data": {"data": [{"role": "assistant"}]}})
    write_json(tmp_path / "model-requests.json", {"data": {"requests": [{"model": "mock"}]}})
    session = SimpleNamespace(
        config=SimpleNamespace(getoption=lambda name: tmp_path), exitstatus=0
    )
    evidence.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 0
    assert json.loads((tmp_path / "result.json").read_text())["status"] == "passed"


def test_empty_captures_fail_a_passing_assertion(tmp_path):
    write_json(tmp_path / "call.json", {"outcome": "passed"})
    write_json(tmp_path / "items.json", {"data": {"data": []}})
    write_json(tmp_path / "model-requests.json", {"data": {"requests": []}})
    session = SimpleNamespace(
        config=SimpleNamespace(getoption=lambda name: tmp_path), exitstatus=0
    )
    evidence.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 1
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["status"] == "failed"
    assert len(result["evidence_failures"]) == 2
