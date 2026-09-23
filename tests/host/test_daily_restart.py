"""Tests for the daily host restart clock (config, timezone, and re-exec)."""

from __future__ import annotations

import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from omnigent.host import daily_restart
from omnigent.host.daily_restart import (
    daily_restart_enabled,
    next_daily_restart,
    reexec_host_process,
    resolve_user_timezone,
)
from omnigent.host.identity_env import HOST_ID_ENV_VAR

NY = ZoneInfo("America/New_York")
KOLKATA = ZoneInfo("Asia/Kolkata")


# ── next_daily_restart ───────────────────────────────


def test_next_daily_restart_same_day_when_before_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """A now before 04:00 local yields the same day's 04:00."""
    now = datetime(2026, 1, 5, 2, 0, 0, tzinfo=NY)
    not_before = datetime(2026, 1, 4, 0, 0, 0, tzinfo=NY)

    result = next_daily_restart(now=now, not_before=not_before, tz=NY)

    assert result == datetime(2026, 1, 5, 4, 0, 0, tzinfo=NY)


def test_next_daily_restart_next_day_when_past_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """A now after 04:00 local rolls over to the next day's 04:00."""
    now = datetime(2026, 1, 5, 5, 0, 0, tzinfo=NY)
    not_before = datetime(2026, 1, 4, 0, 0, 0, tzinfo=NY)

    result = next_daily_restart(now=now, not_before=not_before, tz=NY)

    assert result == datetime(2026, 1, 6, 4, 0, 0, tzinfo=NY)


def test_next_daily_restart_exactly_at_target_returns_now() -> None:
    """now == target instant satisfies >=, so it is returned as-is."""
    now = datetime(2026, 1, 5, 4, 0, 0, tzinfo=NY)
    not_before = datetime(2026, 1, 4, 0, 0, 0, tzinfo=NY)

    result = next_daily_restart(now=now, not_before=not_before, tz=NY)

    assert result == now


def test_next_daily_restart_not_before_wins_when_later() -> None:
    """not_before later than now (e.g. min-uptime not reached) pushes to the next day."""
    now = datetime(2026, 1, 5, 3, 30, 0, tzinfo=NY)
    not_before = datetime(2026, 1, 5, 4, 30, 0, tzinfo=NY)

    result = next_daily_restart(now=now, not_before=not_before, tz=NY)

    assert result == datetime(2026, 1, 6, 4, 0, 0, tzinfo=NY)


def test_next_daily_restart_spring_forward() -> None:
    """DST start (America/New_York, 2026-03-08): result stays pinned at local 04:00."""
    now = datetime(2026, 3, 7, 5, 0, 0, tzinfo=NY)
    not_before = now

    result = next_daily_restart(now=now, not_before=not_before, tz=NY)

    expected = datetime(2026, 3, 8, 4, 0, 0, tzinfo=NY)
    assert result == expected
    assert (result.hour, result.minute, result.second) == (4, 0, 0)
    # Spring-forward day is 23 hours long; landing an hour earlier (04:00
    # instead of 05:00) trims one more hour off the elapsed wall-clock span.
    assert result.timestamp() - now.timestamp() == 22 * 3600


def test_next_daily_restart_fall_back() -> None:
    """DST end (America/New_York, 2026-11-01): result stays pinned at local 04:00."""
    now = datetime(2026, 10, 31, 5, 0, 0, tzinfo=NY)
    not_before = now

    result = next_daily_restart(now=now, not_before=not_before, tz=NY)

    expected = datetime(2026, 11, 1, 4, 0, 0, tzinfo=NY)
    assert result == expected
    assert (result.hour, result.minute, result.second) == (4, 0, 0)
    # Fall-back day is 25 hours long; landing an hour earlier trims one more
    # hour off the elapsed wall-clock span.
    assert result.timestamp() - now.timestamp() == 24 * 3600


def test_next_daily_restart_now_in_different_zone_than_target() -> None:
    """now given in UTC while tz is a +05:30 zone still lands on local 04:00."""
    now = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)  # 2026-06-01 05:30 IST
    not_before = now

    result = next_daily_restart(now=now, not_before=not_before, tz=KOLKATA)

    assert result.tzinfo is KOLKATA
    assert (result.year, result.month, result.day) == (2026, 6, 2)
    assert (result.hour, result.minute, result.second) == (4, 0, 0)
    assert result.timestamp() >= now.timestamp()


