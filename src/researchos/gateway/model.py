"""Single model gateway for ResearchOS.

This module is the only place in the codebase that imports a provider SDK
(``openai``), and it does so lazily so a core install without the ``llm``
extra keeps working. Agents and skills never see a client: they receive a
plain ``(prompt, system="") -> str`` callable.

Configuration is environment-driven (see :data:`PROVIDER_PRESETS` and
:func:`load_model_config`). The feature flag ``RESEARCHOS_MODEL_ENABLED``
defaults to off, which preserves the deterministic, no-network behaviour of
``create_app()`` and the existing test suite.

Contract (MVP plan section 11/12)::

    gateway = create_model_gateway()
    if gateway is not None:
        answer = gateway.complete(prompt, system="You are ...")

    # Agents receive the callable, not the gateway:
    SynthesizerAgent(ctxflow=ctxflow, llm=gateway)
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429})
_JSON_REPAIR_SYSTEM = (
    "You repair malformed JSON. Return only one valid JSON value. "
    "Do not add prose or markdown fences."
)
_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*(.*?)\s*```\s*$",
    re.DOTALL | re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ModelGatewayError(RuntimeError):
    """Base class for gateway failures."""


class ModelConfigError(ModelGatewayError, ValueError):
    """Invalid provider/model configuration."""


class ModelCallError(ModelGatewayError):
    """A provider call failed after the configured number of attempts."""

    def __init__(
        self,
        message: str,
        *,
        attempts: int = 0,
        last_error: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.last_error = last_error


class ModelResponseError(ModelGatewayError):
    """The provider returned a response the gateway could not use."""


# ---------------------------------------------------------------------------
# Provider presets
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderPreset:
    """Connection details for one OpenAI-compatible provider."""

    base_url: str
    default_model: str
    api_key_env: Optional[str] = None
    requires_api_key: bool = True
    json_object: bool = True
    json_schema: bool = False


PROVIDER_PRESETS: Dict[str, ProviderPreset] = {
    "groq": ProviderPreset(
        base_url="https://api.groq.com/openai/v1",
        default_model="openai/gpt-oss-20b",
        api_key_env="GROQ_API_KEY",
        json_object=True,
        json_schema=True,
    ),
    "ollama": ProviderPreset(
        base_url="http://localhost:11434/v1",
        default_model="qwen3:8b",
        api_key_env=None,
        requires_api_key=False,
        json_object=True,
        json_schema=False,
    ),
    "nvidia": ProviderPreset(
        base_url="https://integrate.api.nvidia.com/v1",
        default_model="meta/llama-3.3-70b-instruct",
        api_key_env="NVIDIA_API_KEY",
        json_object=True,
        json_schema=False,
    ),
    "openrouter": ProviderPreset(
        base_url="https://openrouter.ai/api/v1",
        default_model="openai/gpt-oss-20b",
        api_key_env="OPENROUTER_API_KEY",
        json_object=True,
        json_schema=False,
    ),
    "hf": ProviderPreset(
        base_url="https://router.huggingface.co/v1",
        default_model="Qwen/Qwen3-8B",
        api_key_env="HF_TOKEN",
        json_object=True,
        json_schema=False,
    ),
}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Upper bound on retries after the first attempt. Three attempts is the
# documented default; ten is already generous and still bounded.
_MAX_RETRIES = 10

# Free tiers (for example Groq's 8k tokens-per-minute window) reject a call
# with a "try again in Ns" hint. Honour the hint, but keep one stalled call
# from parking the whole run: never sleep longer than this, per attempt.
_MAX_RETRY_DELAY_S = 30.0


@dataclass(frozen=True)
class ModelConfig:
    """Resolved gateway configuration.

    ``max_retries`` is the number of retries *after* the first attempt, so
    the default of ``2`` allows at most three attempts per call. It is capped
    at :data:`_MAX_RETRIES` so a typo cannot turn one call into an hours-long
    retry storm.
    """

    provider: str = "groq"
    model_id: str = PROVIDER_PRESETS["groq"].default_model
    base_url: str = PROVIDER_PRESETS["groq"].base_url
    api_key: Optional[str] = None
    timeout_s: float = 20.0
    max_retries: int = 2
    temperature: float = 0.0
    max_tokens: int = 512
    json_object: bool = True
    json_schema: bool = False

    def __post_init__(self) -> None:
        if self.provider not in PROVIDER_PRESETS:
            raise ModelConfigError(
                "Unsupported RESEARCHOS_MODEL_PROVIDER "
                f"{self.provider!r}; expected one of "
                + ", ".join(sorted(PROVIDER_PRESETS))
            )
        if not self.model_id:
            raise ModelConfigError("RESEARCHOS_MODEL_ID must not be empty")
        if self.timeout_s <= 0:
            raise ModelConfigError("RESEARCHOS_MODEL_TIMEOUT_S must be > 0")
        if self.max_retries < 0:
            raise ModelConfigError("RESEARCHOS_MAX_RETRIES must be >= 0")
        if self.max_retries > _MAX_RETRIES:
            raise ModelConfigError(
                f"RESEARCHOS_MAX_RETRIES must be <= {_MAX_RETRIES}, "
                f"got {self.max_retries}"
            )
        if self.max_tokens <= 0:
            raise ModelConfigError("max_tokens must be > 0")
        if not 0.0 <= self.temperature <= 2.0:
            raise ModelConfigError("temperature must be between 0 and 2")


def _read_env(env: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    """Return an env mapping, merging the project ``.env`` for defaults.

    An explicitly supplied mapping is used as-is so tests are isolated from
    the developer's local ``.env``.
    """

    if env is not None:
        return env

    merged: Dict[str, str] = {}
    try:
        from dotenv import dotenv_values

        merged.update(
            {key: value for key, value in dotenv_values().items() if value is not None}
        )
    except Exception:  # pragma: no cover - dotenv is a core dependency
        pass
    merged.update(os.environ)
    return merged


def model_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """Whether the model gateway should be constructed.

    Disabled by default so ``create_app()`` stays deterministic and makes no
    network calls unless the operator opts in.
    """

    raw = _read_env(env).get("RESEARCHOS_MODEL_ENABLED", "")
    return str(raw).strip().lower() in _TRUTHY


def _parse_float(raw: Any, default: float, name: str) -> float:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ModelConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value <= 0:
        raise ModelConfigError(f"{name} must be > 0, got {value}")
    return value


def _parse_int(raw: Any, default: int, name: str) -> int:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ModelConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < 0:
        raise ModelConfigError(f"{name} must be >= 0, got {value}")
    return value


def load_model_config(env: Optional[Mapping[str, str]] = None) -> ModelConfig:
    """Resolve ``RESEARCHOS_MODEL_*`` variables into a :class:`ModelConfig`.

    Recognised variables:

    ``RESEARCHOS_MODEL_PROVIDER``
        ``groq`` (default), ``ollama``, ``nvidia``, ``openrouter`` or ``hf``.
    ``RESEARCHOS_MODEL_ID``
        Provider model id; defaults to the preset.
    ``RESEARCHOS_MODEL_TIMEOUT_S``
        Per-request timeout in seconds; default ``20``.
    ``RESEARCHOS_MAX_RETRIES``
        Retries after the first attempt; default ``2``.
    ``RESEARCHOS_MODEL_BASE_URL`` / ``RESEARCHOS_MODEL_API_KEY``
        Optional escape hatches for self-hosted or proxied endpoints.
    """

    values = _read_env(env)
    provider = str(values.get("RESEARCHOS_MODEL_PROVIDER") or "groq").strip().lower()
    preset = PROVIDER_PRESETS.get(provider)
    if preset is None:
        raise ModelConfigError(
            "Unsupported RESEARCHOS_MODEL_PROVIDER "
            f"{provider!r}; expected one of " + ", ".join(sorted(PROVIDER_PRESETS))
        )

    model_id = str(values.get("RESEARCHOS_MODEL_ID") or preset.default_model).strip()
    base_url = str(values.get("RESEARCHOS_MODEL_BASE_URL") or preset.base_url).strip()
    raw_key = values.get("RESEARCHOS_MODEL_API_KEY") or (
        values.get(preset.api_key_env) if preset.api_key_env else None
    )
    api_key = str(raw_key).strip() if raw_key else None

    if preset.requires_api_key and not api_key:
        env_name = preset.api_key_env or "RESEARCHOS_MODEL_API_KEY"
        raise ModelConfigError(
            f"{env_name} must be set to use the {provider!r} provider"
        )

    # Ollama ignores the key, but the OpenAI client requires a non-empty value.
    if not preset.requires_api_key and not api_key:
        api_key = "ollama"

    return ModelConfig(
        provider=provider,
        model_id=model_id,
        base_url=base_url,
        api_key=api_key,
        timeout_s=_parse_float(
            values.get("RESEARCHOS_MODEL_TIMEOUT_S"),
            20.0,
            "RESEARCHOS_MODEL_TIMEOUT_S",
        ),
        max_retries=_parse_int(
            values.get("RESEARCHOS_MAX_RETRIES"),
            2,
            "RESEARCHOS_MAX_RETRIES",
        ),
        json_object=preset.json_object,
        json_schema=preset.json_schema,
    )


# ---------------------------------------------------------------------------
# Usage accounting
# ---------------------------------------------------------------------------

@dataclass
class _Stats:
    calls: int = 0
    failures: int = 0
    retries: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_calls: int = 0
    latency_ms: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "calls": self.calls,
            "failures": self.failures,
            "retries": self.retries,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated_calls": self.estimated_calls,
            "latency_ms": round(self.latency_ms, 3),
        }


def estimate_tokens(text: str) -> int:
    """Estimate tokens at the project's 4-characters-per-token convention."""

    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


