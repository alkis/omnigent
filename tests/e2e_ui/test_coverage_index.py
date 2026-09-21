from __future__ import annotations

from pathlib import Path

from tests.e2e_ui.coverage_index import (
    build_shard_payload,
    merge_shard_payloads,
    normalize_browser_coverage,
    normalize_frontend_module_path,
)


def _coverage(*counts: int) -> dict[str, object]:
    return {"s": {str(index): count for index, count in enumerate(counts)}, "f": {}, "b": {}}


def test_normalize_frontend_module_path() -> None:
    repo_root = Path("/repo")

    assert (
        normalize_frontend_module_path("/@fs//repo/web/src/chat/App.tsx?v=123", repo_root)
        == "web/src/chat/App.tsx"
    )
    assert normalize_frontend_module_path("/repo/web/src/chat/App.test.tsx", repo_root) is None
    assert normalize_frontend_module_path("/repo/server/app.py", repo_root) is None


def test_normalize_browser_coverage_keeps_only_executed_modules() -> None:
    modules, error = normalize_browser_coverage(
        {
            "/repo/web/src/chat/App.tsx": _coverage(0, 2),
            "/repo/web/src/chat/Unused.tsx": _coverage(0, 0),
        },
        Path("/repo"),
    )

    assert modules == ["web/src/chat/App.tsx"]
    assert error is None


def test_normalize_browser_coverage_reports_missing_and_malformed() -> None:
    assert normalize_browser_coverage(None, Path("/repo")) == (
        [],
        "browser coverage was missing",
    )
    assert normalize_browser_coverage(["bad"], Path("/repo")) == (
        [],
        "browser coverage was not an object",
    )
    assert normalize_browser_coverage({"bad": []}, Path("/repo")) == (
        [],
        "browser coverage contained malformed entries",
    )


def test_merge_shards_unions_duplicate_nodeids_and_sorts_reverse_index() -> None:
    first = build_shard_payload(
        shard="1",
        repository_sha="abc",
        tests={
            "tests/e2e_ui/test_chat.py::test_chat": {
                "status": "captured",
                "frontend_modules": ["web/src/z.ts", "web/src/a.ts"],
            }
        },
    )
    second = build_shard_payload(
        shard="2",
        repository_sha="abc",
        tests={
            "tests/e2e_ui/test_chat.py::test_chat": {
                "status": "captured",
                "frontend_modules": ["web/src/a.ts", "web/src/b.ts"],
            }
        },
    )

    merged = merge_shard_payloads([second, first], ["1", "2"])

    assert merged["complete"] is True
    assert merged["shards"] == ["1", "2"]
    assert merged["tests"]["tests/e2e_ui/test_chat.py::test_chat"]["frontend_modules"] == [
        "web/src/a.ts",
        "web/src/b.ts",
        "web/src/z.ts",
    ]
    assert merged["modules"]["web/src/a.ts"] == [
        "tests/e2e_ui/test_chat.py::test_chat"
    ]


def test_merge_is_incomplete_for_missing_shards_and_capture_errors() -> None:
    payload = build_shard_payload(
        shard="1",
        repository_sha="abc",
        tests={
            "tests/e2e_ui/test_chat.py::test_chat": {
                "status": "missing",
                "frontend_modules": [],
                "message": "no coverage",
            }
        },
    )

    merged = merge_shard_payloads([payload], ["1", "2"])

    assert merged["complete"] is False
    assert {error["message"] for error in merged["capture_errors"]} == {
        "missing shard 2",
        "no coverage",
    }
