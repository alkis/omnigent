"""Build and merge per-test frontend coverage indexes for E2E UI runs."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

SCHEMA_VERSION = 1
DEFAULT_OUTPUT_ROOT = Path("artifacts/e2e-ui-coverage")
_FRONTEND_ROOT = "web/src/"
_EXCLUDED_PARTS = {"node_modules", "storybook", "ai-elements"}


def normalize_frontend_module_path(raw_path: str, repo_root: Path) -> str | None:
    """Return a stable ``web/src/...`` path for an Istanbul coverage key."""
    path = unquote(urlsplit(raw_path).path).replace("\\", "/")
    if path.startswith("/@fs/"):
        path = path.removeprefix("/@fs")

    root = repo_root.resolve().as_posix().rstrip("/")
    if path.startswith(f"{root}/"):
        path = path[len(root) + 1 :]
    elif _FRONTEND_ROOT in path:
        path = path[path.index(_FRONTEND_ROOT) :]
    else:
        return None

    normalized = PurePosixPath(path).as_posix()
    if not normalized.startswith(_FRONTEND_ROOT):
        return None
    relative_parts = PurePosixPath(normalized).parts[2:]
    if any(part in _EXCLUDED_PARTS for part in relative_parts):
        return None
    if normalized.endswith((".d.ts", ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")):
        return None
    if ".stories." in normalized or normalized.endswith("/test-setup.ts"):
        return None
    return normalized


def _has_executed_counter(file_coverage: Mapping[str, Any]) -> bool:
    for counter_name in ("s", "f", "b"):
        counters = file_coverage.get(counter_name, {})
        if not isinstance(counters, Mapping):
            continue
        for value in counters.values():
            values = value if isinstance(value, list) else [value]
            if any(isinstance(count, int | float) and count > 0 for count in values):
                return True
    return False


def normalize_browser_coverage(raw: Any, repo_root: Path) -> tuple[list[str], str | None]:
    """Normalize ``window.__coverage__`` into executed frontend module paths."""
    if raw is None:
        return [], "browser coverage was missing"
    if not isinstance(raw, Mapping):
        return [], "browser coverage was not an object"

    modules: set[str] = set()
    malformed = False
    for raw_path, file_coverage in raw.items():
        if not isinstance(raw_path, str) or not isinstance(file_coverage, Mapping):
            malformed = True
            continue
        path = normalize_frontend_module_path(raw_path, repo_root)
        if path is not None and _has_executed_counter(file_coverage):
            modules.add(path)

    error = "browser coverage contained malformed entries" if malformed else None
    return sorted(modules), error


def build_shard_payload(
    *,
    shard: str,
    repository_sha: str,
    tests: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a deterministic per-shard payload from normalized test records."""
    normalized_tests: dict[str, dict[str, Any]] = {}
    capture_errors: list[dict[str, str]] = []
    for nodeid in sorted(tests):
        record = tests[nodeid]
        status = str(record.get("status", "missing"))
        modules = sorted(set(record.get("frontend_modules", [])))
        normalized_tests[nodeid] = {"status": status, "frontend_modules": modules}
        message = record.get("message")
        if status != "captured":
            capture_errors.append(
                {"nodeid": nodeid, "kind": status, "message": str(message or status)}
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "repository_sha": repository_sha,
        "coverage": {"frontend_modules": True, "api_routes": False, "css": False},
        "shard": shard,
        "complete": not capture_errors,
        "capture_errors": capture_errors,
        "tests": normalized_tests,
    }


def merge_shard_payloads(
    payloads: Iterable[Mapping[str, Any]], expected_shards: Iterable[str]
) -> dict[str, Any]:
    """Merge shard payloads into the stable frontend-module coverage index."""
    expected = sorted(set(expected_shards))
    by_shard = {str(payload.get("shard", "")): payload for payload in payloads}
    repository_shas = {str(payload.get("repository_sha", "")) for payload in by_shard.values()}
    merged_tests: dict[str, dict[str, Any]] = {}
    capture_errors: list[dict[str, str]] = []

    for shard in sorted(by_shard):
        payload = by_shard[shard]
        if payload.get("schema_version") != SCHEMA_VERSION:
            capture_errors.append(
                {"nodeid": "", "kind": "malformed", "message": f"shard {shard}: schema mismatch"}
            )
            continue
        for error in payload.get("capture_errors", []):
            if isinstance(error, Mapping):
                capture_errors.append(
                    {key: str(error.get(key, "")) for key in ("nodeid", "kind", "message")}
                )
        tests = payload.get("tests", {})
        if not isinstance(tests, Mapping):
            capture_errors.append(
                {
                    "nodeid": "",
                    "kind": "malformed",
                    "message": f"shard {shard}: tests was not an object",
                }
            )
            continue
        for nodeid, raw_record in tests.items():
            if not isinstance(nodeid, str) or not isinstance(raw_record, Mapping):
                continue
            current = merged_tests.setdefault(
                nodeid, {"status": "captured", "frontend_modules": set()}
            )
            current["frontend_modules"].update(raw_record.get("frontend_modules", []))
            if raw_record.get("status") != "captured":
                current["status"] = str(raw_record.get("status", "malformed"))

    missing_shards = sorted(set(expected) - set(by_shard))
    for shard in missing_shards:
        capture_errors.append(
            {"nodeid": "", "kind": "missing", "message": f"missing shard {shard}"}
        )
    if len(repository_shas) != 1:
        capture_errors.append(
            {"nodeid": "", "kind": "malformed", "message": "shards have different repository SHAs"}
        )

    tests_output = {
        nodeid: {
            "status": record["status"],
            "frontend_modules": sorted(record["frontend_modules"]),
        }
        for nodeid, record in sorted(merged_tests.items())
    }
    modules: dict[str, list[str]] = {}
    for nodeid, record in tests_output.items():
        for module in record["frontend_modules"]:
            modules.setdefault(module, []).append(nodeid)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "repository_sha": next(iter(repository_shas), ""),
        "coverage": {"frontend_modules": True, "api_routes": False, "css": False},
        "shards": sorted(by_shard),
        "complete": not capture_errors and sorted(by_shard) == expected,
        "capture_errors": sorted(
            capture_errors, key=lambda error: (error["nodeid"], error["kind"], error["message"])
        ),
        "tests": tests_output,
        "modules": {module: sorted(nodeids) for module, nodeids in sorted(modules.items())},
    }


def repository_sha(repo_root: Path) -> str:
    """Return the CI SHA or current git HEAD for freshness validation."""
    if sha := os.environ.get("GITHUB_SHA"):
        return sha
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, text=True
    ).strip()


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write stable, newline-terminated JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _merge_command(args: argparse.Namespace) -> None:
    payloads = [json.loads(path.read_text()) for path in sorted(args.input_dir.glob("*.json"))]
    output = merge_shard_payloads(payloads, args.expected_shards.split(","))
    write_json(args.output, output)


def main(argv: list[str] | None = None) -> None:
    """Run coverage-index helper commands."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(required=True)
    merge = subparsers.add_parser("merge")
    merge.add_argument("--input-dir", type=Path, required=True)
    merge.add_argument("--output", type=Path, required=True)
    merge.add_argument("--expected-shards", required=True)
    merge.set_defaults(handler=_merge_command)
    args = parser.parse_args(argv)
    args.handler(args)


if __name__ == "__main__":
    main()
