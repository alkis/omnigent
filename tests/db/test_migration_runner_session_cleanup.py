"""Exercise upgrading an existing database and rolling back the cleanup journal."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from omnigent.db.utils import _build_alembic_config


def test_cleanup_journal_upgrade_and_downgrade(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'cleanup.db'}"
    config = _build_alembic_config(uri)
    assert len(ScriptDirectory.from_config(config).get_heads()) == 1
    engine = sa.create_engine(uri)
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "ii1a2b3c4d5e")
            assert "runner_session_cleanup" not in sa.inspect(connection).get_table_names()
            command.upgrade(config, "head")
            columns = {
                column["name"]
                for column in sa.inspect(connection).get_columns("runner_session_cleanup")
            }
            assert columns == {
                "workspace_id",
                "command_id",
                "runner_id",
                "session_id",
                "delete_completed",
            }
            command.downgrade(config, "ii1a2b3c4d5e")
            assert "runner_session_cleanup" not in sa.inspect(connection).get_table_names()
            assert "omnigent_conversation_metadata" in sa.inspect(connection).get_table_names()
    finally:
        engine.dispose()
