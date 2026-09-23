"""Observe the prepared runtime without inferring ticket fidelity from readiness."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import shutil
import subprocess
import time
from pathlib import Path

import httpx

from .runtime import write_json
from .transport import Relay

_MACHINE_POLICIES = {
    "claude-native": (
        Path("/etc/claude-code/managed-settings.json"),
        Path("/etc/claude-code/managed-settings.d"),
        Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
    ),
    "codex-native": (Path("/etc/codex/managed_config.toml"), Path("/etc/codex/requirements.toml")),
}


def _command(args: list[str], root: Path) -> str | None:
    try:
        result = subprocess.run(args, cwd=root, capture_output=True, text=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def launch_observations(root: Path) -> dict:
    """Run inside the supervisor's isolated environment, before starting its children."""
    dirty = _command(["git", "status", "--porcelain", "--untracked-files=normal"], root)
    index = root / "omnigent/server/static/web-ui/index.html"
    observed = {
        "build.commit": _command(["git", "rev-parse", "HEAD"], root),
        "build.dirty": None if dirty is None else bool(dirty),
        "build.diff_sha256": None,
        "ui.index_sha256": hashlib.sha256(index.read_bytes()).hexdigest()
        if index.is_file()
        else None,
        "ui.build_commit": None,
        "os.system": platform.system(),
        "os.machine": platform.machine(),
        "python.version": platform.python_version(),
        "model_backend": "mock",
        "auth.provider": "header",
        "auth.local_single_user": True,
        "catalog.lookup_enabled": False,
        "runner.idle_timeout_s": 0,
        "surface": None,
        "session.starting_state": None,
        "session.harness": None,
        "session.model": None,
    }
    diff = _command(["git", "diff", "HEAD", "--binary"], root)
    if diff is not None:
        observed["build.diff_sha256"] = hashlib.sha256(diff.encode()).hexdigest()
    for harness, executable in (("claude-native", "claude"), ("codex-native", "codex")):
        binary = shutil.which(executable)
        observed[f"{harness}.binary"] = binary
        observed[f"{harness}.version"] = _command([binary, "--version"], root) if binary else None
        observed[f"{harness}.machine_policy_files"] = [
            str(path) for path in _MACHINE_POLICIES[harness] if path.exists()
        ]
    try:
        observed["openai-agents.version"] = importlib.metadata.version("openai-agents")
    except importlib.metadata.PackageNotFoundError:
        observed["openai-agents.version"] = None
    return {"captured_at": time.time(), "observed": observed}


def compare_requirements(observed: dict, requirements: list[dict]) -> list[dict]:
    """Exact comparisons only; unknown or unsupported fields never become matches."""
    comparisons = []
    if not isinstance(requirements, list):
        raise ValueError("requirements must be a list of {field, expected, source} objects")
    for requirement in requirements:
        if (
            not isinstance(requirement, dict)
            or set(requirement) != {"field", "expected", "source"}
            or not isinstance(requirement["field"], str)
            or not requirement["field"].strip()
            or not isinstance(requirement["source"], str)
            or not requirement["source"].strip()
            or requirement["expected"] is None
        ):
            raise ValueError("each requirement needs field, non-null expected, and source")
        actual = observed.get(requirement["field"])
        status = "unknown" if actual is None else "mismatch"
        if actual is not None and type(actual) is type(requirement["expected"]):
            if actual == requirement["expected"]:
                status = "match"
        comparisons.append({**requirement, "actual": actual, "status": status})
    return comparisons


def inspect_environment(output: Path, harness: str, requirements: list[dict]) -> dict:
    state = json.loads((output / "environment.json").read_text())
    manifest_path = output / "launch-observations.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    observed = manifest.get("observed", {})
    checks = {
        "supervisor_ready": state.get("status") == "ready",
        "lease_active": state.get("expires_at", 0) > time.time(),
        "harness_installed_at_launch": bool(observed.get(f"{harness}.version")),
        "machine_policy_absent": harness == "openai-agents"
        or observed.get(f"{harness}.machine_policy_files") == [],
        "runner_online": False,
        "model_reachable": False,
    }
    errors = []
    if not checks["machine_policy_absent"]:
        errors.append(
            "Machine CLI policy is present or unobserved; isolated CLI homes do not prove "
            "mock routing. Use the configured CI runtime for validation; do not override policy."
        )
    if checks["supervisor_ready"] and checks["lease_active"]:
        try:
            with (
                Relay(unix_target=output / "server.sock") as server,
                Relay(unix_target=output / "model.sock") as model,
                httpx.Client(trust_env=False, timeout=5) as client,
            ):
                runner = client.get(
                    f"http://127.0.0.1:{server.port}/v1/runners/{state['runner_id']}/status"
                )
                runner.raise_for_status()
                checks["runner_online"] = runner.json().get("online") is True
                response = client.get(f"http://127.0.0.1:{model.port}/stats")
                response.raise_for_status()
                checks["model_reachable"] = True
        except (OSError, httpx.HTTPError, ValueError, KeyError) as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    comparisons = compare_requirements(observed, requirements)
    return {
        "schema_version": 1,
        "checked_at": time.time(),
        "launch_captured_at": manifest.get("captured_at"),
        "recipe": harness,
        "runner_id": state.get("runner_id"),
        "observed_at_launch": observed,
        "live_checks": checks,
        "ready_for_smoke": all(checks.values()),
        "requirements": comparisons,
        "requirements_status": (
            "not_assessed"
            if not comparisons
            else "mismatch"
            if any(c["status"] == "mismatch" for c in comparisons)
            else "unknown"
            if any(c["status"] == "unknown" for c in comparisons)
            else "match"
        ),
        "errors": errors,
        "limitations": [
            "Readiness does not prove a real turn, journey fidelity, or a reported bug.",
            "Launch metadata is a snapshot; changed files or binaries require a fresh runtime.",
            "CLI versions are installed prerequisites, not evidence of a session's process.",
            "UI asset hash does not establish which commit built the SPA.",
            "Diff hash covers tracked changes only; untracked code is not identified by it.",
            "Surface, session configuration, and starting state need journey-specific evidence.",
            "Mock providers cannot verify live-provider behavior.",
            "model_backend describes the prepared service; session routing is unverified.",
            "Requirements are caller-supplied; completeness needs independent review.",
        ],
    }


def doctor(output: Path, harness: str, requirements: list[dict], destination: Path) -> int:
    report = inspect_environment(output, harness, requirements)
    write_json(destination, report)
    print(json.dumps(report, indent=2), flush=True)
    return (
        0
        if report["ready_for_smoke"] and report["requirements_status"] in ("match", "not_assessed")
        else 1
    )
