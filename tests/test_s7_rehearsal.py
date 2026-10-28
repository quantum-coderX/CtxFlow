import importlib.util
from pathlib import Path

import pytest

from ctxflow.memory import DatabaseConfig


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "rehearse_restore.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("rehearse_restore", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rehearsal_validates_archive_without_restore(tmp_path, monkeypatch):
    module = _load_module()
    source = DatabaseConfig("source-host", 5432, "source-db", "user", "secret")
    archive = tmp_path / "memory.dump"
    calls = []

    def fake_backup(_config, output, *, executable):
        calls.append(("backup", executable))
        output.write_bytes(b"valid archive")

    def fake_tool(executable, arguments, config):
        calls.append(("validate", executable, arguments, config.database))

    monkeypatch.setattr(module, "backup_database", fake_backup)
    monkeypatch.setattr(module, "_run_postgres_tool", fake_tool)

    report = module.rehearse(source, archive)

    assert report["archive_valid"] is True
    assert report["restore_executed"] is False
    assert calls == [
        ("backup", "pg_dump"),
        ("validate", "pg_restore", ("--list", str(archive)), "source-db"),
    ]


def test_rehearsal_requires_distinct_target_for_restore(tmp_path):
    module = _load_module()
    source = DatabaseConfig("host", 5432, "db", "user", "secret")
    with pytest.raises(ValueError, match="restore_config"):
        module.rehearse(
            source,
            tmp_path / "memory.dump",
            execute_restore=True,
        )

    with pytest.raises(ValueError, match="target must differ"):
        module.rehearse(
            source,
            tmp_path / "memory.dump",
            restore_config=source,
            execute_restore=True,
        )


def test_rehearsal_executes_restore_only_when_requested(tmp_path, monkeypatch):
    module = _load_module()
    source = DatabaseConfig("source", 5432, "source-db", "user", "secret")
    target = DatabaseConfig("target", 5432, "staging-db", "user", "secret")
    archive = tmp_path / "memory.dump"
    calls = []

    def fake_backup(_config, output, *, executable):
        output.write_bytes(b"valid archive")

    monkeypatch.setattr(module, "backup_database", fake_backup)
    monkeypatch.setattr(
        module,
        "_run_postgres_tool",
        lambda executable, arguments, config: calls.append(
            ("validate", executable, config.database)
        ),
    )
    monkeypatch.setattr(
        module,
        "restore_database",
        lambda config, input_path, *, executable: calls.append(
            ("restore", executable, config.database, str(input_path))
        ),
    )

    report = module.rehearse(
        source,
        archive,
        restore_config=target,
        execute_restore=True,
    )

    assert report["restore_executed"] is True
    assert calls == [
        ("validate", "pg_restore", "source-db"),
        ("restore", "pg_restore", "staging-db", str(archive)),
    ]
