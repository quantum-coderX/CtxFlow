"""Application lifecycle helpers for the PostgreSQL memory path."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
from typing import Any

import asyncpg
from dotenv import dotenv_values

from ctxflow.memory.embeddings import Embedder, HashEmbedder, LocalBGEEmbedder
from ctxflow.memory.edge_proposer import LLMEdgeProposer
from ctxflow.memory.ingestion import EventIngestor
from ctxflow.memory.postgres_store import PostgresMemoryStore

_REQUIRED_DATABASE_VARIABLES = (
    "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"
)
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DatabaseConfig:
    """Connection settings loaded independently from environment variables."""

    host: str
    port: int
    database: str
    user: str
    password: str


def load_database_config(dotenv_path: str | Path | None = None) -> DatabaseConfig:
    """Load and validate PostgreSQL settings without constructing a URL.

    An explicit ``dotenv_path`` is authoritative. Without one, process
    environment variables take precedence over a local ``.env`` (containers).
    """

    if dotenv_path is not None:
        values = dotenv_values(dotenv_path)
    else:
        values = {**dotenv_values(), **{k: v for k, v in os.environ.items() if v}}
    missing = [name for name in _REQUIRED_DATABASE_VARIABLES if not values.get(name)]
    if missing:
        raise ValueError(
            "Missing required database environment variables: " + ", ".join(missing)
        )
    try:
        port = int(str(values["DB_PORT"]))
    except ValueError as error:
        raise ValueError("DB_PORT must be an integer") from error
    if not 1 <= port <= 65535:
        raise ValueError("DB_PORT must be between 1 and 65535")
    return DatabaseConfig(
        host=str(values["DB_HOST"]),
        port=port,
        database=str(values["DB_NAME"]),
        user=str(values["DB_USER"]),
        password=str(values["DB_PASSWORD"]),
    )


async def create_pool(
    config: DatabaseConfig | None = None,
    *,
    dotenv_path: str | Path | None = None,
    **pool_options: Any,
) -> Any:
    """Create an asyncpg pool using separate, validated connection fields."""

    settings = config or load_database_config(dotenv_path)
    return await asyncpg.create_pool(
        host=settings.host,
        port=settings.port,
        database=settings.database,
        user=settings.user,
        password=settings.password,
        **pool_options,
    )


@dataclass
class PostgresMemoryRuntime:
    """Owned PostgreSQL resources and the configured event ingestor."""

    pool: Any
    store: PostgresMemoryStore
    ingestor: EventIngestor

    @classmethod
    async def create(
        cls,
        *,
        embedder: Embedder | None = None,
        extractor: Any | None = None,
        edge_proposer: LLMEdgeProposer | None = None,
        dotenv_path: str | Path | None = None,
        llm_timeout: float = 30.0,
        extraction_retries: int = 2,
        provider_timeout: float = 30.0,
        provider_retries: int = 2,
        **pool_options: Any,
    ) -> "PostgresMemoryRuntime":
        if embedder is not None:
            active_embedder = embedder
        else:
            try:
                active_embedder = LocalBGEEmbedder()
            except RuntimeError as error:
                _LOGGER.warning(
                    "Local BGE embedder unavailable; using the deterministic "
                    "384-dimensional HashEmbedder fallback: %s",
                    error,
                )
                active_embedder = HashEmbedder(dimension=384)
        pool = await create_pool(dotenv_path=dotenv_path, **pool_options)
        groq_key = _groq_api_key(dotenv_path)
        active_extractor = extractor
        active_edge_proposer = edge_proposer
        if groq_key and (active_extractor is None or active_edge_proposer is None):
            from ctxflow.llm_extractor import LLMExtractor
            from ctxflow.providers.groq import GroqLLM

            llm = GroqLLM(api_key=groq_key, timeout=llm_timeout)
            if active_extractor is None:
                active_extractor = LLMExtractor(
                    llm, extraction_retries=extraction_retries
                )
            if active_edge_proposer is None:
                active_edge_proposer = LLMEdgeProposer(llm)
        store = PostgresMemoryStore(pool, embedding_dimension=active_embedder.dimension)
        return cls(
            pool,
            store,
            EventIngestor(
                store,
                active_embedder,
                active_extractor,
                edge_proposer=active_edge_proposer,
                provider_timeout=provider_timeout,
                provider_retries=provider_retries,
            ),
        )

    async def close(self) -> None:
        await self.pool.close()

    async def __aenter__(self) -> "PostgresMemoryRuntime":
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        await self.close()


def _groq_api_key(dotenv_path: str | Path | None) -> str | None:
    value = dotenv_values(dotenv_path).get("GROQ_API_KEY")
    return os.getenv("GROQ_API_KEY") or (str(value) if value else None)