def _get_attr_or_key(value: Any, name: str) -> Any:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _response_content(response: Any) -> str:
    choices = _get_attr_or_key(response, "choices")
    if not choices:
        raise ModelResponseError("provider response contained no choices")
    message = _get_attr_or_key(choices[0], "message")
    content = _get_attr_or_key(message, "content")
    if not isinstance(content, str) or not content.strip():
        raise ModelResponseError("provider returned an empty completion")
    return content


def _response_usage(response: Any) -> Optional[Tuple[Optional[int], Optional[int], Optional[int]]]:
    usage = _get_attr_or_key(response, "usage")
    if usage is None:
        return None
    return (
        _get_attr_or_key(usage, "prompt_tokens"),
        _get_attr_or_key(usage, "completion_tokens"),
        _get_attr_or_key(usage, "total_tokens"),
    )


def _parse_json(text: str) -> Any:
    """Parse JSON, tolerating markdown fences and leading/trailing prose."""

    stripped = text.strip()
    fenced = _FENCE_RE.match(stripped)
    if fenced:
        stripped = fenced.group(1).strip()

    try:
        return json.loads(stripped)
    except json.JSONDecodeError as first_error:
        starts = [index for index in (stripped.find("{"), stripped.find("[")) if index >= 0]
        if not starts:
            raise first_error
        start = min(starts)
        end = max(stripped.rfind("}"), stripped.rfind("]"))
        if end <= start:
            raise first_error
        return json.loads(stripped[start : end + 1])


