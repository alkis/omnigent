"""Replay explicit cleanup after reconnect without interpreting ordinary 404s."""

from __future__ import annotations

import asyncio
import logging

import httpx

from omnigent.db.workspace_cache import WorkspaceScopedCache
from omnigent.errors import OmnigentError
from omnigent.runner.routing import RunnerRouter
from omnigent.stores.runner_session_cleanup_store import RunnerSessionCleanupStore

_logger = logging.getLogger(__name__)


class RunnerSessionCleanup:
    """Retry the journal on the replica holding the runner's authenticated tunnel."""

    def __init__(self, store: RunnerSessionCleanupStore, router: RunnerRouter) -> None:
        self.store = store
        self._router = router
        self._tasks: WorkspaceScopedCache[str, asyncio.Task[None]] = WorkspaceScopedCache()
        self._all_tasks: set[asyncio.Task[None]] = set()

    async def replay(self, runner_id: str) -> bool:
        """Drain successful commands; leave every failed delivery pending."""
        after = ""
        failed = False
        while True:
            commands = await asyncio.to_thread(self.store.pending, runner_id, after=after)
            if not commands:
                return not failed
            for command in commands:
                try:
                    client = self._router.client_for_cleanup(runner_id)
                    response = await client.delete(
                        f"/v1/sessions/{command.session_id}", timeout=60.0
                    )
                    response.raise_for_status()
                    await asyncio.to_thread(self.store.acknowledge, command.command_id)
                except (httpx.HTTPError, ConnectionError, OmnigentError):
                    failed = True
                    _logger.warning(
                        "Runner session cleanup deferred: runner=%s session=%s",
                        runner_id,
                        command.session_id,
                    )
            after = commands[-1].command_id

    async def pending_sessions(self, runner_id: str) -> set[str]:
        """Sessions being deleted must not be reinitialized during reconnect."""
        result: set[str] = set()
        after = ""
        while commands := await asyncio.to_thread(self.store.pending, runner_id, after=after):
            result.update(command.session_id for command in commands)
            after = commands[-1].command_id
        return result

    def retry(self, runner_id: str) -> None:
        """Retry transient delivery failures while this replica owns the tunnel."""
        task = self._tasks.get(runner_id)
        if task is not None and not task.done():
            return

        async def run() -> None:
            try:
                while self._router.runner_is_online(runner_id):
                    try:
                        if await self.replay(runner_id):
                            return
                    except Exception:
                        _logger.exception(
                            "Runner session cleanup retry failed: runner=%s", runner_id
                        )
                    await asyncio.sleep(30)
            finally:
                self._tasks.pop(runner_id, None)

        task = asyncio.create_task(run(), name=f"runner-cleanup-{runner_id}")
        self._tasks[runner_id] = task
        self._all_tasks.add(task)
        task.add_done_callback(self._all_tasks.discard)

    async def shutdown(self) -> None:
        """Cancel background delivery; undelivered commands remain in the database."""
        tasks = list(self._all_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
