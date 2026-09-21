from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("select-impacted-e2e.py")
FIXTURE = Path(__file__).parent / "fixtures/impacted-e2e/index.json"
SPEC = importlib.util.spec_from_file_location("select_impacted_e2e", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
selector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(selector)


@pytest.fixture
def coverage_index() -> dict[str, object]:
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    source = tmp_path / "web/src/components"
    source.mkdir(parents=True)
    (source / "Direct.tsx").write_text("export const Direct = () => null;\n")
    (source / "Leaf.ts").write_text("export const leaf = true;\n")
    (source / "Middle.ts").write_text("export { leaf } from './Leaf';\n")
    (source / "Consumer.tsx").write_text("import { leaf } from '@/components/Middle';\n")
    (source / "New.ts").write_text("export const uncovered = true;\n")
    return tmp_path


def select(index: dict[str, object], changed: list[str], repo: Path) -> dict[str, object]:
    return selector.select_impacted_tests(payload=index, changed_files=changed, repo_root=repo)


def test_direct_coverage_match(coverage_index: dict[str, object], repo: Path) -> None:
    result = select(coverage_index, ["web/src/components/Direct.tsx"], repo)
    assert result["mode"] == "selected"
    assert result["selected_node_ids"] == ["tests/e2e_ui/chat/test_direct.py::test_direct"]


def test_transitive_consumer_match(coverage_index: dict[str, object], repo: Path) -> None:
    result = select(coverage_index, ["web/src/components/Leaf.ts"], repo)
    assert result["mode"] == "selected"
    assert result["confidence"] == "medium"
    assert result["selected_node_ids"] == ["tests/e2e_ui/chat/test_consumer.py::test_consumer"]


@pytest.mark.parametrize(
    ("changed", "reason"),
    [
        (["web/src/components/New.ts"], "unresolved-frontend-change"),
        (["tests/e2e_ui/conftest.py"], "test-or-coverage-infrastructure-changed"),
        (["omnigent/server/routes/sessions.py"], "api-route-coverage-unavailable"),
        (["uv.lock"], "test-or-coverage-infrastructure-changed"),
    ],
)
def test_full_suite_fallbacks(
    coverage_index: dict[str, object], repo: Path, changed: list[str], reason: str
) -> None:
    result = select(coverage_index, changed, repo)
    assert result["mode"] == "full"
    assert reason in result["reasons"]


def test_shared_css_selects_all_snapshots(coverage_index: dict[str, object], repo: Path) -> None:
    result = select(coverage_index, ["web/src/index.css"], repo)
    assert result["mode"] == "snapshots"
    assert result["reasons"] == ["shared-css-requires-all-snapshots"]


def test_unrelated_documentation_skips_fast_lane(
    coverage_index: dict[str, object], repo: Path
) -> None:
    result = select(coverage_index, ["docs/architecture.md"], repo)
    assert result["mode"] == "none"
    assert result["confidence"] == "high"


def test_empty_changed_files_fail_open(coverage_index: dict[str, object], repo: Path) -> None:
    result = select(coverage_index, [], repo)
    assert result["mode"] == "full"
    assert result["reasons"] == ["changed-files-empty"]


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({"complete": False}, "index-incomplete"),
        ({"schema_version": 2}, "index-schema-mismatch"),
        ({"repository_sha": "stale"}, "index-sha-not-ancestor"),
    ],
)
def test_invalid_indexes_fail_open(
    coverage_index: dict[str, object], mutation: dict[str, object], reason: str
) -> None:
    coverage_index.update(mutation)
    with pytest.raises(selector.IndexErrorReason, match=reason):
        selector.validate_index(
            coverage_index,
            now=datetime(2026, 9, 21, tzinfo=UTC),
            max_age=timedelta(days=14),
            is_ancestor=lambda sha: sha == "fixture-sha",
        )


def test_stale_index_age_fails_open(coverage_index: dict[str, object]) -> None:
    with pytest.raises(selector.IndexErrorReason, match="index-stale-by-age"):
        selector.validate_index(
            coverage_index,
            now=datetime(2026, 10, 21, tzinfo=UTC),
            max_age=timedelta(days=14),
            is_ancestor=lambda _sha: True,
        )
