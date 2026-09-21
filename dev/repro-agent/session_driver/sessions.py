"""Reusable session operations over the production HTTP API, without pytest.

Evidence is scoped to a caller-named journey and a baseline captured before
input. Receipts are diagnostic artifacts, not a tamper-proof attestation.
"""

from __future__ import annotations

import io
import json
import re
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

import httpx
from filelock import FileLock

HARNESSES = ("claude-native", "codex-native", "openai-agents")


def write_json(path: Path, value: dict) -> None:
    """Publish complete state, including when another tool process reads it."""
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")
        temp = Path(handle.name)
    temp.replace(path)


def item_text(item: dict) -> str:
    return " ".join(
        block["text"]
        for block in item.get("content", [])
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )


def session_bundle(harness: str, workspace: Path, models: dict[str, str]) -> tuple[bytes, dict]:
    """Use the same terminal definitions as the native product launchers."""
    if harness not in HARNESSES:
        raise ValueError(f"unsupported harness: {harness}")
    labels = {}
    if harness == "openai-agents":
        name = "config.yaml"
        text = json.dumps(
            {
                "spec_version": 1,
                "name": "repro-session",
                "instructions": "Follow the user's instructions.",
                "executor": {
                    "type": "omnigent",
                    "model": models[harness],
                    "config": {"harness": harness},
                },
            }
        )
    else:
        from omnigent._wrapper_labels import (
            CLAUDE_NATIVE_WRAPPER_VALUE,
            CODEX_NATIVE_WRAPPER_VALUE,
            UI_MODE_LABEL_KEY,
            UI_MODE_TERMINAL_VALUE,
            WRAPPER_LABEL_KEY,
        )

        with tempfile.TemporaryDirectory() as directory:
            if harness == "claude-native":
                from omnigent.harnesses.claude_native.main import (
                    _materialize_claude_agent_spec,
                )

                spec = _materialize_claude_agent_spec(Path(directory))
                wrapper = CLAUDE_NATIVE_WRAPPER_VALUE
            else:
                from omnigent.harnesses.codex_native.main import (
                    _materialize_codex_agent_spec,
                )

                spec = _materialize_codex_agent_spec(Path(directory), model=models[harness])
                wrapper = CODEX_NATIVE_WRAPPER_VALUE
            text = spec.read_text()
        # Native launcher specs use the compat parser (not strict config.yaml).
        name = f"{harness}.yaml"
        labels = {UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE, WRAPPER_LABEL_KEY: wrapper}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        data = text.encode()
        info = tarfile.TarInfo(name)
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return buf.getvalue(), {"labels": labels, "workspace": str(workspace)}


