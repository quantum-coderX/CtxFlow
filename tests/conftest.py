"""Shared test isolation.

The model gateway resolves its configuration from the environment and the
project ``.env``. A developer's local provider settings must not change what
the suite exercises, so every test starts with model configuration cleared and
the project ``.env`` ignored.

Tests that need a gateway inject ``ModelGateway(..., client=FakeClient())`` or
pass ``create_app(model_gateway=...)``; they never call a live provider.
"""

from __future__ import annotations

import pytest

MODEL_ENV_VARS = (
    "RESEARCHOS_MODEL_ENABLED",
    "RESEARCHOS_MODEL_PROVIDER",
    "RESEARCHOS_MODEL_ID",
    "RESEARCHOS_MODEL_BASE_URL",
    "RESEARCHOS_MODEL_API_KEY",
    "RESEARCHOS_MODEL_TIMEOUT_S",
    "RESEARCHOS_MAX_RETRIES",
    "GROQ_API_KEY",
    "NVIDIA_API_KEY",
    "OPENROUTER_API_KEY",
    "HF_TOKEN",
)


@pytest.fixture(autouse=True)
def isolate_model_environment(monkeypatch):
    """Clear model env vars and ignore the project .env for every test."""
    # ``researchos.api.server`` calls ``load_dotenv`` at import time. Import it
    # here first so that side effect happens *before* the cleanup below, even
    # when a test imports the server lazily from inside a fixture.
    import researchos.api.server  # noqa: F401

    for name in MODEL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    # Keep the suite hermetic: no live arXiv/Semantic Scholar/OpenAlex calls and
    # no ``uvx`` subprocess spawn. Tests that exercise those paths inject a fake
    # client or a fake provider function.
    monkeypatch.setenv("RESEARCHOS_EXTERNAL_SEARCH", "off")
    monkeypatch.setenv("RESEARCHOS_MCP_ENABLED", "0")

    try:
        import dotenv
    except ImportError:  # pragma: no cover - dotenv is a core dependency
        return

    # ``researchos.gateway.model`` imports dotenv_values lazily at call time,
    # so patching the module attribute is enough to keep a local .env out of
    # the suite.
    monkeypatch.setattr(dotenv, "dotenv_values", lambda *args, **kwargs: {})
