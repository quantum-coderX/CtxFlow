"""In-process revisions used by clients to detect project graph changes."""

from collections import defaultdict
from threading import Lock

_lock = Lock()
_revisions: dict[str, int] = defaultdict(int)


def bump_graph_revision(project_id: str) -> int:
    """Mark a project graph as changed and return its new revision."""
    with _lock:
        _revisions[project_id] += 1
        return _revisions[project_id]


def get_graph_revision(project_id: str) -> int:
    """Return the latest revision observed by this API process."""
    with _lock:
        return _revisions[project_id]
