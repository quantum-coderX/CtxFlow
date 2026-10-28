"""Retention maintenance for append-only memory run events."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


@dataclass(frozen=True)
class RetentionPolicy:
    """Retention windows for durable operational records, in whole days."""

    run_events_days: int = 90

    def __post_init__(self) -> None:
        if self.run_events_days < 1:
            raise ValueError("run_events_days must be at least 1")


async def purge_run_events(pool: Any, policy: RetentionPolicy | None = None) -> int:
    """Delete run events older than the configured retention window.

    The operation is intentionally separate from request-time retrieval. The
    current schema has no checkpoint table, so run events are the only
    retention-managed records.
    """

    active_policy = policy or RetentionPolicy()
    status = await pool.execute(
        """DELETE FROM run_events
        WHERE created_at < NOW() - ($1 * INTERVAL '1 day')""",
        active_policy.run_events_days,
    )
    match = re.fullmatch(r"DELETE\s+(\d+)", status.strip(), re.IGNORECASE)
    return int(match.group(1)) if match else 0
