"""Back up or restore the PostgreSQL memory database.

Examples:
    python scripts/backup_restore.py backup --output ctxflow.dump
    python scripts/backup_restore.py restore --input ctxflow.dump

Restore is explicit because it can replace existing database objects.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
from typing import Sequence

from ctxflow.memory.runtime import DatabaseConfig, load_database_config


def _command_environment(config: DatabaseConfig) -> dict[str, str]:
    environment = os.environ.copy()
    environment["PGPASSWORD"] = config.password
    return environment


def _run_postgres_tool(
    executable: str,
    arguments: Sequence[str],
    config: DatabaseConfig,
) -> None:
    subprocess.run(
        [executable, *arguments],
        check=True,
        env=_command_environment(config),
    )


def backup_database(
    config: DatabaseConfig,
    output: Path,
    *,
    executable: str = "pg_dump",
) -> None:
    """Create a custom-format PostgreSQL backup."""

    if output.exists() and output.is_dir():
        raise ValueError("backup output must be a file path")
    output.parent.mkdir(parents=True, exist_ok=True)
    _run_postgres_tool(
        executable,
        (
            "--format=custom",
            f"--file={output}",
            f"--host={config.host}",
            f"--port={config.port}",
            f"--username={config.user}",
            f"--dbname={config.database}",
        ),
        config,
    )


def restore_database(
    config: DatabaseConfig,
    input_path: Path,
    *,
    executable: str = "pg_restore",
) -> None:
    """Restore a custom-format backup, replacing objects from that backup."""

    if not input_path.is_file():
        raise ValueError(f"backup file does not exist: {input_path}")
    _run_postgres_tool(
        executable,
        (
            "--clean",
            "--if-exists",
            "--no-owner",
            f"--host={config.host}",
            f"--port={config.port}",
            f"--username={config.user}",
            f"--dbname={config.database}",
            str(input_path),
        ),
        config,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("backup", "restore"))
    parser.add_argument("--dotenv", type=Path, default=Path(".env"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--input", type=Path)
    args = parser.parse_args()

    try:
        config = load_database_config(args.dotenv)
        if args.operation == "backup":
            if args.output is None:
                raise ValueError("--output is required for backup")
            backup_database(config, args.output)
        else:
            if args.input is None:
                raise ValueError("--input is required for restore")
            restore_database(config, args.input)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
