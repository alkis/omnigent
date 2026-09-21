import importlib.util
import sys
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("e2e_failure_clusters.py")
SPEC = importlib.util.spec_from_file_location("e2e_failure_clusters", MODULE_PATH)
assert SPEC and SPEC.loader
clusters = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = clusters
SPEC.loader.exec_module(clusters)


def write_junit(tmp_path, cases):
    body = "".join(cases)
    path = tmp_path / "junit.xml"
    path.write_text(f'<testsuites><testsuite name="e2e">{body}</testsuite></testsuites>')
    return path


def case(name, child=""):
    return f'<testcase classname="tests.e2e.test_demo" name="{name}">{child}</testcase>'


def failure(text, *, tag="failure", phase="call"):
    return f'<{tag} phase="{phase}" message="failed"><![CDATA[{text}]]></{tag}>'


def test_shared_fixture_outage_clusters_affected_tests(tmp_path):
    shared = "ERROR at setup of test_x\nConnectionError: fixture api_server unavailable"
    report = clusters.analyze([write_junit(tmp_path, [
        case("test_one", failure(shared, tag="error", phase="setup")),
        case("test_two", failure(shared, tag="error", phase="setup")),
    ])], [])

    cluster = report["clusters"][0]
    assert cluster["category"] == "setup_fixture_failure"
    assert cluster["affected_test_count"] == 2
    assert len(cluster["representative_tests"]) == 2


def test_identical_assertions_share_one_cluster(tmp_path):
    assertion = "tests/e2e/test_demo.py:42: in verify\nAssertionError: expected ready, got pending"
    report = clusters.analyze([write_junit(tmp_path, [
        case("test_one", failure(assertion)),
        case("test_two", failure(assertion)),
    ])], [])

    assert len(report["clusters"]) == 1
    assert report["clusters"][0]["category"] == "assertion_failure"
    assert report["clusters"][0]["common_meaningful_frames"] == ["tests/e2e/test_demo.py:verify"]


def test_retry_only_pass_is_not_deterministic_failure(tmp_path):
    report = clusters.analyze([write_junit(tmp_path, [
        case("test_flaky", failure("TimeoutError: transient", tag="rerun")),
        case("test_flaky"),
    ])], [])

    assert report["clusters"] == []
    assert report["summary"]["retry_only_passes"] == 1
    assert report["retry_only_passes"][0]["attempts"] == 2


def test_deterministic_retry_failure_remains_a_cluster(tmp_path):
    report = clusters.analyze([write_junit(tmp_path, [
        case("test_broken", failure("RuntimeError: server process exited", tag="rerun")),
        case("test_broken", failure("RuntimeError: server process exited", tag="failure")),
    ])], [])

    assert report["summary"]["deterministic_failed_tests"] == 1
    assert report["clusters"][0]["category"] == "server_bootstrap_failure"
    assert report["clusters"][0]["attempt_count"] == 2


def test_stale_snapshot_mismatch_is_separate(tmp_path):
    report = clusters.analyze([write_junit(tmp_path, [
        case("test_visual", failure("AssertionError: snapshot baseline mismatch")),
    ])], [])

    assert report["clusters"][0]["category"] == "stale_assertion_or_snapshot"


def test_unrelated_failures_form_separate_clusters(tmp_path):
    report = clusters.analyze([write_junit(tmp_path, [
        case("test_assertion", failure("AssertionError: expected 1 got 2")),
        case("test_exception", failure("ValueError: malformed payload")),
    ])], [])

    assert len(report["clusters"]) == 2
    assert {item["category"] for item in report["clusters"]} == {
        "assertion_failure",
        "exception_failure",
    }


def test_bootstrap_log_creates_infrastructure_cluster(tmp_path):
    log = tmp_path / "server.log"
    log.write_text("health check failed: connection refused on port 6767\n")

    report = clusters.analyze([], [log])

    assert report["clusters"][0]["category"] == "server_bootstrap_failure"
    assert report["clusters"][0]["affected_test_count"] == 0
