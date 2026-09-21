#!/usr/bin/env python3
"""Cluster E2E failures from pytest JUnit XML and diagnostic logs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

PATH_RE = re.compile(r"(?P<path>(?:[A-Za-z]:)?[^\s:\"]+\.py):(?P<line>\d+)(?::\s*in\s+(?P<func>[^\s]+))?")
ADDRESS_RE = re.compile(r"\b(?:0x[0-9a-f]+|\d{4,}|[0-9a-f]{8}-[0-9a-f-]{27,})\b", re.I)
SPACE_RE = re.compile(r"\s+")
PARAM_RE = re.compile(r"\[[^\]]+\]$")

STALE_PATTERNS = (
    "snapshot", "image mismatch", "received value does not match",
    "expected:", "actual:", "assert diff", "golden file", "baseline",
)
SETUP_PATTERNS = (
    "error at setup", "fixture", "conftest.py", "setup failed", "scope mismatch",
    "fixturelookup error", "failed to initialize fixture",
)
BOOTSTRAP_PATTERNS = (
    "connection refused", "failed to start server", "server failed to start", "health check failed",
    "address already in use", "timed out waiting for server", "bootstrap", "server process exited",
    "runner tunnel", "host daemon",
)


@dataclass
class Attempt:
    test_id: str
    outcome: str
    phase: str
    message: str
    text: str
    source: str
    index: int

    @property
    def evidence(self) -> str:
        return "\n".join(part for part in (self.message, self.text) if part).strip()


@dataclass
class Cluster:
    category: str
    signature: str
    frames: tuple[str, ...]
    tests: set[str] = field(default_factory=set)
    attempts: int = 0
    sources: set[str] = field(default_factory=set)
    examples: list[str] = field(default_factory=list)

    @property
    def cluster_id(self) -> str:
        raw = "\0".join((self.category, self.signature, *self.frames))
        return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _text(element: ET.Element | None) -> str:
    return "" if element is None else "".join(element.itertext()).strip()


def _test_id(case: ET.Element) -> str:
    classname = case.get("classname", "").strip()
    name = case.get("name", "unknown").strip()
    return f"{classname}::{name}" if classname else name


def parse_junit(path: Path) -> list[Attempt]:
    root = ET.parse(path).getroot()
    attempts: list[Attempt] = []
    for index, case in enumerate(root.iter("testcase")):
        children = list(case)
        outcome = "passed"
        phase = "call"
        result = None
        for child in children:
            tag = child.tag.rsplit("}", 1)[-1]
            if tag in {"failure", "error", "skipped", "rerun", "flaky"}:
                result = child
                outcome = "rerun" if tag in {"rerun", "flaky"} else tag
                phase = child.get("phase", child.get("when", "setup" if tag == "error" else "call"))
                break
        properties = {
            prop.get("name", ""): prop.get("value", "")
            for prop in case.findall("./properties/property")
        }
        property_outcome = properties.get("outcome", properties.get("result", "")).lower()
        if property_outcome in {"passed", "failure", "failed", "error", "rerun", "flaky"}:
            outcome = {"failed": "failure", "flaky": "rerun"}.get(property_outcome, property_outcome)
        attempts.append(
            Attempt(
                test_id=_test_id(case),
                outcome=outcome,
                phase=properties.get("phase", phase),
                message="" if result is None else result.get("message", ""),
                text=_text(result),
                source=str(path),
                index=index,
            )
        )
    return attempts


def normalize_signature(text: str) -> str:
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("Captured ", "During handling of the above")):
            continue
        if PATH_RE.search(line) and not any(token in line.lower() for token in ("error", "assert", "exception")):
            continue
        line = ADDRESS_RE.sub("<value>", line)
        line = re.sub(r"\b\d+(?:\.\d+)?s\b", "<duration>", line)
        line = re.sub(r"/tmp/[^\s]+", "/tmp/<path>", line)
        line = PARAM_RE.sub("[<param>]", line)
        line = SPACE_RE.sub(" ", line)
        lines.append(line[:500])
    if not lines:
        return "unknown failure"
    exception_lines = [line for line in lines if re.search(r"(?:Error|Exception|Failed|assert)", line, re.I)]
    return (exception_lines[-1] if exception_lines else lines[-1])[:500]


def meaningful_frames(text: str) -> tuple[str, ...]:
    frames = []
    for match in PATH_RE.finditer(text):
        path = match.group("path").replace("\\", "/")
        if "/site-packages/" in path or path.startswith("<"):
            continue
        marker = next((part for part in ("tests/", "omnigent/", ".github/") if part in path), None)
        if marker:
            path = marker + path.split(marker, 1)[1]
        frame = f"{path}:{match.group('func') or match.group('line')}"
        if frame not in frames:
            frames.append(frame)
    return tuple(frames[-3:])


def classify(attempt: Attempt) -> str:
    lower = attempt.evidence.lower()
    if any(pattern in lower for pattern in BOOTSTRAP_PATTERNS):
        return "server_bootstrap_failure"
    if attempt.phase in {"setup", "teardown"} or any(pattern in lower for pattern in SETUP_PATTERNS):
        return "setup_fixture_failure"
    if any(pattern in lower for pattern in STALE_PATTERNS):
        return "stale_assertion_or_snapshot"
    if "assertionerror" in lower or re.search(r"\bassert\b", lower):
        return "assertion_failure"
    return "exception_failure"


def _log_failures(paths: Iterable[Path]) -> list[Attempt]:
    attempts = []
    for path in paths:
        text = path.read_text(errors="replace")
        matching = [line.strip() for line in text.splitlines() if any(p in line.lower() for p in BOOTSTRAP_PATTERNS)]
        if matching:
            attempts.append(Attempt("<infrastructure>", "error", "setup", matching[0], "\n".join(matching[:20]), str(path), 0))
    return attempts


def analyze(junit_paths: Iterable[Path], log_paths: Iterable[Path]) -> dict:
    junit_paths = list(junit_paths)
    log_paths = list(log_paths)
    attempts = [attempt for path in junit_paths for attempt in parse_junit(path)]
    by_test: dict[str, list[Attempt]] = defaultdict(list)
    for attempt in attempts:
        by_test[attempt.test_id].append(attempt)

    clusters: dict[tuple[str, str, tuple[str, ...]], Cluster] = {}
    retry_only = []
    passed = []
    for test_id, history in sorted(by_test.items()):
        history.sort(key=lambda item: item.index)
        failed = [item for item in history if item.outcome in {"failure", "error", "rerun"}]
        final = history[-1]
        if final.outcome == "passed":
            if failed:
                retry_only.append({
                    "test": test_id,
                    "attempts": len(history),
                    "prior_signatures": sorted({normalize_signature(item.evidence) for item in failed}),
                })
            else:
                passed.append(test_id)
            continue
        if final.outcome == "skipped":
            continue
        evidence = final.evidence
        category = classify(final)
        signature = normalize_signature(evidence)
        frames = meaningful_frames(evidence)
        key = (category, signature, frames)
        cluster = clusters.setdefault(key, Cluster(category, signature, frames))
        cluster.tests.add(test_id)
        cluster.attempts += len(history)
        cluster.sources.update(item.source for item in history)
        if evidence and len(cluster.examples) < 3:
            cluster.examples.append(evidence[:1200])

    for attempt in _log_failures(log_paths):
        category = classify(attempt)
        signature = normalize_signature(attempt.evidence)
        frames = meaningful_frames(attempt.evidence)
        key = (category, signature, frames)
        cluster = clusters.setdefault(key, Cluster(category, signature, frames))
        cluster.sources.add(attempt.source)
        if len(cluster.examples) < 3:
            cluster.examples.append(attempt.evidence[:1200])

    rendered = []
    for cluster in clusters.values():
        tests = sorted(cluster.tests)
        rendered.append({
            "id": cluster.cluster_id,
            "category": cluster.category,
            "signature": cluster.signature,
            "common_meaningful_frames": list(cluster.frames),
            "affected_test_count": len(tests),
            "representative_tests": tests[:3],
            "attempt_count": cluster.attempts,
            "sources": sorted(cluster.sources),
            "evidence_examples": cluster.examples,
        })
    rendered.sort(key=lambda item: (-item["affected_test_count"], item["category"], item["id"]))
    deterministic_count = sum(item["affected_test_count"] for item in rendered)
    return {
        "schema_version": 1,
        "summary": {
            "junit_files": len(junit_paths),
            "log_files": len(log_paths),
            "deterministic_failed_tests": deterministic_count,
            "retry_only_passes": len(retry_only),
            "clusters": len(rendered),
        },
        "clusters": rendered,
        "retry_only_passes": retry_only,
        "passed_test_count": len(passed),
    }


def render_markdown(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "## E2E failure clusters",
        "",
        f"- **Deterministic failed tests:** {summary['deterministic_failed_tests']}",
        f"- **Root-cause clusters:** {summary['clusters']}",
        f"- **Retry-only passes / flakes:** {summary['retry_only_passes']}",
        "",
    ]
    if not report["clusters"]:
        lines.append("No deterministic E2E failures were found.")
    for cluster in report["clusters"]:
        lines.extend([
            f"### `{cluster['id']}` — {cluster['category'].replace('_', ' ')}",
            f"- **Affected tests:** {cluster['affected_test_count']}",
            f"- **Signature:** `{cluster['signature'].replace(chr(96), chr(39))}`",
        ])
        if cluster["representative_tests"]:
            lines.append("- **Representative:** " + ", ".join(f"`{test}`" for test in cluster["representative_tests"]))
        if cluster["common_meaningful_frames"]:
            lines.append("- **Frames:** " + ", ".join(f"`{frame}`" for frame in cluster["common_meaningful_frames"]))
        lines.append("")
    if report["retry_only_passes"]:
        lines.extend(["### Retry-only passes", ""])
        for item in report["retry_only_passes"][:10]:
            lines.append(f"- `{item['test']}` passed after {item['attempts']} attempts")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit", action="append", type=Path, default=[], help="JUnit XML path; repeatable")
    parser.add_argument("--log", action="append", type=Path, default=[], help="Diagnostic log path; repeatable")
    parser.add_argument("--json-output", required=True, type=Path)
    parser.add_argument("--markdown-output", required=True, type=Path)
    args = parser.parse_args()
    if not args.junit and not args.log:
        parser.error("at least one --junit or --log input is required")
    report = analyze(args.junit, args.log)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    args.markdown_output.write_text(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
