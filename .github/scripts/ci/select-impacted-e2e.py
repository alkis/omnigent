#!/usr/bin/env python3
"""Select an additive E2E fast lane from generated frontend coverage."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_MAX_INDEX_AGE_DAYS = 14
FRONTEND_ROOT = "web/src/"
SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".css")
GLOBAL_CSS = {"web/src/index.css", "web/src/themePalettes.generated.css"}
IMPORT_RE = re.compile(
    r"(?:import\s*(?:[^'\"]*?\s+from\s*)?|export\s+[^'\"]*?\s+from\s*|import\s*\()"
    r"['\"]([^'\"]+)['\"]"
)

FULL_SUITE_PREFIXES = (
    ".github/actions/",
    ".github/scripts/ci/",
    ".github/workflows/",
    "tests/e2e_ui/",
)
FULL_SUITE_PATHS = {
    "package.json",
    "pnpm-lock.yaml",
    "pyproject.toml",
    "uv.lock",
    "web/package.json",
    "web/vite.config.ts",
    "web/vite.coverage.ts",
}
SAFE_PREFIXES = ("docs/", ".github/ISSUE_TEMPLATE/")
SAFE_SUFFIXES = (".md", ".rst", ".txt")


class IndexErrorReason(ValueError):
    """An unusable index that must trigger a full-suite fallback."""


def normalize_repo_path(raw_path: str) -> str:
    path = raw_path.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return PurePosixPath(path).as_posix()


def load_changed_files(path: Path) -> list[str]:
    return sorted({normalize_repo_path(line) for line in path.read_text().splitlines() if line})


def _parse_generated_at(value: Any) -> datetime:
    if not isinstance(value, str):
        raise IndexErrorReason("index-generated-at-missing")
    try:
        generated_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IndexErrorReason("index-generated-at-invalid") from exc
    if generated_at.tzinfo is None:
        raise IndexErrorReason("index-generated-at-unzoned")
    return generated_at.astimezone(UTC)


def validate_index(
    payload: Any,
    *,
    now: datetime,
    max_age: timedelta,
    is_ancestor: Callable[[str], bool],
) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise IndexErrorReason("index-not-object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise IndexErrorReason("index-schema-mismatch")
    if payload.get("complete") is not True or payload.get("capture_errors"):
        raise IndexErrorReason("index-incomplete")
    coverage = payload.get("coverage")
    if not isinstance(coverage, Mapping) or coverage.get("frontend_modules") is not True:
        raise IndexErrorReason("frontend-coverage-unavailable")
    if not isinstance(payload.get("tests"), Mapping) or not payload["tests"]:
        raise IndexErrorReason("index-tests-empty")
    if not isinstance(payload.get("modules"), Mapping) or not payload["modules"]:
        raise IndexErrorReason("index-modules-empty")
    shards = payload.get("shards")
    if not isinstance(shards, list) or not shards or not all(isinstance(item, str) for item in shards):
        raise IndexErrorReason("index-shards-missing")
    generated_at = _parse_generated_at(payload.get("generated_at"))
    if generated_at > now + timedelta(minutes=5) or now - generated_at > max_age:
        raise IndexErrorReason("index-stale-by-age")
    repository_sha = payload.get("repository_sha")
    if not isinstance(repository_sha, str) or not repository_sha or not is_ancestor(repository_sha):
        raise IndexErrorReason("index-sha-not-ancestor")
    return payload


def _resolve_import(importer: Path, specifier: str, repo_root: Path) -> str | None:
    if specifier.startswith("@/"):
        candidate = repo_root / "web/src" / specifier[2:]
    elif specifier.startswith("."):
        candidate = importer.parent / specifier
    else:
        return None
    candidates = [candidate]
    if not candidate.suffix:
        candidates.extend(candidate.with_suffix(suffix) for suffix in SOURCE_SUFFIXES)
        candidates.extend(candidate / f"index{suffix}" for suffix in SOURCE_SUFFIXES)
    for resolved in candidates:
        if resolved.is_file():
            return resolved.relative_to(repo_root).as_posix()
    return None


def build_reverse_dependencies(repo_root: Path) -> dict[str, set[str]]:
    reverse: dict[str, set[str]] = defaultdict(set)
    source_root = repo_root / "web/src"
    for importer in source_root.rglob("*"):
        if not importer.is_file() or importer.suffix not in SOURCE_SUFFIXES[:-1]:
            continue
        try:
            source = importer.read_text()
        except UnicodeDecodeError:
            continue
        importer_path = importer.relative_to(repo_root).as_posix()
        for specifier in IMPORT_RE.findall(source):
            imported = _resolve_import(importer, specifier, repo_root)
            if imported is not None:
                reverse[imported].add(importer_path)
    return reverse


def transitive_consumers(changed_path: str, reverse: Mapping[str, set[str]]) -> set[str]:
    consumers: set[str] = set()
    pending = deque([changed_path])
    while pending:
        current = pending.popleft()
        for consumer in reverse.get(current, set()):
            if consumer not in consumers:
                consumers.add(consumer)
                pending.append(consumer)
    return consumers


def _result(
    *,
    mode: str,
    confidence: str,
    reasons: Iterable[str],
    selected: Iterable[str],
    full_count: int,
    changed_files: Iterable[str],
    index_sha: str = "",
) -> dict[str, Any]:
    node_ids = sorted(set(selected))
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "confidence": confidence,
        "reasons": sorted(set(reasons)),
        "selected_node_ids": node_ids,
        "selected_count": len(node_ids),
        "full_count": full_count,
        "changed_files": sorted(set(changed_files)),
        "index_repository_sha": index_sha,
    }


def select_impacted_tests(
    *, payload: Mapping[str, Any], changed_files: list[str], repo_root: Path
) -> dict[str, Any]:
    tests = payload["tests"]
    modules = payload["modules"]
    full_count = len(tests)
    index_sha = str(payload.get("repository_sha", ""))

    if not changed_files:
        return _result(
            mode="full",
            confidence="none",
            reasons=["changed-files-empty"],
            selected=[],
            full_count=full_count,
            changed_files=changed_files,
            index_sha=index_sha,
        )

    if any(path in FULL_SUITE_PATHS or path.startswith(FULL_SUITE_PREFIXES) for path in changed_files):
        return _result(
            mode="full",
            confidence="none",
            reasons=["test-or-coverage-infrastructure-changed"],
            selected=[],
            full_count=full_count,
            changed_files=changed_files,
            index_sha=index_sha,
        )

    backend_changes = [
        path
        for path in changed_files
        if path.endswith(".py") and not path.startswith(("tests/", ".github/"))
    ]
    if backend_changes:
        return _result(
            mode="full",
            confidence="none",
            reasons=["api-route-coverage-unavailable"],
            selected=[],
            full_count=full_count,
            changed_files=changed_files,
            index_sha=index_sha,
        )

    css_changes = [path for path in changed_files if path.startswith(FRONTEND_ROOT) and path.endswith(".css")]
    if any(path in GLOBAL_CSS for path in css_changes):
        return _result(
            mode="snapshots",
            confidence="medium",
            reasons=["shared-css-requires-all-snapshots"],
            selected=[],
            full_count=full_count,
            changed_files=changed_files,
            index_sha=index_sha,
        )

    frontend_changes = [path for path in changed_files if path.startswith(FRONTEND_ROOT)]
    if not frontend_changes:
        if all(path.startswith(SAFE_PREFIXES) or path.endswith(SAFE_SUFFIXES) for path in changed_files):
            return _result(
                mode="none",
                confidence="high",
                reasons=["no-runtime-impact"],
                selected=[],
                full_count=full_count,
                changed_files=changed_files,
                index_sha=index_sha,
            )
        return _result(
            mode="full",
            confidence="none",
            reasons=["unclassified-change"],
            selected=[],
            full_count=full_count,
            changed_files=changed_files,
            index_sha=index_sha,
        )

    selected: set[str] = set()
    reasons: set[str] = set()
    unresolved: list[str] = []
    reverse = build_reverse_dependencies(repo_root)
    for path in frontend_changes:
        direct = modules.get(path, [])
        if isinstance(direct, list) and direct:
            selected.update(str(nodeid) for nodeid in direct)
            reasons.add("direct-coverage-match")
            continue
        if not (repo_root / path).is_file():
            unresolved.append(path)
            continue
        consumers = transitive_consumers(path, reverse)
        matched = {
            str(nodeid)
            for consumer in consumers
            for nodeid in modules.get(consumer, [])
            if isinstance(modules.get(consumer), list)
        }
        if matched:
            selected.update(matched)
            reasons.add("transitive-consumer-match")
        else:
            unresolved.append(path)

    if unresolved or not selected:
        return _result(
            mode="full",
            confidence="none",
            reasons=["unresolved-frontend-change", *unresolved],
            selected=[],
            full_count=full_count,
            changed_files=changed_files,
            index_sha=index_sha,
        )
    return _result(
        mode="selected",
        confidence="high" if reasons == {"direct-coverage-match"} else "medium",
        reasons=reasons,
        selected=selected,
        full_count=full_count,
        changed_files=changed_files,
        index_sha=index_sha,
    )


def _git_is_ancestor(repo_root: Path, repository_sha: str) -> bool:
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", repository_sha, "HEAD"],
            cwd=repo_root,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--changed-files", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--max-index-age-days", type=int, default=DEFAULT_MAX_INDEX_AGE_DAYS)
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    changed_files = load_changed_files(args.changed_files)
    try:
        payload = json.loads(args.index.read_text())
        index = validate_index(
            payload,
            now=datetime.now(UTC),
            max_age=timedelta(days=args.max_index_age_days),
            is_ancestor=lambda sha: _git_is_ancestor(repo_root, sha),
        )
        result = select_impacted_tests(payload=index, changed_files=changed_files, repo_root=repo_root)
    except (OSError, json.JSONDecodeError, IndexErrorReason) as exc:
        reason = str(exc) or exc.__class__.__name__
        result = _result(
            mode="full",
            confidence="none",
            reasons=[reason],
            selected=[],
            full_count=0,
            changed_files=changed_files,
        )
    write_json(args.output, result)


if __name__ == "__main__":
    main()