def test_next_daily_restart_rejects_naive_now() -> None:
    naive = datetime(2026, 1, 5, 2, 0, 0)  # noqa: DTZ001 - intentionally naive
    aware = datetime(2026, 1, 5, 2, 0, 0, tzinfo=NY)

    with pytest.raises(ValueError):
        next_daily_restart(now=naive, not_before=aware, tz=NY)


def test_next_daily_restart_rejects_naive_not_before() -> None:
    naive = datetime(2026, 1, 5, 2, 0, 0)  # noqa: DTZ001 - intentionally naive
    aware = datetime(2026, 1, 5, 2, 0, 0, tzinfo=NY)

    with pytest.raises(ValueError):
        next_daily_restart(now=aware, not_before=naive, tz=NY)


# ── daily_restart_enabled ───────────────────────────────


@pytest.fixture(autouse=True)
def _clear_host_id_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate every test from an ambient managed-sandbox environment."""
    monkeypatch.delenv(HOST_ID_ENV_VAR, raising=False)


def test_daily_restart_enabled_default_true_no_file(tmp_path: Path) -> None:
    assert daily_restart_enabled(config_path=tmp_path / "config.yaml") is True


def test_daily_restart_enabled_true_when_host_section_lacks_key(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"host": {"name": "my-laptop"}}))

    assert daily_restart_enabled(config_path=config_path) is True


@pytest.mark.parametrize("disabled_value", [False, "off", "OFF", "false", "0", 0])
def test_daily_restart_enabled_false_for_disable_values(
    tmp_path: Path, disabled_value: object
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"host": {"daily_restart": disabled_value}}))

    assert daily_restart_enabled(config_path=config_path) is False


def test_daily_restart_enabled_true_for_true(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"host": {"daily_restart": True}}))

    assert daily_restart_enabled(config_path=config_path) is True


def test_daily_restart_enabled_false_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daily_restart, "_is_windows", lambda: True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"host": {"daily_restart": True}}))

    assert daily_restart_enabled(config_path=config_path) is False


def test_daily_restart_enabled_false_for_managed_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(HOST_ID_ENV_VAR, "some-host-id")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"host": {"daily_restart": True}}))

    assert daily_restart_enabled(config_path=config_path) is False


def test_daily_restart_enabled_true_on_invalid_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("host: {daily_restart: [1, 2\n")

    assert daily_restart_enabled(config_path=config_path) is True


# ── resolve_user_timezone ───────────────────────────────


def test_resolve_user_timezone_uses_arca_when_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daily_restart, "_arca_timezone_name", lambda: "America/Los_Angeles")

    result = resolve_user_timezone()

    assert result == ZoneInfo("America/Los_Angeles")


def test_resolve_user_timezone_falls_through_on_invalid_arca_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daily_restart, "_arca_timezone_name", lambda: "Not/AZone")
    monkeypatch.setenv("TZ", "Europe/Berlin")

    result = resolve_user_timezone()

    assert result == ZoneInfo("Europe/Berlin")


def test_resolve_user_timezone_uses_localtime_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daily_restart, "_arca_timezone_name", lambda: None)
    monkeypatch.delenv("TZ", raising=False)
    link = tmp_path / "localtime"
    link.symlink_to(tmp_path / "usr" / "share" / "zoneinfo" / "Asia" / "Tokyo")
    monkeypatch.setattr(daily_restart, "_LOCALTIME", link)

    result = resolve_user_timezone()

    assert result == ZoneInfo("Asia/Tokyo")


def test_resolve_user_timezone_falls_back_to_fixed_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daily_restart, "_arca_timezone_name", lambda: None)
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(daily_restart, "_LOCALTIME", tmp_path / "nonexistent-localtime")

    result = resolve_user_timezone()

    assert result is not None


# ── _arca_timezone_name ───────────────────────────────


def test_arca_timezone_name_none_on_non_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daily_restart, "_is_linux", lambda: False)
    called = []
    monkeypatch.setattr(
        urllib.request, "build_opener", lambda *a, **k: called.append(True) or None
    )

    assert daily_restart._arca_timezone_name() is None
    assert called == []


def test_arca_timezone_name_none_when_vendor_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daily_restart, "_is_linux", lambda: True)
    monkeypatch.setattr(daily_restart, "_DMI_SYS_VENDOR", tmp_path / "does-not-exist")
    called = []
    monkeypatch.setattr(
        urllib.request, "build_opener", lambda *a, **k: called.append(True) or None
    )

    assert daily_restart._arca_timezone_name() is None
    assert called == []


def test_arca_timezone_name_none_when_vendor_not_ec2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daily_restart, "_is_linux", lambda: True)
    vendor_file = tmp_path / "sys_vendor"
    vendor_file.write_text("LENOVO\n")
    monkeypatch.setattr(daily_restart, "_DMI_SYS_VENDOR", vendor_file)
    called = []
    monkeypatch.setattr(
        urllib.request, "build_opener", lambda *a, **k: called.append(True) or None
    )

    assert daily_restart._arca_timezone_name() is None
    assert called == []


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


class _FakeOpener:
    """Records every request handed to ``.open`` and replays queued responses."""

    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.requests: list[urllib.request.Request] = []

    def open(self, request: urllib.request.Request, timeout: float | None = None) -> _FakeResponse:
        self.requests.append(request)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return _FakeResponse(response)


def _install_fake_opener(monkeypatch: pytest.MonkeyPatch, responses: list[object]) -> _FakeOpener:
    opener = _FakeOpener(responses)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **k: opener)
    return opener


def _ec2_vendor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daily_restart, "_is_linux", lambda: True)
    vendor_file = tmp_path / "sys_vendor"
    vendor_file.write_text("Amazon EC2\n")
    monkeypatch.setattr(daily_restart, "_DMI_SYS_VENDOR", vendor_file)


def test_arca_timezone_name_success_with_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ec2_vendor(tmp_path, monkeypatch)
    opener = _install_fake_opener(monkeypatch, [b"tok-123", b"America/Chicago\n"])

    result = daily_restart._arca_timezone_name()

    assert result == "America/Chicago"
    assert len(opener.requests) == 2
    token_request, tag_request = opener.requests
    assert token_request.get_method() == "PUT"
    assert tag_request.get_method() == "GET"
    assert tag_request.get_header("X-aws-ec2-metadata-token") == "tok-123"


def test_arca_timezone_name_succeeds_without_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed token PUT still allows a tokenless GET to succeed."""
    _ec2_vendor(tmp_path, monkeypatch)
    opener = _install_fake_opener(
        monkeypatch, [urllib.error.URLError("no route"), b"America/New_York"]
    )

    result = daily_restart._arca_timezone_name()

    assert result == "America/New_York"
    _token_request, tag_request = opener.requests
    assert tag_request.get_header("X-aws-ec2-metadata-token") is None