class SessionDriver:
    """One local environment; every operation targets a session it created."""

    def __init__(self, environment: Path, *, transport=None):
        self.path = environment.resolve()
        self.state = json.loads(self.path.read_text())
        if self.state["status"] != "ready" or self.state["model_backend"] != "mock":
            raise ValueError("expected a ready, isolated mock environment")
        self.output = self.path.parent
        self.http = httpx.Client(
            base_url=self.state["base_url"],
            timeout=10,
            trust_env=False,
            transport=transport,
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.http.close()

    def _request(self, method: str, path: str, **kwargs):
        result = self.http.request(method, path, **kwargs)
        result.raise_for_status()
        return result.json() if result.content else {}

    def _file(self, session_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
            raise ValueError("invalid session id")
        return self.output / f"session-{session_id}.json"

    def _load(self, session_id: str) -> dict:
        value = json.loads(self._file(session_id).read_text())
        if value["attempt_id"] != self.state["attempt_id"] or value["closed"]:
            raise ValueError("session is closed or belongs to another environment")
        return value

    def items(self, session_id: str) -> list[dict]:
        """Read every page in transcript order; never truncate long journeys."""
        items = []
        params = {"order": "asc", "limit": 100}
        seen = set()
        while True:
            page = self._request("GET", f"/v1/sessions/{session_id}/items", params=params)
            items.extend(page["data"])
            if not page.get("has_more"):
                return items
            cursor = page.get("last_id")
            if not cursor or cursor in seen:
                raise ValueError("transcript pagination did not advance")
            seen.add(cursor)
            params["after"] = cursor

    def start(self, harness: str, journey: str) -> dict:
        if not journey.strip():
            raise ValueError("name the actual journey being exercised")
        bundle, metadata = session_bundle(
            harness, Path(self.state["workspace"]), self.state["models"]
        )
        created = self._request(
            "POST",
            "/v1/sessions",
            timeout=30,
            data={"metadata": json.dumps(metadata)},
            files={"bundle": ("session.tar.gz", bundle, "application/gzip")},
        )
        session_id = created["session_id"]
        record = {
            "attempt_id": self.state["attempt_id"],
            "session_id": session_id,
            "harness": harness,
            "model_backend": "mock",
            "journey": journey,
            "test_nodeid": self.state.get("test_nodeid"),
            "browser_url": f"{self.state['base_url']}/c/{session_id}",
            "turns": [],
            "closed": False,
        }
        write_json(self._file(session_id), record)
        try:
            self._request(
                "PATCH",
                f"/v1/sessions/{session_id}",
                json={"runner_id": self.state["runner_id"]},
            )
        except Exception:
            self.close(session_id)
            raise
        return record

    def _begin(self, session_id: str, prompt: str, reply: str, surface: str) -> dict:
        if not prompt.strip() or not reply.strip() or reply in prompt:
            raise ValueError("supply a prompt and a distinct scripted reply absent from it")
        record = self._load(session_id)
        if record["turns"] and record["turns"][-1]["status"] == "pending":
            raise ValueError("wait for or close the pending turn before starting another")
        snapshot = self._request("GET", f"/v1/sessions/{session_id}")
        if snapshot["status"] != "idle":
            raise ValueError(f"session is not idle: {snapshot['status']}")
        baseline = [item["id"] for item in self.items(session_id)]
        # The mock routes by model, so allow one active turn per environment.
        for path in self.output.glob("session-*.json"):
            other = json.loads(path.read_text())
            if (
                not other["closed"]
                and other["turns"]
                and other["turns"][-1]["status"] == "pending"
            ):
                raise ValueError(
                    "another turn is pending; use separate environments for parallel journeys"
                )
        for key in ("default", self.state["models"][record["harness"]]):
            self._request(
                "POST",
                f"{self.state['mock_url']}/mock/set_fallback",
                json={"key": key, "text": reply},
            )
        turn = {
            "turn_id": uuid.uuid4().hex,
            "status": "pending",
            "surface": surface,
            "prompt": prompt,
            "expected_reply": reply,
            "baseline_ids": baseline,
        }
        record["turns"].append(turn)
        write_json(self._file(session_id), record)
        return turn

    def begin(self, session_id: str, prompt: str, reply: str) -> dict:
        """Capture a baseline BEFORE the caller submits this prompt in the UI."""
        with FileLock(str(self.output / "turn.lock")):
            return self._begin(session_id, prompt, reply, "browser")

    def send(self, session_id: str, prompt: str, reply: str) -> dict:
        with FileLock(str(self.output / "turn.lock")):
            self._begin(session_id, prompt, reply, "api")
            # Never automatically retry a POST: a lost ACK may still mean delivery.
            ack = self._request(
                "POST",
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": prompt}],
                    },
                },
            )
            record = self._load(session_id)
            record["turns"][-1]["ack_item_id"] = ack.get("item_id")
            write_json(self._file(session_id), record)
            return record["turns"][-1]

    def wait(self, session_id: str, turn_id: str, timeout: float = 30) -> dict:
        if not 0 <= timeout <= 60:
            raise ValueError("timeout must be between 0 and 60 seconds; retry pending waits")
        record = self._load(session_id)
        turn = next((t for t in record["turns"] if t["turn_id"] == turn_id), None)
        if turn is None:
            raise ValueError("unknown turn for this session")
        if turn["status"] != "pending":
            return turn
        deadline = time.monotonic() + timeout
        while True:
            fresh = [i for i in self.items(session_id) if i["id"] not in turn["baseline_ids"]]
            snapshot = self._request("GET", f"/v1/sessions/{session_id}")
            users = [i for i in fresh if i.get("role") == "user"]
            matching = [i for i in users if item_text(i) == turn["prompt"]]
            error = None
            if snapshot["status"] == "failed":
                error = snapshot.get("last_task_error") or "session failed"
            elif len(users) > 1 or (users and not matching):
                error = "concurrent or unrelated input; cannot attribute this turn"
            elif matching and turn.get("ack_item_id") and matching[0]["id"] != turn["ack_item_id"]:
                error = "observed user item differs from the send acknowledgement"
            assistants = [
                i
                for i in fresh
                if i.get("role") == "assistant"
                and i.get("status") == "completed"
                and turn["expected_reply"] in item_text(i)
            ]
            if error:
                turn.update(status="failed", error=error)
            elif (
                len(matching) == 1
                and assistants
                and snapshot["status"] == "idle"
                and fresh.index(matching[0]) < fresh.index(assistants[0])
            ):
                turn.update(
                    status="completed",
                    user=matching[0],
                    assistants=assistants,
                    observed_at=time.time(),
                )
            if turn["status"] != "pending":
                # Merge under the same lock used by send/close; don't overwrite other state.
                with FileLock(str(self.output / "turn.lock")):
                    latest = self._load(session_id)
                    for i, value in enumerate(latest["turns"]):
                        if value["turn_id"] == turn_id:
                            latest["turns"][i] = turn
                    write_json(self._file(session_id), latest)
                return turn
            if time.monotonic() >= deadline:
                return {
                    **turn,
                    "detail": "no completed turn observed; wait again or inspect logs",
                }
            time.sleep(0.25)

    def inspect(self, session_id: str) -> dict:
        return {
            "evidence": self._load(session_id),
            "snapshot": self._request("GET", f"/v1/sessions/{session_id}"),
            "items": self.items(session_id),
            "logs": self.state.get("logs", str(self.output)),
        }

    def close(self, session_id: str) -> dict:
        with FileLock(str(self.output / "turn.lock")):
            record = json.loads(self._file(session_id).read_text())
            if record["attempt_id"] != self.state["attempt_id"]:
                raise ValueError("session belongs to another environment")
            if record["closed"]:
                return record
            response = self.http.delete(f"/v1/sessions/{session_id}")
            if response.status_code != 404:
                response.raise_for_status()
            record["closed"] = True
            for turn in record["turns"]:
                if turn["status"] == "pending":
                    turn.update(status="cancelled", error="session closed before completion")
            write_json(self._file(session_id), record)
            return record
