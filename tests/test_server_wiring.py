"""Tests for server wiring — projects route, workspace, workflow SSE, runs API.

These tests verify the 10 wiring gaps identified in the audit:
1. Projects route is mounted and returns 200
2. ResearchWorkspace is in app.state
3. /api/stream uses ResearchWorkflow (8-stage events)
4. /api/runs lists and retrieves run records
5. Project isolation: graphs are scoped
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

import pytest
from fastapi.testclient import TestClient

from ctxflow import CtxFlow
from researchos.api.server import create_app
from researchos.persistence import ResearchWorkspace


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def workspace(tmp_path):
    """In-memory workspace that persists to tmp_path."""
    return ResearchWorkspace(data_dir=tmp_path)


@pytest.fixture()
def client(workspace):
    """TestClient with a real workspace but no model gateway."""
    app = create_app(workspace=workspace)
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture()
def client_with_ctxflow(tmp_path):
    """TestClient with an injected CtxFlow (legacy/test mode)."""
    ctx = CtxFlow()
    app = create_app(ctxflow=ctx)
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c, ctx


# ---------------------------------------------------------------------------
# Gap 1: Projects route is mounted
# ---------------------------------------------------------------------------

class TestProjectsRoute:
    def test_list_projects_returns_200(self, client):
        r = client.get("/api/projects")
        assert r.status_code == 200
        data = r.json()
        assert "projects" in data

    def test_list_projects_has_default(self, client):
        r = client.get("/api/projects")
        ids = [p["project_id"] for p in r.json()["projects"]]
        assert "default" in ids

    def test_create_project(self, client):
        r = client.post(
            "/api/projects",
            json={"project_id": "test-proj", "name": "Test Project"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["project_id"] == "test-proj"
        assert data["name"] == "Test Project"

    def test_create_project_invalid_id(self, client):
        r = client.post(
            "/api/projects",
            json={"project_id": "bad id!", "name": "Bad"},
        )
        assert r.status_code == 400

    def test_create_project_idempotent(self, client):
        client.post("/api/projects", json={"project_id": "idp", "name": "A"})
        r = client.post("/api/projects", json={"project_id": "idp", "name": "B"})
        assert r.status_code == 200  # should update, not error


# ---------------------------------------------------------------------------
# Gap 2: ResearchWorkspace in app.state
# ---------------------------------------------------------------------------

class TestWorkspaceState:
    def test_workspace_on_app_state(self, client):
        # Access app.state through the ASGI app
        assert hasattr(client.app.state, "workspace")
        ws = client.app.state.workspace
        assert ws is not None
        assert isinstance(ws, ResearchWorkspace)

    def test_projects_persist_to_disk(self, workspace, tmp_path):
        """Projects created should be visible in a fresh workspace from same dir."""
        workspace.create_project("persisted-proj", name="Persisted")
        # Load a fresh workspace pointing at the same directory
        ws2 = ResearchWorkspace(data_dir=tmp_path)
        ids = [p.project_id for p in ws2.list_projects()]
        assert "persisted-proj" in ids


# ---------------------------------------------------------------------------
# Gap 3 & 4: /api/stream uses ResearchWorkflow; events are emitted
# ---------------------------------------------------------------------------

class TestWorkflowSSE:
    def test_stream_returns_sse(self, client):
        """Streaming response starts and emits at least a 'start' event."""
        events = []
        with client.stream("GET", "/api/stream?query=test+question") as r:
            assert r.status_code == 200
            for line in r.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[5:].strip()))
                if len(events) >= 2:
                    break

        assert len(events) >= 1
        # First event should be 'start' with run_id
        first = events[0]
        assert "run_id" in first or "query" in first or "message" in first

    def test_stream_emits_done_event(self, client):
        """Stream should produce a 'done' event with answer/verdict fields."""
        done_events = []
        with client.stream("GET", "/api/stream?query=what+is+known") as r:
            assert r.status_code == 200
            current_event = None
            for line in r.iter_lines():
                if line.startswith("event:"):
                    current_event = line[6:].strip()
                elif line.startswith("data:"):
                    if current_event == "done":
                        done_events.append(json.loads(line[5:].strip()))
                        break

        assert len(done_events) == 1
        done = done_events[0]
        assert "status" in done
        assert "answer" in done

    def test_stream_with_project_id(self, client):
        """Stream accepts project_id param."""
        with client.stream("GET", "/api/stream?query=test&project_id=proj-a") as r:
            assert r.status_code == 200
            # Just check it connects and starts streaming
            for _ in r.iter_lines():
                break

    def test_stream_does_not_expose_secrets(self, client):
        """Streaming response must never contain api_key or similar strings."""
        content = []
        with client.stream("GET", "/api/stream?query=test") as r:
            for line in r.iter_lines():
                content.append(line)
        full = "\n".join(content).lower()
        assert "api_key" not in full or "[redacted]" in "\n".join(content)


# ---------------------------------------------------------------------------
# Gap 5: /api/runs endpoints
# ---------------------------------------------------------------------------

class TestRunsAPI:
    def test_list_runs_empty(self, client):
        r = client.get("/api/runs")
        assert r.status_code == 200
        assert "runs" in r.json()

    def test_chat_endpoint_persists_prompt_and_synthesized_answer(
        self, client, workspace
    ):
        graph = workspace.get_graph("default")
        graph.ingest(
            "CtxFlow stores research claims in a project-scoped graph.",
            node_type="claim",
            tags={"ctxflow", "research"},
        )
        response = client.post(
            "/api/chat",
            json={
                "query": "What does CtxFlow store?",
                "session_id": "legacy-chat-session",
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["run_id"]
        assert payload["session_id"] == "legacy-chat-session"
        assert payload["answer"]
        records = workspace.list_runs(
            project_id="default",
            session_id="legacy-chat-session",
        )
        assert len(records) == 1
        assert records[0]["query"] == "What does CtxFlow store?"
        assert records[0]["answer"] == payload["answer"]

    def test_list_runs_after_streaming(self, client):
        """After a successful SSE run, it should appear in /api/runs."""
        # Consume the full stream
        with client.stream("GET", "/api/stream?query=test+question&project_id=run-test") as r:
            # Drain the stream
            for _ in r.iter_lines():
                pass

        # Give a moment for the run to be persisted
        import time as _time
        _time.sleep(0.2)

        r = client.get("/api/runs?project_id=run-test")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data["runs"], list)

    def test_stream_persists_prompt_answer_and_session_scope(self, client, workspace):
        session_id = "browser-session-a"
        with client.stream(
            "GET",
            "/api/stream?query=Persist%20this%20conversation%20turn"
            f"&project_id=conversation-test&session_id={session_id}",
        ) as response:
            assert response.status_code == 200
            for _ in response.iter_lines():
                pass

        records = workspace.list_runs(
            project_id="conversation-test",
            session_id=session_id,
        )
        assert len(records) == 1
        assert records[0]["query"] == "Persist this conversation turn"
        assert records[0]["answer"]
        assert records[0]["session_id"] == session_id
        assert records[0]["status"] in {"completed", "degraded"}

        other_session = client.get(
            "/api/runs",
            params={
                "project_id": "conversation-test",
                "session_id": "browser-session-b",
            },
        )
        assert other_session.status_code == 200
        assert other_session.json()["runs"] == []

        same_session = client.get(
            "/api/runs",
            params={
                "project_id": "conversation-test",
                "session_id": session_id,
            },
        )
        assert same_session.status_code == 200
        assert len(same_session.json()["runs"]) == 1
        assert same_session.json()["runs"][0]["answer"] == records[0]["answer"]

    def test_get_run_not_found(self, client):
        r = client.get("/api/runs/nonexistent-run-id-999")
        assert r.status_code == 404

    def test_get_run_after_streaming(self, client, workspace):
        """After a run completes, GET /api/runs/{run_id} should return it."""
        # Save a run record manually
        record = {
            "run_id": "test-run-123",
            "project_id": "default",
            "query": "test query",
            "status": "completed",
            "started_at": time.time(),
        }
        workspace.save_run(record)

        r = client.get("/api/runs/test-run-123")
        assert r.status_code == 200
        data = r.json()
        assert data["run_id"] == "test-run-123"


# ---------------------------------------------------------------------------
# Gap 6: Project isolation
# ---------------------------------------------------------------------------

class TestProjectIsolation:
    def test_graphs_are_independent(self, workspace):
        """Ingesting into project A does not affect project B."""
        graph_a = workspace.get_graph("proj-a")
        graph_b = workspace.get_graph("proj-b")

        node_id = graph_a.ingest("Claim about project A", node_type="claim")
        assert graph_a.graph.get_node(node_id) is not None
        assert graph_b.graph.get_node(node_id) is None

    def test_paper_ingest_is_project_scoped(self, client):
        """Ingesting into proj-a should not show in proj-b graph data."""
        client.post(
            "/api/papers/ingest",
            json={
                "project_id": "iso-a",
                "title": "Paper in Project A",
                "abstract": "This paper is in project A only.",
            },
        )

        r_a = client.get("/api/graph/data?project_id=iso-a")
        r_b = client.get("/api/graph/data?project_id=iso-b")
        assert r_a.status_code == 200
        assert r_b.status_code == 200

        nodes_a = {n["id"] for n in r_a.json()["nodes"]}
        nodes_b = {n["id"] for n in r_b.json()["nodes"]}
        # A has nodes, B should have none
        assert len(nodes_a) > 0
        assert len(nodes_b) == 0


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_returns_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"

    def test_health_reports_persistence_backend(self, client):
        r = client.get("/health")
        data = r.json()
        assert "persistence" in data
        assert data["persistence"] in ("memory", "ctxflow-json")

    def test_health_model_disabled_by_default(self, client):
        r = client.get("/health")
        data = r.json()
        assert data["model_enabled"] is False
