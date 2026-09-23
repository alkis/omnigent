"""Daily host process restart at 04:00 user-local time.

``omnigent host`` re-execs itself once a day so a long-running daemon picks up
updated dependencies and clears any slow leaks without the user noticing. The
target hour is interpreted in the user's local timezone: on an Arca dev box
that is the IANA zone the Arca launcher wrote into the ``launcher_zone_id`` EC2
instance tag; otherwise it is the machine's own local zone.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import NoReturn
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from omnigent.host.identity_env import HOST_ID_ENV_VAR

_logger = logging.getLogger(__name__)

DAILY_RESTART_HOUR = 4
# A busy host keeps waiting for idle until 08:00, then skips that day.
DAILY_RESTART_WINDOW_S = 4 * 3600
# A host younger than this at the target time waits for the next day.
DAILY_RESTART_MIN_UPTIME_S = 3600
# Key inside the `host:` section of config.yaml.
DAILY_RESTART_CONFIG_KEY = "daily_restart"

_IMDS_BASE = "http://169.254.169.254/latest"
_ARCA_TIMEZONE_TAG = "launcher_zone_id"
# IMDS answers in milliseconds when reachable. The probe runs in a worker
# thread that a stop can't cancel, so keep the worst case (two misses) short.
_IMDS_TIMEOUT_S = 0.5
_DMI_SYS_VENDOR = Path("/sys/class/dmi/id/sys_vendor")
_LOCALTIME = Path("/etc/localtime")

_DISABLE_STRINGS = {"false", "0", "no", "off"}


def _is_windows() -> bool:
    """Small seam so tests can monkeypatch platform detection safely.

    Patching ``os.name`` directly breaks ``pathlib``, which reads it at
    import time.
    """
    return os.name == "nt"


def _is_linux() -> bool:
    """Small seam so tests can monkeypatch platform detection."""
    return sys.platform.startswith("linux")


def daily_restart_enabled(config_path: Path | None = None) -> bool:
    """Whether the daily restart is enabled for this host process.

    :param config_path: Optional config YAML path, forwarded to
        :func:`omnigent.host.identity.host_config_path`. Defaults to the
        user-level path.
    :returns: ``False`` on Windows (re-exec is not same-PID there) and on a
        server-managed sandbox host (:data:`HOST_ID_ENV_VAR` set — the server
        owns its lifecycle). Otherwise ``True`` unless
        ``host.daily_restart`` in config.yaml is explicitly falsy. Never
        raises.
    """
    if _is_windows():
        return False
    if os.environ.get(HOST_ID_ENV_VAR) is not None:
        return False

    import yaml

    from omnigent.host.identity import host_config_path

    path = host_config_path(config_path)
    try:
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        _logger.debug("daily_restart_enabled: config unreadable at %s; defaulting to True", path)
        return True

    host_section = cfg.get("host")
    if not isinstance(host_section, dict):
        return True
    value = host_section.get(DAILY_RESTART_CONFIG_KEY)
    return not _is_disable_value(value)


def _is_disable_value(value: object) -> bool:
    """Whether a config value explicitly opts out of the daily restart."""
    if value is False:
        return True
    if isinstance(value, bool):
        return False  # True is handled above; bool is an int subclass, check first.
    if isinstance(value, int):
        return value == 0
    if isinstance(value, str):
        return value.strip().lower() in _DISABLE_STRINGS
    return False


def resolve_user_timezone() -> tzinfo:
    """Resolve the user's local timezone for the daily restart clock.

    Blocking (does a best-effort IMDS network call on Arca); callers should
    run this via ``asyncio.to_thread``.

    :returns: An Arca-tagged IANA zone when available and valid, else the
        machine's local IANA zone, else a fixed-offset zone derived from the
        system clock. Never raises.
    """
    arca_name = _arca_timezone_name()
    if arca_name:
        try:
            tz = ZoneInfo(arca_name)
        except (ZoneInfoNotFoundError, ValueError):
            _logger.warning("Arca timezone tag %r is not a valid IANA zone; ignoring", arca_name)
        else:
            _logger.info("Using Arca timezone %s", arca_name)
            return tz

    local_name = _local_timezone_name()
    if local_name:
        try:
            return ZoneInfo(local_name)
        except (ZoneInfoNotFoundError, ValueError):
            _logger.debug("Local timezone %r is not a valid IANA zone; falling back", local_name)

    fixed = datetime.now().astimezone().tzinfo
    return fixed if fixed is not None else timezone.utc


def _local_timezone_name() -> str | None:
    """Best-effort IANA zone name from ``$TZ`` or ``/etc/localtime``."""
    tz_env = os.environ.get("TZ")
    if tz_env:
        name = tz_env[1:] if tz_env.startswith(":") else tz_env
        if name:
            return name

    try:
        link = os.readlink(_LOCALTIME)
    except OSError:
        return None
    # The first hop usually names the zone; macOS may chain it through a
    # versioned tzdata directory, so fall back to the fully resolved path.
    marker = "zoneinfo/"
    for target in (link, os.path.realpath(_LOCALTIME)):
        idx = target.rfind(marker)
        if idx != -1:
            return target[idx + len(marker) :]
    return None


def _arca_timezone_name() -> str | None:
    """Read the user's IANA zone from the Arca ``launcher_zone_id`` EC2 tag.

    The Arca laptop CLI writes this tag on every ``arca start`` as a
    best-effort hint; it may be absent. Only probed on an EC2 instance
    (Linux with DMI vendor ``Amazon EC2``) so a laptop never dials the
    link-local metadata address.

    :returns: The tag value, or ``None`` when not on Arca, the tag is
        missing, or the request fails.
    """
    if not _is_linux():
        return None
    try:
        vendor = _DMI_SYS_VENDOR.read_text().strip()
    except OSError:
        return None
    if vendor != "Amazon EC2":
        return None

    token = _imds_request(
        "PUT",
        "/api/token",
        {"X-aws-ec2-metadata-token-ttl-seconds": "60"},
    )
    headers = {"X-aws-ec2-metadata-token": token} if token else {}
    tag = _imds_request("GET", f"/meta-data/tags/instance/{_ARCA_TIMEZONE_TAG}", headers)
    if tag is None:
        return None
    tag = tag.strip()
    return tag or None


def _imds_request(method: str, path: str, headers: Mapping[str, str]) -> str | None:
    """Issue one IMDS request, returning the decoded body or ``None``.

    Bypasses any configured HTTP(S) proxy — the metadata endpoint is
    link-local and a proxy would break the request.

    :param method: HTTP method (``"PUT"`` for the token, ``"GET"`` for tags).
    :param path: Path appended to :data:`_IMDS_BASE`.
    :param headers: Request headers.
    :returns: The response body decoded as UTF-8 and stripped, or ``None``
        on any error (missing tag, timeout, network error).
    """
    request = urllib.request.Request(_IMDS_BASE + path, headers=dict(headers), method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=_IMDS_TIMEOUT_S) as response:
            body = response.read().decode("utf-8").strip()
    except (urllib.error.URLError, OSError, TimeoutError):
        _logger.debug("IMDS request %s %s failed", method, path, exc_info=True)
        return None
    return body or None


def next_daily_restart(
    *, now: datetime, not_before: datetime, tz: tzinfo, hour: int = DAILY_RESTART_HOUR
) -> datetime:
    """Compute the next local ``hour:00:00`` instant at or after both inputs.

    :param now: Current time (timezone-aware, any zone).
    :param not_before: Earliest allowed instant (timezone-aware, any zone) —
        e.g. the host's own start time plus :data:`DAILY_RESTART_MIN_UPTIME_S`.
    :param tz: The user's local timezone to express the result in.
    :param hour: The local hour of day to target.
    :returns: The earliest datetime in *tz* whose local wall time is
        ``hour:00:00`` and whose instant is ``>= max(now, not_before)``.
    :raises ValueError: If *now* or *not_before* is naive.
    """
    if now.tzinfo is None or not_before.tzinfo is None:
        raise ValueError("now and not_before must be timezone-aware")

    # Compare timestamps (same-tzinfo datetimes compare by wall time), and
    # build candidates from dates so each stays at local hour:00 across DST.
    anchor = now if now.timestamp() >= not_before.timestamp() else not_before
    anchor_ts = anchor.timestamp()
    base_date = anchor.astimezone(tz).date()

    candidate = datetime.combine(base_date, time(hour), tzinfo=tz)
    if candidate.timestamp() >= anchor_ts:
        return candidate
    candidate = datetime.combine(base_date + timedelta(days=1), time(hour), tzinfo=tz)
    if candidate.timestamp() >= anchor_ts:
        return candidate
    # Safety net for pathological offsets (e.g. a huge fixed-offset zone);
    # two consecutive days should always suffice in practice.
    return datetime.combine(base_date + timedelta(days=2), time(hour), tzinfo=tz)


def reexec_host_process(launch_env: Mapping[str, str]) -> NoReturn:
    """Replace this process image with a fresh copy of the same command line.

    Keeps the same PID so supervisor process tracking (launchd/systemd) and
    the PID-based daemon registry stay valid across the restart.

    :param launch_env: Environment variables to launch with; merged with
        ``OMNIGENT_HOST_NO_OPEN=1`` so the re-exec never reopens a browser tab.
    :raises SystemExit: If ``os.execve`` fails, with code 1. A service
        manager relaunches the host; a plain background daemon is respawned
        by the next CLI command that needs it.
    """
    argv = [sys.executable, *sys.orig_argv[1:]]
    env = {**launch_env, "OMNIGENT_HOST_NO_OPEN": "1"}
    _logger.info("Daily restart: re-executing host process (%s)", " ".join(argv))

    # os.execve skips atexit handlers, so flush every background sender by
    # hand before replacing the process image.
    with contextlib.suppress(Exception):
        from omnigent.telemetry.client import get_client

        client = get_client()
        if client is not None:
            client.shutdown()
    with contextlib.suppress(Exception):
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush(timeout_millis=5000)
    with contextlib.suppress(Exception):
        for handler in logging.getLogger().handlers:
            handler.flush()
        sys.stdout.flush()
        sys.stderr.flush()

    try:
        os.execve(sys.executable, argv, env)
    except OSError:
        _logger.exception("Daily restart: exec failed; exiting so the host can be relaunched")
        raise SystemExit(1) from None