def _is_retryable(exc: BaseException) -> bool:
    """Classify provider errors without importing the SDK."""

    if isinstance(exc, (ValueError, TypeError, KeyError, ModelConfigError)):
        return False
    if isinstance(exc, ModelResponseError):
        # Empty/obstructed completions are worth one more attempt.
        return True

    status = getattr(exc, "status_code", None)
    if status is None:
        status = _get_attr_or_key(getattr(exc, "response", None), "status_code")
    if isinstance(status, int):
        return status in _RETRYABLE_STATUS or status >= 500

    name = type(exc).__name__.lower()
    return any(
        marker in name
        for marker in (
            "timeout",
            "connection",
            "ratelimit",
            "internalserver",
            "serviceunavailable",
        )
    )


def _is_auth_error(exc: BaseException) -> bool:
    """True for a bad/missing API key (HTTP 401), which will not self-heal."""

    status = getattr(exc, "status_code", None)
    if status is None:
        status = _get_attr_or_key(getattr(exc, "response", None), "status_code")
    if status == 401:
        return True
    return "authentication" in type(exc).__name__.lower()


_RETRY_AFTER_SECONDS_RE = re.compile(
    r"try again in\s+([0-9]+(?:\.[0-9]+)?)\s*(ms|s)\b", re.IGNORECASE
)
_RETRY_AFTER_HEADER_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")


def _retry_after_seconds(exc: BaseException) -> Optional[float]:
    """Best-effort rate-limit hint from a provider error, in seconds.

    Providers expose the hint either as a ``retry-after`` header (seconds) or
    inside the message, e.g. Groq's "Please try again in 18.39s".
    """

    headers = _get_attr_or_key(getattr(exc, "response", None), "headers")
    raw = _get_attr_or_key(headers, "retry-after")
    if raw is None:
        raw = _get_attr_or_key(headers, "Retry-After")
    if isinstance(raw, str) and _RETRY_AFTER_HEADER_RE.match(raw.strip()):
        return max(0.0, float(raw.strip()))

    message = str(exc)
    match = _RETRY_AFTER_SECONDS_RE.search(message)
    if not match:
        return None
    value = float(match.group(1))
    if match.group(2).lower() == "ms":
        value /= 1000.0
    return max(0.0, value)


