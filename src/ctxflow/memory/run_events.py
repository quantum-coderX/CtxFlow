"""Append-only run-event hash-chain helpers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class RunEvent:
    run_id: str
    project_id: str
    sequence: int
    event_type: str
    event_data: dict[str, Any]
    previous_hash: str | None
    event_hash: str
    created_at: datetime


def event_hash(
    *,
    run_id: str,
    project_id: str,
    sequence: int,
    event_type: str,
    event_data: dict[str, Any],
    previous_hash: str | None,
) -> str:
    payload = {
        "run_id": run_id,
        "project_id": project_id,
        "sequence": sequence,
        "event_type": event_type,
        "event_data": event_data,
        "previous_hash": previous_hash,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def verify_event_chain(events: list[RunEvent]) -> bool:
    if not events:
        return True
    run_id = events[0].run_id
    project_id = events[0].project_id
    expected_previous: str | None = None
    for sequence, item in enumerate(events):
        if (
            item.run_id != run_id
            or item.project_id != project_id
            or item.sequence != sequence
            or item.previous_hash != expected_previous
        ):
            return False
        expected = event_hash(
            run_id=item.run_id,
            project_id=item.project_id,
            sequence=item.sequence,
            event_type=item.event_type,
            event_data=item.event_data,
            previous_hash=item.previous_hash,
        )
        if item.event_hash != expected:
            return False
        expected_previous = item.event_hash
    return True
