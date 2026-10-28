"""Apply the configured retention policy to durable run events.

Usage:
    python scripts/retention.py --dotenv .env --run-events-days 90
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from ctxflow.memory.retention import RetentionPolicy, purge_run_events
from ctxflow.memory.runtime import create_pool


async def _run(dotenv_path: Path, policy: RetentionPolicy) -> int:
    pool = await create_pool(dotenv_path=dotenv_path)
    try:
        return await purge_run_events(pool, policy)
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dotenv", type=Path, default=Path(".env"))
    parser.add_argument("--run-events-days", type=int, default=90)
    args = parser.parse_args()
    try:
        deleted = asyncio.run(
            _run(args.dotenv, RetentionPolicy(run_events_days=args.run_events_days))
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(f"deleted_run_events={deleted}")


if __name__ == "__main__":
    main()
