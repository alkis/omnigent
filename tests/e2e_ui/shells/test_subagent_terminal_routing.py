"""A colocated child's shell uses its inherited workspace routing host."""

from __future__ import annotations

import os
import re
import signal
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from playwright.sync_api import Page, Route, WebSocket, expect

from tests.e2e_ui.conftest import fetch_with_retry, open_right_rail

_ROUTING_HOST = "host_parent_test"
_SLICE_HEADER = "x-databricks-omnigent-slice-key"


@pytest.fixture
def workspace_ui(live_server: str, tmp_path: Path) -> Iterator[str]:
    """Run the workspace frontend against the isolated local test server."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "OMNIGENT_URL": live_server,
        "VITE_DATABRICKS_WORKSPACE": "true",
    }
    env.pop("OMNIGENT_AUTH_TOKEN", None)
    log_path = tmp_path / "workspace-ui.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            ["pnpm", "exec", "vite", "--host", "127.0.0.1", "--port", str(port), "--strictPort"],
            cwd=Path(__file__).resolve().parents[3] / "web",
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail(f"Workspace UI exited: {log_path.read_text()}")
                try:
                    if httpx.get(base_url, timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                pytest.fail(f"Workspace UI did not start: {log_path.read_text()}")
            yield base_url
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)


def test_subagent_shell_uses_inherited_routing_host(
    page: Page,
    terminal_session: tuple[str, str],
    workspace_ui: str,
) -> None:
    """Exercise real shell launch/attach with a replica-affinity gate."""
    base_url, parent_id = terminal_session
    agent_response = httpx.get(f"{base_url}/v1/sessions/{parent_id}/agent", timeout=10)
    agent_response.raise_for_status()
    child_response = httpx.post(
        f"{base_url}/v1/sessions",
        json={
            "agent_id": agent_response.json()["id"],
            "parent_session_id": parent_id,
            "title": "Shell routing child",
        },
        timeout=10,
    )
    child_response.raise_for_status()
    child_id = child_response.json()["id"]
    terminal_keys: list[str | None] = []
    attach_urls: list[str] = []

    def snapshot_with_inherited_host(route: Route) -> None:
        response = fetch_with_retry(route)
        snapshot = response.json()
        assert snapshot["kind"] == "sub_agent"
        assert snapshot["host_id"] is None
        # Backend inheritance has API coverage; model its workspace response here.
        snapshot["routing_host_id"] = _ROUTING_HOST
        route.fulfill(response=response, json=snapshot)

    def require_routing_host(route: Route) -> None:
        key = route.request.headers.get(_SLICE_HEADER)
        terminal_keys.append(key)
        if key == _ROUTING_HOST:
            response = fetch_with_retry(route)
            payload = response.json()
            # Exercise the server relay, not the local runner's loopback shortcut.
            for terminal in payload.get("data", []):
                terminal.get("metadata", {}).pop("direct_attach_url", None)
            route.fulfill(response=response, json=payload)
        else:
            route.fulfill(
                status=503,
                json={"error": {"code": "runner_unavailable", "message": "Runner unavailable"}},
            )

    def capture_attach(ws: WebSocket) -> None:
        if f"/v1/sessions/{child_id}/resources/terminals/" in ws.url and "/attach" in ws.url:
            attach_urls.append(ws.url)

    page.route(re.compile(rf"/v1/sessions/{child_id}(\?|$)"), snapshot_with_inherited_host)
    page.route(
        re.compile(rf"/v1/sessions/{child_id}/resources/terminals(\?|/|$)"), require_routing_host
    )
    page.route_web_socket(re.compile(r"/v1/sessions/updates"), lambda ws: None)
    page.on("websocket", capture_attach)
    try:
        page.goto(f"{workspace_ui}/c/{child_id}")
        open_right_rail(page)
        rail = page.get_by_role("complementary", name="Workspace")
        rail.get_by_role("button", name="Open new").click()
        page.get_by_role("menuitem", name=re.compile("Shell")).click()
        terminal = rail.get_by_test_id("terminal-view").last
        expect(terminal).to_have_attribute("data-state", "connected", timeout=30_000)
        # A cold inventory read may precede the snapshot; later reads recover.
        assert terminal_keys and terminal_keys[-1] == _ROUTING_HOST
        assert attach_urls
        assert all(
            parse_qs(urlparse(url).query).get("omnigent_slice_key") == [_ROUTING_HOST]
            for url in attach_urls
        )
    finally:
        httpx.delete(f"{base_url}/v1/sessions/{child_id}", timeout=10).raise_for_status()
