"""Check the 24-hour runner idle default and explicit timeout overrides.

These configuration checks do not wait for an overnight idle period."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.runner._entry import _load_runner_idle_timeout_s_from_config

# An idle window must span a full overnight gap: evening to next morning.
_ONE_DAY_S = 24 * 60 * 60


def test_default_runner_idle_timeout_survives_overnight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Missing configuration selects the 24-hour default."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))

    timeout_s = _load_runner_idle_timeout_s_from_config()

    assert timeout_s == float(_ONE_DAY_S), (
        f"runner idle-timeout default is {timeout_s:.0f}s "
        f"({timeout_s / 3600:.1f}h); the intended default is exactly "
        f"{_ONE_DAY_S}s (24h) — long enough to survive overnight, not unbounded"
    )


def test_default_runner_idle_timeout_survives_overnight_with_empty_runner_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An empty runner section selects the same 24-hour default."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("runner: {}\n", encoding="utf-8")

    timeout_s = _load_runner_idle_timeout_s_from_config()

    assert timeout_s == float(_ONE_DAY_S), (
        f"runner idle-timeout default is {timeout_s:.0f}s "
        f"({timeout_s / 3600:.1f}h); the intended default is exactly "
        f"{_ONE_DAY_S}s (24h) — long enough to survive overnight, not unbounded"
    )


def test_explicit_idle_timeout_still_wins_over_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An explicitly configured timeout overrides the default."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "runner:\n  idle_timeout_s: 900\n",
        encoding="utf-8",
    )

    assert _load_runner_idle_timeout_s_from_config() == 900.0