def _retry_delay(exc: BaseException, attempt: int) -> float:
    """Backoff for one retry: provider hint when present, else exponential."""

    base = 0.25 * (2 ** attempt)
    hint = _retry_after_seconds(exc)
    if hint is None:
        return min(8.0, base)
    # A hint shorter than our own backoff is not worth trusting.
    return min(_MAX_RETRY_DELAY_S, max(base, hint))


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------

class ModelGateway:
    """One OpenAI-compatible client with retries and token accounting.

    Parameters
    ----------
    config : ModelConfig
        Resolved provider/model settings.
    client : optional
        Pre-built client, used by tests. When omitted, an ``openai.OpenAI``
        client is created lazily on the first call.
    sleep : callable, optional
        Backoff function; defaults to :func:`time.sleep`. Tests pass a no-op.
    clock : callable, optional
        Monotonic clock; defaults to :func:`time.monotonic`.
    """

    def __init__(
        self,
        config: ModelConfig,
        *,
        client: Any = None,
        sleep: Any = None,
        clock: Any = None,
    ) -> None:
        self.config = config
        self._client = client
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._closed = False
        self._stats = _Stats()
        self._by_model: Dict[str, _Stats] = {}
        # Set after an authentication failure: retrying an invalid key on every
        # claim/tag/synthesis call would make ingestion crawl and spam the log.
        self._unavailable_reason: Optional[str] = None

    # -- provider ---------------------------------------------------------

    def _get_client(self) -> Any:
        if self._closed:
            raise ModelGatewayError("model gateway is closed")
        if self._client is not None:
            return self._client

        with self._lock:
            if self._client is not None:
                return self._client
            if self._closed:
                raise ModelGatewayError("model gateway is closed")
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover - depends on extras
                raise ModelConfigError(
                    "The 'openai' package is required for model calls. "
                    "Install it with `pip install -e \".[llm]\"`."
                ) from exc
            self._client = OpenAI(
                api_key=self.config.api_key,
                base_url=self.config.base_url,
                timeout=self.config.timeout_s,
                max_retries=0,
            )
            return self._client

    def _stats_for_model(self) -> _Stats:
        return self._by_model.setdefault(self.config.model_id, _Stats())

    def _record_retry(self) -> None:
        with self._lock:
            self._stats.retries += 1
            self._stats_for_model().retries += 1

    def _record_success(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        estimated: bool,
        latency_ms: float,
    ) -> None:
        with self._lock:
            for stats in (self._stats, self._stats_for_model()):
                stats.calls += 1
                stats.prompt_tokens += prompt_tokens
                stats.completion_tokens += completion_tokens
                stats.total_tokens += total_tokens
                stats.latency_ms += latency_ms
                if estimated:
                    stats.estimated_calls += 1

    def _record_failure(self, latency_ms: float) -> None:
        with self._lock:
            for stats in (self._stats, self._stats_for_model()):
                stats.failures += 1
                stats.latency_ms += latency_ms

    # -- completion -------------------------------------------------------

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Return the model's text completion for *prompt*.

        Retries transient provider failures up to ``config.max_retries``
        times. Exhausted or non-retryable failures raise :class:`ModelCallError`.
        """

        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        if self._unavailable_reason:
            raise ModelCallError(
                "model gateway disabled after an authentication failure: "
                f"{self._unavailable_reason}"
            )
        client = self._get_client()

        token_limit = self.config.max_tokens if max_tokens is None else max_tokens
        if token_limit <= 0:
            raise ValueError("max_tokens must be > 0")
        temp = self.config.temperature if temperature is None else temperature
        if not 0.0 <= temp <= 2.0:
            raise ValueError("temperature must be between 0 and 2")

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        kwargs: Dict[str, Any] = {
            "model": self.config.model_id,
            "messages": messages,
            "temperature": temp,
            "max_tokens": token_limit,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format

        started = self._clock()
        attempts = 0
        last_error: Optional[BaseException] = None

        for attempt in range(self.config.max_retries + 1):
            attempts = attempt + 1
            try:
                response = client.chat.completions.create(**kwargs)
                content = _response_content(response)
            except Exception as exc:
                last_error = exc
                if attempt >= self.config.max_retries or not _is_retryable(exc):
                    latency_ms = (self._clock() - started) * 1000.0
                    self._record_failure(latency_ms)
                    if _is_auth_error(exc):
                        self._unavailable_reason = (
                            str(exc).strip() or type(exc).__name__
                        )
                    raise ModelCallError(
                        f"model call failed after {attempts} attempt(s): {exc}",
                        attempts=attempts,
                        last_error=exc,
                    ) from exc

                self._record_retry()
                delay = _retry_delay(exc, attempt)
                logger.warning(
                    "Model call to %s/%s failed (attempt %d/%d): %s; retrying in %.2fs",
                    self.config.provider,
                    self.config.model_id,
                    attempts,
                    self.config.max_retries + 1,
                    exc,
                    delay,
                )
                self._sleep(delay)
                continue

            # Success -----------------------------------------------------
            usage = _response_usage(response)
            if usage is None:
                prompt_tokens = estimate_tokens(
                    "\n".join(message["content"] for message in messages)
                )
                completion_tokens = estimate_tokens(content)
                total_tokens = prompt_tokens + completion_tokens
                estimated = True
            else:
                raw_prompt, raw_completion, raw_total = usage
                estimated = raw_prompt is None or raw_completion is None
                prompt_tokens = (
                    int(raw_prompt)
                    if raw_prompt is not None
                    else estimate_tokens(
                        "\n".join(message["content"] for message in messages)
                    )
                )
                completion_tokens = (
                    int(raw_completion)
                    if raw_completion is not None
                    else estimate_tokens(content)
                )
                total_tokens = (
                    int(raw_total)
                    if raw_total is not None
                    else prompt_tokens + completion_tokens
                )

            latency_ms = (self._clock() - started) * 1000.0
            self._record_success(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                estimated=estimated,
                latency_ms=latency_ms,
            )
            return content

        # The loop always returns or raises; this keeps type checkers happy.
        raise ModelCallError(  # pragma: no cover
            "model call failed without an attempt",
            attempts=attempts,
            last_error=last_error,
        )

    def __call__(self, prompt: str, system: str = "") -> str:
        """Make the gateway usable anywhere an ``LLMCallable`` is expected."""

        return self.complete(prompt, system=system)

    def complete_json(
        self,
        prompt: str,
        *,
        system: str = "",
        schema: Optional[Dict[str, Any]] = None,
        max_tokens: Optional[int] = None,
    ) -> Any:
        """Return parsed JSON, repairing one malformed response.

        Uses provider-side JSON mode when the preset supports it, then falls
        back to parsing plus a single repair call. Schema validation is left
        to the caller; ``schema`` is only used for provider-side enforcement.
        """

        response_format: Optional[Dict[str, Any]] = None
        if schema is not None and self.config.json_schema:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "response",
                    "schema": schema,
                    "strict": True,
                },
            }
        elif self.config.json_object:
            response_format = {"type": "json_object"}

        raw = self.complete(
            prompt,
            system=system,
            max_tokens=max_tokens,
            response_format=response_format,
        )
        try:
            return _parse_json(raw)
        except json.JSONDecodeError:
            repair_prompt = (
                "The following text was supposed to be JSON but is not valid. "
                "Return only the corrected JSON value.\n\n" + raw
            )
            repaired = self.complete(
                repair_prompt,
                system=_JSON_REPAIR_SYSTEM,
                max_tokens=max_tokens,
                response_format=response_format,
            )
            try:
                return _parse_json(repaired)
            except json.JSONDecodeError as exc:
                raise ModelResponseError(
                    "model did not return valid JSON after one repair"
                ) from exc

    # -- observability ----------------------------------------------------

    def usage(self) -> Dict[str, Any]:
        """Return cumulative gateway usage (tokens, latency, failures)."""

        with self._lock:
            return self._stats.as_dict()

    def usage_by_model(self) -> Dict[str, Dict[str, Any]]:
        """Return cumulative usage keyed by model id."""

        with self._lock:
            return {model: stats.as_dict() for model, stats in self._by_model.items()}

    def reset_usage(self) -> None:
        """Clear all counters (useful between benchmark runs)."""

        with self._lock:
            self._stats = _Stats()
            self._by_model.clear()

    def close(self) -> None:
        """Close the underlying client, if any."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            client = self._client
            self._client = None

        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # pragma: no cover - defensive
                logger.warning("Failed to close model client", exc_info=True)


def create_model_gateway(
    env: Optional[Mapping[str, str]] = None,
) -> Optional[ModelGateway]:
    """Build a gateway when ``RESEARCHOS_MODEL_ENABLED`` is truthy.

    Returns ``None`` when the flag is off, which is the default. When the flag
    is on but the configuration is invalid, :class:`ModelConfigError` is raised
    so the operator sees the misconfiguration instead of a silent fallback.
    """

    if not model_enabled(env):
        return None
    return ModelGateway(load_model_config(env))