def test_arca_timezone_name_none_on_tag_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ec2_vendor(tmp_path, monkeypatch)
    not_found = urllib.error.HTTPError("http://169.254.169.254/x", 404, "Not Found", None, None)  # type: ignore[arg-type]
    _install_fake_opener(monkeypatch, [b"tok-123", not_found])

    assert daily_restart._arca_timezone_name() is None


# ── reexec_host_process ───────────────────────────────


class _Sentinel(Exception):
    """Raised by a fake execve to prove the real syscall was never reached."""


def test_reexec_host_process_builds_argv_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
        captured["path"] = path
        captured["argv"] = argv
        captured["env"] = env
        raise _Sentinel

    monkeypatch.setattr(os, "execve", fake_execve)
    monkeypatch.setattr(
        sys,
        "orig_argv",
        ["/usr/bin/python3", "-P", "-m", "omnigent.host._daemon_entry", "--local"],
    )
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3.12")

    with pytest.raises(_Sentinel):
        reexec_host_process({"OMNIGENT_SERVER": "https://example.databricksapps.com"})

    assert captured["path"] == "/usr/bin/python3.12"
    assert captured["argv"] == [
        "/usr/bin/python3.12",
        "-P",
        "-m",
        "omnigent.host._daemon_entry",
        "--local",
    ]
    env = captured["env"]
    assert env["OMNIGENT_SERVER"] == "https://example.databricksapps.com"
    assert env["OMNIGENT_HOST_NO_OPEN"] == "1"
    # Only the launch_env passed in (plus the no-open marker) is forwarded —
    # the live process environment is not merged in.
    assert set(env) == {"OMNIGENT_SERVER", "OMNIGENT_HOST_NO_OPEN"}


def test_reexec_host_process_exits_on_execve_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
        raise OSError("exec format error")

    monkeypatch.setattr(os, "execve", fake_execve)
    monkeypatch.setattr(
        sys, "orig_argv", ["/usr/bin/python3", "-m", "omnigent.host._daemon_entry"]
    )
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3.12")

    with pytest.raises(SystemExit) as excinfo:
        reexec_host_process({})

    assert excinfo.value.code == 1


