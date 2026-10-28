import importlib.util
from pathlib import Path

import pytest

from ctxflow.memory import DatabaseConfig, InMemoryMemoryStore, Memory


ROOT = Path(__file__).parents[1]
MIGRATION = ROOT / "migrations" / "005_memory_scope_and_run_events.sql"
SCRIPT = ROOT / "scripts" / "backup_restore.py"


def _load_backup_module():
    spec = importlib.util.spec_from_file_location("backup_restore", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scope_migration_is_idempotent_by_construction():
    sql = MIGRATION.read_text(encoding="utf-8").upper()
    assert "ADD COLUMN IF NOT EXISTS" in sql
    assert "CREATE INDEX IF NOT EXISTS" in sql
    assert "CREATE TABLE IF NOT EXISTS" in sql
    assert "DROP " not in sql


def test_memory_store_isolation_fails_closed_for_reads_and_search():
    import asyncio

    async def run():
        store = InMemoryMemoryStore()
        await store.add(Memory(id="a", project_id="project-a", text="shared finding"))
        await store.add(Memory(id="b", project_id="project-b", text="shared finding"))

        assert await store.get("b", project_id="project-a") is None
        assert [item.memory_id for item in await store.search(
            None, "shared", 10, project_id="project-a"
        )] == ["a"]
        assert (await store.neighbors("b", project_id="project-a", depth=1)).nodes == []

    asyncio.run(run())


def test_backup_sets_password_outside_command_and_restore_requires_file(tmp_path):
    module = _load_backup_module()
    config = DatabaseConfig("db.example", 5432, "ctxflow", "backup", "secret")
    calls = []

    def fake_run(command, *, check, env):
        calls.append((command, check, env))

    module.subprocess.run = fake_run
    output = tmp_path / "ctxflow.dump"
    module.backup_database(config, output)

    assert calls[0][0][0] == "pg_dump"
    assert "--format=custom" in calls[0][0]
    assert calls[0][2]["PGPASSWORD"] == "secret"
    assert "secret" not in " ".join(calls[0][0])

    with pytest.raises(ValueError, match="does not exist"):
        module.restore_database(config, tmp_path / "missing.dump")


def test_restore_invokes_pg_restore_with_clean_and_no_owner(tmp_path):
    module = _load_backup_module()
    config = DatabaseConfig("db.example", 5432, "ctxflow", "backup", "secret")
    calls = []
    module.subprocess.run = lambda command, **kwargs: calls.append((command, kwargs))
    backup = tmp_path / "ctxflow.dump"
    backup.write_bytes(b"backup")

    module.restore_database(config, backup)

    assert calls[0][0][0] == "pg_restore"
    assert "--clean" in calls[0][0]
    assert "--if-exists" in calls[0][0]
    assert "--no-owner" in calls[0][0]
    assert str(backup) == calls[0][0][-1]
