"""Shared fixtures for host tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _no_arca_timezone_probe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the daily-restart loop off the EC2 instance-metadata service.

    Tests that drive the real ``run_host_process`` start the daily-restart
    loop, whose timezone lookup probes the metadata address on an EC2 box.
    Pointing the DMI vendor file at a missing path skips that probe; tests of
    the probe itself patch the path again.

    :param monkeypatch: Pytest monkeypatch fixture; restores the path at
        teardown.
    :param tmp_path: Per-test temp dir holding the (absent) vendor file.
    :returns: None.
    """
    monkeypatch.setattr("omnigent.host.daily_restart._DMI_SYS_VENDOR", tmp_path / "no-sys-vendor")
