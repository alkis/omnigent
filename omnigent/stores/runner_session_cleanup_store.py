"""Durable delivery of explicit session teardown, independent of session lookup."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select, tuple_, update
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlRunnerSessionCleanup, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)


@dataclass(frozen=True)
class RunnerSessionCleanup:
    """An authorized teardown command addressed to its original runner."""

    command_id: str
    runner_id: str
    session_id: str
    delete_completed: bool = False


class RunnerSessionCleanupStore:
    """Persist commands before cleanup; acknowledge only successful runner DELETEs."""

    def __init__(self, storage_location: str) -> None:
        self._sessions = make_named_managed_session_maker(
            get_or_create_engine(storage_location),
            query_name_prefix="omnigent.runner_session_cleanup",
            immediate=True,
        )

    def enqueue(self, targets: list[tuple[str, str]]) -> list[RunnerSessionCleanup]:
        """Persist authorized (runner_id, session_id) pairs in the current workspace."""
        commands = [RunnerSessionCleanup(uuid.uuid4().hex, runner, sid) for runner, sid in targets]

        def write(session: Session) -> None:
            for command in commands:
                session.add(
                    SqlRunnerSessionCleanup(
                        command_id=command.command_id,
                        runner_id=command.runner_id,
                        session_id=command.session_id,
                    )
                )

        run_write_transaction(self._sessions, "enqueue_teardown", write)
        return commands

    def pending(
        self, runner_id: str, *, after: str = "", limit: int = 100
    ) -> list[RunnerSessionCleanup]:
        """Read a bounded page for this workspace and runner, without session lookups."""
        with self._sessions("list_pending_teardown") as session:
            query = select(SqlRunnerSessionCleanup).where(
                SqlRunnerSessionCleanup.workspace_id == current_workspace_id(),
                SqlRunnerSessionCleanup.runner_id == runner_id,
            )
            if after:
                query = query.where(SqlRunnerSessionCleanup.command_id > after)
            rows = session.scalars(query.order_by(SqlRunnerSessionCleanup.command_id).limit(limit))
            return [
                RunnerSessionCleanup(
                    row.command_id, row.runner_id, row.session_id, row.delete_completed
                )
                for row in rows
            ]

    def complete(self, command_ids: list[str]) -> None:
        """Allow acknowledgement once server-side deletion has completed."""

        def write(session: Session) -> None:
            targets = session.execute(
                select(
                    SqlRunnerSessionCleanup.runner_id, SqlRunnerSessionCleanup.session_id
                ).where(
                    SqlRunnerSessionCleanup.workspace_id == current_workspace_id(),
                    SqlRunnerSessionCleanup.command_id.in_(command_ids),
                )
            ).all()
            # Retrying an interrupted DELETE also completes its older commands.
            session.execute(
                update(SqlRunnerSessionCleanup)
                .where(
                    SqlRunnerSessionCleanup.workspace_id == current_workspace_id(),
                    tuple_(
                        SqlRunnerSessionCleanup.runner_id, SqlRunnerSessionCleanup.session_id
                    ).in_(targets),
                )
                .values(delete_completed=True)
            )

        run_write_transaction(self._sessions, "complete_session_deletion", write)

    def acknowledge(self, command_id: str) -> None:
        """Remove precisely the delivered command, preserving concurrent requests."""

        def write(session: Session) -> None:
            session.execute(
                delete(SqlRunnerSessionCleanup).where(
                    SqlRunnerSessionCleanup.workspace_id == current_workspace_id(),
                    SqlRunnerSessionCleanup.command_id == command_id,
                    SqlRunnerSessionCleanup.delete_completed.is_(True),
                )
            )

        run_write_transaction(self._sessions, "acknowledge_teardown", write)

    def discard(self, command_ids: list[str]) -> None:
        """Forget this request's commands when deletion is explicitly rejected."""

        def write(session: Session) -> None:
            session.execute(
                delete(SqlRunnerSessionCleanup).where(
                    SqlRunnerSessionCleanup.workspace_id == current_workspace_id(),
                    SqlRunnerSessionCleanup.command_id.in_(command_ids),
                )
            )

        run_write_transaction(self._sessions, "discard_rejected_deletion", write)