# ── reexec_host_process: real subprocess re-exec ────────────────────────

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _subprocess_env(tmp_path: Path) -> dict[str, str]:
    """Build a subprocess environment isolated to *tmp_path*.

    Unsets ``OMNIGENT_HOST_NO_OPEN`` (so a script can tell a first run from
    a re-exec'd one) and points ``HOME``, the config home, and the data dir
    at *tmp_path* so identity/config/log/registry writes never escape it.
    Puts the repo root on ``PYTHONPATH`` so the script can ``import omnigent``.
    """
    env = dict(os.environ)
    env.pop("OMNIGENT_HOST_NO_OPEN", None)
    env["HOME"] = str(tmp_path)
    env["OMNIGENT_CONFIG_HOME"] = str(tmp_path / "config-home")
    env["OMNIGENT_DATA_DIR"] = str(tmp_path / "data-dir")
    existing_path = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{_REPO_ROOT}{os.pathsep}{existing_path}" if existing_path else str(_REPO_ROOT)
    )
    return env


def _parse_kv_line(line: str) -> dict[str, str]:
    """Parse a ``key=value key2=value2`` line printed by a test script."""
    return dict(field.split("=", 1) for field in line.split())


@pytest.mark.skipif(sys.platform == "win32", reason="os.execve does not keep the pid on Windows")
def test_reexec_host_process_real_exec_keeps_pid(tmp_path: Path) -> None:
    """A real ``os.execve`` (the syscall itself, not mocked) keeps the pid.

    A script re-execs itself once via :func:`reexec_host_process`, using a
    marker file in *tmp_path* to tell its first run from its second.
    """
    marker = tmp_path / "reexeced"
    script = tmp_path / "reexec_script.py"
    script.write_text(
        "\n".join(
            [
                "import os",
                "from pathlib import Path",
                "",
                "from omnigent.host.daily_restart import reexec_host_process",
                "",
                f"marker = Path({str(marker)!r})",
                'no_open = os.environ.get("OMNIGENT_HOST_NO_OPEN", "")',
                "if marker.exists():",
                '    extra = os.environ.get("EXTRA_MARK", "")',
                '    print(f"pid={os.getpid()} no_open={no_open} extra={extra}", flush=True)',
                "else:",
                '    marker.write_text("1")',
                '    print(f"pid={os.getpid()} no_open={no_open}", flush=True)',
                '    reexec_host_process({**os.environ, "EXTRA_MARK": "1"})',
                "",
            ]
        )
    )

    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        env=_subprocess_env(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 2, result.stdout
    first, second = (_parse_kv_line(line) for line in lines)
    assert first["pid"] == second["pid"]
    assert first["no_open"] == ""
    assert second["no_open"] == "1"
    assert second["extra"] == "1"


@pytest.mark.skipif(sys.platform == "win32", reason="os.execve does not keep the pid on Windows")
def test_daemon_entry_real_reexec_reclaims_lifecycle_lock(tmp_path: Path) -> None:
    """A real daily-restart re-exec lets the new process reclaim the flock.

    Drives ``_daemon_entry.main()`` end to end in a subprocess with a faked
    ``run_host_process`` (returns ``True`` once, then ``False``) so it never
    opens a real tunnel; everything else — identity load, the daemon
    record, the lifecycle lock, and the re-exec itself — runs for real. If
    the lock were still held after the re-exec, ``try_acquire()`` would
    return ``False`` and the second run would exit before ever calling the
    faked ``run_host_process``, so only one ``run pid=`` line would appear.
    """
    server_url = "https://server.example.invalid"
    run_marker = tmp_path / "ran-once"
    script = tmp_path / "daemon_entry_script.py"
    script.write_text(
        "\n".join(
            [
                "import os",
                "import sys",
                "from pathlib import Path",
                "",
                f"sys.argv = ['_daemon_entry', '--server', {server_url!r}]",
                f"run_marker = Path({str(run_marker)!r})",
                "",
                "import omnigent.host.connect as connect",
                "",
                "def _fake_run_host_process(**_kw):",
                '    print(f"run pid={os.getpid()}", flush=True)',
                "    if run_marker.exists():",
                "        return False",
                '    run_marker.write_text("1")',
                "    return True",
                "",
                "connect.run_host_process = _fake_run_host_process",
                "",
                "from omnigent.host import _daemon_entry",
                "",
                "_daemon_entry.main()",
                "",
            ]
        )
    )

    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        env=_subprocess_env(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    run_lines = [line for line in result.stdout.splitlines() if line.startswith("run pid=")]
    assert len(run_lines) == 2, result.stdout
    first_pid = run_lines[0].removeprefix("run pid=")
    second_pid = run_lines[1].removeprefix("run pid=")
    assert first_pid == second_pid
