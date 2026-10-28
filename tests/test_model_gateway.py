"""Tests for the ResearchOS model gateway and its server wiring."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from researchos.api.server import create_app
from researchos.gateway import (
    ModelCallError,
    ModelConfig,
    ModelConfigError,
    ModelGateway,
    ModelGatewayError,
    ModelResponseError,
    create_model_gateway,
    estimate_tokens,
    load_model_config,
    model_enabled,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeCompletions:
    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self._results) > 1:
            result = self._results.pop(0)
        else:
            result = self._results[0]
        if isinstance(result, BaseException):
            raise result
        return result


class FakeClient:
    def __init__(self, results):
        self.completions = FakeCompletions(results)
        self.chat = SimpleNamespace(completions=self.completions)
        self.closed = False

    def close(self):
        self.closed = True


def response(content: str, prompt=10, completion=5, total=15):
    usage = None
    if prompt is not None or completion is not None or total is not None:
        usage = SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
        )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=usage,
    )


def config(**overrides):
    base = ModelConfig(
        provider="groq",
        model_id="test-model",
        base_url="http://model.test/v1",
        api_key="test-key",
        timeout_s=5.0,
        max_retries=2,
    )
    return replace(base, **overrides)


def gateway_with(results, **overrides):
    client = FakeClient(results)
    sleeps = []
    gateway = ModelGateway(
        config(**overrides),
        client=client,
        sleep=sleeps.append,
    )
    return gateway, client, sleeps


class StatusError(Exception):
    def __init__(self, status_code, message="provider error"):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class TestConfiguration:
    def test_disabled_by_default(self):
        assert model_enabled({}) is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_flag_values(self, value):
        assert model_enabled({"RESEARCHOS_MODEL_ENABLED": value}) is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "", "maybe"])
    def test_falsey_flag_values(self, value):
        assert model_enabled({"RESEARCHOS_MODEL_ENABLED": value}) is False

    def test_defaults_to_groq_preset(self):
        cfg = load_model_config({"GROQ_API_KEY": "gsk-test"})
        assert cfg.provider == "groq"
        assert cfg.model_id == "openai/gpt-oss-20b"
        assert cfg.base_url == "https://api.groq.com/openai/v1"
        assert cfg.api_key == "gsk-test"
        assert cfg.timeout_s == 20.0
        assert cfg.max_retries == 2
        assert cfg.temperature == 0.0
        assert cfg.max_tokens == 512

    def test_provider_and_overrides(self):
        cfg = load_model_config(
            {
                "RESEARCHOS_MODEL_PROVIDER": "ollama",
                "RESEARCHOS_MODEL_ID": "llama3.2:3b",
                "RESEARCHOS_MODEL_TIMEOUT_S": "7.5",
                "RESEARCHOS_MAX_RETRIES": "0",
            }
        )
        assert cfg.provider == "ollama"
        assert cfg.model_id == "llama3.2:3b"
        assert cfg.base_url == "http://localhost:11434/v1"
        assert cfg.api_key == "ollama"
        assert cfg.timeout_s == 7.5
        assert cfg.max_retries == 0

    def test_generic_api_key_and_base_url_override(self):
        cfg = load_model_config(
            {
                "RESEARCHOS_MODEL_PROVIDER": "nvidia",
                "RESEARCHOS_MODEL_API_KEY": "generic-key",
                "RESEARCHOS_MODEL_BASE_URL": "https://proxy.test/v1",
            }
        )
        assert cfg.api_key == "generic-key"
        assert cfg.base_url == "https://proxy.test/v1"

    @pytest.mark.parametrize(
        "env, message",
        [
            ({"RESEARCHOS_MODEL_PROVIDER": "nope"}, "Unsupported"),
            ({"RESEARCHOS_MODEL_TIMEOUT_S": "abc", "GROQ_API_KEY": "k"}, "number"),
            ({"RESEARCHOS_MODEL_TIMEOUT_S": "0", "GROQ_API_KEY": "k"}, "> 0"),
            ({"RESEARCHOS_MAX_RETRIES": "-1", "GROQ_API_KEY": "k"}, ">= 0"),
            ({"RESEARCHOS_MAX_RETRIES": "1000", "GROQ_API_KEY": "k"}, "<= 10"),
        ],
    )
    def test_invalid_config_raises(self, env, message):
        with pytest.raises(ModelConfigError, match=message):
            load_model_config(env)

    def test_missing_provider_key_raises(self):
        with pytest.raises(ModelConfigError, match="GROQ_API_KEY"):
            load_model_config({"RESEARCHOS_MODEL_PROVIDER": "groq"})

    def test_create_gateway_disabled_returns_none(self):
        assert create_model_gateway({"GROQ_API_KEY": "gsk-test"}) is None

    def test_create_gateway_enabled(self):
        gateway = create_model_gateway(
            {"RESEARCHOS_MODEL_ENABLED": "1", "GROQ_API_KEY": "gsk-test"}
        )
        assert isinstance(gateway, ModelGateway)
        assert gateway.config.provider == "groq"

    def test_lazy_client_uses_resolved_config(self, monkeypatch):
        openai = pytest.importorskip("openai")
        captured = {}

        class DummyClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        monkeypatch.setattr(openai, "OpenAI", DummyClient)
        gateway = ModelGateway(
            config(timeout_s=9.0, max_retries=4)
        )

        gateway._get_client()

        assert captured == {
            "api_key": "test-key",
            "base_url": "http://model.test/v1",
            "timeout": 9.0,
            # Retries are owned by the gateway, not the SDK.
            "max_retries": 0,
        }


# ---------------------------------------------------------------------------
# Completion and retries
# ---------------------------------------------------------------------------

class TestCompletion:
    def test_complete_returns_content_and_records_usage(self):
        gateway, client, _ = gateway_with([response("hello", 12, 8, 20)])

        assert gateway.complete("prompt", system="system") == "hello"

        sent = client.completions.calls[0]
        assert sent["model"] == "test-model"
        assert sent["temperature"] == 0.0
        assert sent["max_tokens"] == 512
        assert sent["messages"] == [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "prompt"},
        ]
        usage = gateway.usage()
        assert usage["calls"] == 1
        assert usage["prompt_tokens"] == 12
        assert usage["completion_tokens"] == 8
        assert usage["total_tokens"] == 20
        assert usage["failures"] == 0
        assert usage["estimated_calls"] == 0
        assert usage["latency_ms"] >= 0
        assert gateway.usage_by_model()["test-model"]["calls"] == 1

    def test_gateway_is_an_agent_style_callable(self):
        gateway, client, _ = gateway_with([response("ok")])

        assert gateway("hello", system="be terse") == "ok"
        assert client.completions.calls[0]["messages"][0] == {
            "role": "system",
            "content": "be terse",
        }

    def test_per_call_temperature_is_validated(self):
        gateway, client, _ = gateway_with([response("ok")])

        with pytest.raises(ValueError, match="temperature"):
            gateway.complete("prompt", temperature=9.0)

        assert client.completions.calls == []
        assert gateway.usage()["calls"] == 0

    def test_usage_is_estimated_when_provider_omits_it(self):
        gateway, _, _ = gateway_with([response("abcd" * 10, None, None, None)])

        gateway.complete("12345678")

        usage = gateway.usage()
        assert usage["estimated_calls"] == 1
        assert usage["prompt_tokens"] == estimate_tokens("12345678")
        assert usage["completion_tokens"] == estimate_tokens("abcd" * 10)
        assert usage["total_tokens"] == (
            usage["prompt_tokens"] + usage["completion_tokens"]
        )

    def test_retries_transient_errors_then_succeeds(self):
        gateway, _, sleeps = gateway_with(
            [StatusError(503), StatusError(429), response("ok")]
        )

        assert gateway.complete("prompt") == "ok"

        usage = gateway.usage()
        assert usage["calls"] == 1
        assert usage["retries"] == 2
        assert usage["failures"] == 0
        assert sleeps == [0.25, 0.5]

    def test_rate_limit_hint_from_message_is_honoured(self):
        """Groq free tiers report "try again in 18.4s"; sleeping 0.25s is futile."""
        error = StatusError(
            429,
            "Rate limit reached for model in organization on tokens per minute. "
            "Please try again in 18.397s.",
        )
        gateway, _, sleeps = gateway_with([error, response("ok")])

        assert gateway.complete("prompt") == "ok"
        assert sleeps == [18.397]

    def test_rate_limit_hint_from_header_is_honoured(self):
        error = StatusError(429, "slow down")
        error.response = SimpleNamespace(headers={"retry-after": "2.5"})
        gateway, _, sleeps = gateway_with([error, response("ok")])

        assert gateway.complete("prompt") == "ok"
        assert sleeps == [2.5]

    def test_rate_limit_hint_is_capped(self):
        error = StatusError(429, "Please try again in 300s.")
        gateway, _, sleeps = gateway_with([error, response("ok")])

        assert gateway.complete("prompt") == "ok"
        assert sleeps == [30.0]

    def test_non_retryable_error_fails_immediately(self):
        gateway, _, sleeps = gateway_with([StatusError(401)])

        with pytest.raises(ModelCallError) as excinfo:
            gateway.complete("prompt")

        assert excinfo.value.attempts == 1
        assert gateway.usage()["retries"] == 0
        assert gateway.usage()["failures"] == 1
        assert sleeps == []

    def test_auth_failure_trips_a_circuit_breaker(self):
        """An invalid key must not be retried on every later call."""
        gateway, client, _ = gateway_with([StatusError(401), response("ok")])
        calls_before = len(client.completions.calls)

        with pytest.raises(ModelCallError):
            gateway.complete("first")

        # The second call fails fast without touching the provider.
        with pytest.raises(ModelCallError, match="authentication failure"):
            gateway.complete("second")

        assert len(client.completions.calls) == calls_before + 1

    def test_exhausted_retries_raises_with_attempt_count(self):
        gateway, _, sleeps = gateway_with(
            [StatusError(503), StatusError(503), StatusError(503)]
        )

        with pytest.raises(ModelCallError) as excinfo:
            gateway.complete("prompt")

        assert excinfo.value.attempts == 3
        assert gateway.usage()["retries"] == 2
        assert gateway.usage()["failures"] == 1
        assert len(sleeps) == 2

    def test_empty_completion_is_retried(self):
        gateway, _, _ = gateway_with([response("   "), response("ok")])

        assert gateway.complete("prompt") == "ok"
        assert gateway.usage()["retries"] == 1

    def test_close_closes_client_and_blocks_further_calls(self):
        gateway, client, _ = gateway_with([response("ok")])

        gateway.close()
        gateway.close()  # idempotent

        assert client.closed is True
        with pytest.raises(ModelGatewayError, match="closed"):
            gateway.complete("prompt")

    def test_reset_usage_clears_totals_and_by_model(self):
        gateway, _, _ = gateway_with([response("ok", 5, 5, 10)])
        gateway.complete("prompt")

        gateway.reset_usage()

        assert gateway.usage()["calls"] == 0
        assert gateway.usage()["total_tokens"] == 0
        assert gateway.usage_by_model() == {}


# ---------------------------------------------------------------------------
# JSON mode
# ---------------------------------------------------------------------------

class TestJsonCompletion:
    def test_parses_fenced_json(self):
        gateway, _, _ = gateway_with([response('```json\n{"a": 1}\n```')])

        assert gateway.complete_json("return json") == {"a": 1}

    def test_parses_json_surrounded_by_prose(self):
        gateway, _, _ = gateway_with([response('Sure: {"a": [1, 2]} done')])

        assert gateway.complete_json("return json") == {"a": [1, 2]}

    def test_repairs_once_after_invalid_json(self):
        gateway, client, _ = gateway_with(
            [response("not json"), response('{"fixed": true}')]
        )

        assert gateway.complete_json("return json") == {"fixed": True}
        assert len(client.completions.calls) == 2
        assert gateway.usage()["calls"] == 2

    def test_invalid_json_after_repair_raises(self):
        gateway, _, _ = gateway_with(
            [response("not json"), response("still not json")]
        )

        with pytest.raises(ModelResponseError, match="valid JSON"):
            gateway.complete_json("return json")

    def test_json_schema_is_used_when_preset_supports_it(self):
        gateway, client, _ = gateway_with(
            [response('{"a": 1}')], json_schema=True
        )

        schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
        assert gateway.complete_json("return json", schema=schema) == {"a": 1}
        assert client.completions.calls[0]["response_format"]["type"] == "json_schema"


# ---------------------------------------------------------------------------
# Server wiring
# ---------------------------------------------------------------------------

class TestServerWiring:
    def test_app_without_model_exposes_disabled_usage(self, monkeypatch):
        monkeypatch.setenv("RESEARCHOS_MODEL_ENABLED", "0")
        app = create_app()

        with TestClient(app) as client:
            res = client.get("/api/model/usage")

        assert res.status_code == 200
        assert res.json() == {
            "enabled": False,
            "provider": None,
            "model": None,
            "usage": {},
            "by_model": {},
        }
        assert app.state.model_gateway is None

    def test_injected_gateway_reaches_skills_agents_and_usage_endpoint(self):
        gateway, fake_client, _ = gateway_with([response("ok", 3, 2, 5)])
        app = create_app(model_gateway=gateway)

        with TestClient(app) as client:
            assert app.state.model_gateway is gateway

            router = app.state.router
            for name in (
                "retriever",
                "reader",
                "synthesizer",
                "critic",
                "graph_query",
            ):
                assert router._agents[name]._llm is gateway
            assert router._llm is gateway

            # The skills created inside create_app consume the same callable.
            from researchos.api.routes import chat as chat_routes

            assert chat_routes._app_state["paper_reader"]._llm is gateway
            assert chat_routes._app_state["review_writer"]._llm is gateway

            assert gateway.complete("hello") == "ok"

            res = client.get("/api/model/usage")
            assert res.status_code == 200
            body = res.json()
            assert body["enabled"] is True
            assert body["provider"] == "groq"
            assert body["model"] == "test-model"
            assert body["usage"]["calls"] == 1
            assert body["usage"]["total_tokens"] == 5
            assert body["by_model"]["test-model"]["calls"] == 1

        # The caller owns an injected gateway; lifespan must not close it.
        assert fake_client.closed is False

    def test_env_created_gateway_is_closed_on_teardown(self, monkeypatch):
        gateway, fake_client, _ = gateway_with([response("ok")])
        monkeypatch.setattr(
            "researchos.api.server.create_model_gateway", lambda: gateway
        )

        app = create_app()
        with TestClient(app) as client:
            assert client.get("/api/model/usage").json()["enabled"] is True

        assert fake_client.closed is True


# ---------------------------------------------------------------------------
# Failure propagation: a failed model call must not look like success
# ---------------------------------------------------------------------------

class BrokenGateway:
    """Callable gateway whose provider is always unreachable."""

    config = SimpleNamespace(provider="groq", model_id="broken")

    def __call__(self, prompt, system=""):
        raise ModelCallError("provider unavailable", attempts=3)

    def complete(self, *args, **kwargs):
        raise ModelCallError("provider unavailable", attempts=3)

    def usage(self):
        return {"calls": 0, "failures": 1}

    def usage_by_model(self):
        return {}

    def close(self):
        pass


def seed_claim(app):
    app.state.ctxflow.ingest(
        content="Algorithm X reaches 71% accuracy on the benchmark.",
        node_type="claim",
        tags={"algorithm-x", "accuracy"},
        metadata={"project_id": "default"},
    )


class TestSynthesisFailurePropagation:
    def test_chat_reports_llm_failure_in_alerts(self):
        app = create_app(model_gateway=BrokenGateway())
        with TestClient(app) as client:
            seed_claim(app)
            res = client.post(
                "/api/chat",
                json={"query": "What accuracy does Algorithm X reach?"},
            )

        assert res.status_code == 200
        body = res.json()
        assert any(alert["type"] == "llm_failure" for alert in body["alerts"])
        assert "synthesis model was unavailable" in body["answer"]

    def test_stream_degrades_when_synthesizer_fails(self):
        """A broken model gateway should result in a degraded (not failed) run.

        The new ResearchWorkflow SSE done-event carries status/degraded/errors
        instead of the old SkillRouter pipeline_results array.
        """
        app = create_app(model_gateway=BrokenGateway())
        with TestClient(app) as client:
            seed_claim(app)
            with client.stream(
                "GET",
                "/api/stream",
                params={"query": "What accuracy does Algorithm X reach?"},
            ) as response:
                done = None
                event = None
                for line in response.iter_lines():
                    if line.startswith("event:"):
                        event = line[len("event:"):].strip()
                    elif line.startswith("data:"):
                        payload = json.loads(line[len("data:"):].strip())
                        if event == "done":
                            done = payload
                            break

        assert done is not None, "Stream did not emit a 'done' event"
        # A broken model must degrade the answer, not fail the run: the graph
        # evidence and the verdict are still usable.
        assert done.get("status") == "degraded"
        assert done.get("success") is True
        assert done.get("degraded") is True
        assert done.get("errors")
        # Answer must be present (heuristic fallback when LLM fails).
        assert done.get("answer")
        # Secrets must not leak into the response.
        assert "api_key" not in json.dumps(done)


# ---------------------------------------------------------------------------
# Test-environment isolation
# ---------------------------------------------------------------------------

class TestEnvironmentIsolation:
    def test_no_ambient_model_configuration(self):
        import os

        assert "RESEARCHOS_MODEL_ENABLED" not in os.environ
        assert model_enabled() is False

    def test_project_dotenv_cannot_enable_the_gateway(self, tmp_path, monkeypatch):
        dotenv_file = tmp_path / ".env"
        dotenv_file.write_text(
            "RESEARCHOS_MODEL_ENABLED=1\nGROQ_API_KEY=fake\n", encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)

        assert model_enabled() is False
