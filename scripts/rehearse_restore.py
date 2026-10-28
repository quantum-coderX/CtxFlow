"""Rehearse and validate a PostgreSQL memory backup/restore.

The default mode creates a backup and validates its archive without changing
any database. A destructive restore requires ``--execute-restore`` and a
separate target dotenv file.

Examples:
    python scripts/rehearse_restore.py --archive .artifacts/ctxflow.dump
    python scripts/rehearse_restore.py --archive .artifacts/ctxflow.dump \
        --execute-restore --restore-dotenv .env.rehearsal
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
from typing import Any

from ctxflow.memory.runtime import DatabaseConfig, load_database_config
try:
    from scripts.backup_restore import (
        backup_database,
        restore_database,
        _run_postgres_tool,
    )
except ModuleNotFoundError:
    from backup_restore import (  # type: ignore[no-redef]
        backup_database,
        restore_database,
        _run_postgres_tool,
    )


def rehearse(
    source_config: DatabaseConfig,
    archive: Path,
    *,
    restore_config: DatabaseConfig | None = None,
    execute_restore: bool = False,
    dump_executable: str = "pg_dump",
    restore_executable: str = "pg_restore",
) -> dict[str, Any]:
    """Create and validate an archive, optionally restoring to a safe target."""

    if execute_restore and restore_config is None:
        raise ValueError("restore_config is required when execute_restore is true")
    if (
        execute_restore
        and restore_config is not None
        and (
            restore_config.host,
            restore_config.port,
            restore_config.database,
        )
        == (source_config.host, source_config.port, source_config.database)
    ):
        raise ValueError("restore target must differ from the source database")

    backup_database(source_config, archive, executable=dump_executable)
    _run_postgres_tool(
        restore_executable,
        ("--list", str(archive)),
        source_config,
    )
    restored = False
    if execute_restore:
        assert restore_config is not None
        restore_database(
            restore_config,
            archive,
            executable=restore_executable,
        )
        restored = True
    return {
        "archive": str(archive),
        "archive_bytes": archive.stat().st_size,
        "archive_valid": True,
        "restore_executed": restored,
        "source_database": source_config.database,
        "restore_database": restore_config.database if restored and restore_config else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dotenv", type=Path, default=Path(".env"))
    parser.add_argument("--restore-dotenv", type=Path)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--execute-restore", action="store_true")
    args = parser.parse_args()

    try:
        source_config = load_database_config(args.source_dotenv)
        restore_config = (
            load_database_config(args.restore_dotenv)
            if args.restore_dotenv is not None
            else None
        )
        report = rehearse(
            source_config,
            args.archive,
            restore_config=restore_config,
            execute_restore=args.execute_restore,
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
