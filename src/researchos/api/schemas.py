"""API Schemas — Pydantic request/response models for the FastAPI backend.

Defines all the request/response types used by the API routes,
ensuring strong type validation and automatic OpenAPI documentation.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    """User chat message request."""

    query: str = Field(..., description="Natural-language query or instruction.")
    session_id: Optional[str] = Field(
        None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._-]+$",
        description="Session ID for conversation tracking.",
    )
    project_id: str = Field("default", description="Research project workspace ID.")
    include_trace: bool = Field(
        False, description="Whether to include research trace in the response."
    )
    token_budget: int = Field(
        2048,
        ge=1,
        le=100_000,
        description="Token budget for context assembly.",
    )


class ChatResponse(BaseModel):
    """Response to a chat query."""

    run_id: str = ""
    session_id: str = "default"
    answer: str = Field(..., description="Generated answer.")
    intent: str = Field("", description="Classified query intent.")
    cited_nodes: List[str] = Field(
        default_factory=list, description="IDs of cited nodes."
    )
    alerts: List[Dict[str, Any]] = Field(
        default_factory=list, description="Supersession/contradiction alerts."
    )
    trace: Optional[Dict[str, Any]] = Field(
        None, description="Research trace if requested."
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict, description="Execution metadata."
    )
    external_search: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Outcome of the external literature search, when the pipeline "
            "performed one: status ok|empty|partial|error|skipped|unknown, "
            "per-provider detail, and error messages."
        ),
    )


# ---------------------------------------------------------------------------
# Paper Ingestion
# ---------------------------------------------------------------------------

class PaperIngestRequest(BaseModel):
    """Request to ingest a paper."""

    project_id: str = Field("default", description="Project workspace ID.")
    title: str = Field("", description="Paper title.")
    abstract: str = Field("", description="Paper abstract.")
    authors: List[str] = Field(
        default_factory=list, description="Author names."
    )
    year: Optional[int] = Field(None, description="Publication year.")
    doi: str = Field("", description="DOI identifier.")
    arxiv_id: str = Field("", description="arXiv identifier.")
    url: str = Field("", description="Paper URL.")
    content: str = Field(
        "", description="Full paper text (alternative to PDF upload)."
    )


class PaperIngestResponse(BaseModel):
    """Response after ingesting a paper."""

    success: bool = True
    paper_node_id: str = Field("", description="ID of the paper reference node.")
    claim_node_ids: List[str] = Field(
        default_factory=list, description="IDs of extracted claim nodes."
    )
    contradictions_found: int = Field(
        0, description="Number of contradictions detected."
    )
    project_id: str = Field("default", description="Project workspace ID.")
    message: str = ""


# ---------------------------------------------------------------------------
# Graph / Node
# ---------------------------------------------------------------------------

class NodeResponse(BaseModel):
    """A single node in the knowledge graph."""

    id: str
    content: str
    summary: str = ""
    node_type: str = "generic"
    status: str = "active"
    tags: List[str] = Field(default_factory=list)
    superseded_by: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    timestamp: float = 0.0


class EdgeResponse(BaseModel):
    """A single edge in the knowledge graph."""

    source_id: str
    target_id: str
    relation_type: str = "related_to"
    weight: float = 1.0
    metadata: Dict[str, Any] = Field(default_factory=dict)


class GraphStatsResponse(BaseModel):
    """Knowledge graph statistics."""

    total_nodes: int = 0
    total_edges: int = 0
    status_distribution: Dict[str, int] = Field(default_factory=dict)
    type_distribution: Dict[str, int] = Field(default_factory=dict)


class GraphDataResponse(BaseModel):
    """Full graph data for visualization."""

    nodes: List[NodeResponse] = Field(default_factory=list)
    edges: List[EdgeResponse] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Demo corpus
# ---------------------------------------------------------------------------

class DemoSeedRequest(BaseModel):
    """Request to load the checked-in demo corpus."""

    project_id: str = Field("default", description="Project workspace ID.")


class DemoSeedResponse(BaseModel):
    """Result of loading the demo corpus (idempotent per arXiv ID)."""

    success: bool = True
    papers_ingested: int = 0
    claims_ingested: int = 0
    papers_skipped: int = 0
    nodes: int = 0
    edges: int = 0
    project_id: str = "default"
    relations_applied: int = 0
    relations_already_present: int = 0
    relations_skipped: int = 0
    superseded_claims: int = 0
    contested_claims: int = 0
    message: str = ""


# ---------------------------------------------------------------------------
# Research Trace
# ---------------------------------------------------------------------------

class TraceRequest(BaseModel):
    """Request for a research trace."""

    node_id: str = Field(..., description="Node ID to trace.")


class TraceStepResponse(BaseModel):
    """A step in a provenance chain."""

    node_id: str
    content: str
    summary: str = ""
    node_type: str = ""
    status: str = "active"
    edge_relation: Optional[str] = None
    edge_weight: Optional[float] = None


class TraceResponse(BaseModel):
    """Full research trace response."""

    answer_node_id: str
    provenance_chain: List[TraceStepResponse] = Field(default_factory=list)
    superseded_claims: List[NodeResponse] = Field(default_factory=list)
    contradictions: List[Dict[str, Any]] = Field(default_factory=list)
    source_papers: List[NodeResponse] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

class QueryRequest(BaseModel):
    """Knowledge graph query request."""

    query: str = Field(..., description="Natural-language query.")
    k: int = Field(5, description="Number of results.")
    anchor: Optional[str] = Field(None, description="Anchor node for proximity.")
    include_superseded: bool = Field(False, description="Include superseded nodes.")


class QueryResultResponse(BaseModel):
    """A single query result."""

    node_id: str
    content: str
    summary: str = ""
    score: float = 0.0
    node_type: str = ""
    status: str = "active"
    tags: List[str] = Field(default_factory=list)
    score_breakdown: Dict[str, float] = Field(default_factory=dict)


class QueryResponse(BaseModel):
    """Query response with ranked results."""

    results: List[QueryResultResponse] = Field(default_factory=list)
    total: int = 0


# ---------------------------------------------------------------------------
# Literature Search
# ---------------------------------------------------------------------------

class SearchRequest(BaseModel):
    """External literature search request."""

    query: str = Field(..., description="Search query.")
    max_results: int = Field(5, description="Max results per provider.")
    provider: str = Field(
        "both",
        description=(
            "Search provider: arxiv, semantic_scholar, openalex, or both "
            "(arxiv + semantic_scholar + openalex)."
        ),
    )


class SearchResultResponse(BaseModel):
    """A single search result."""

    title: str = ""
    authors: List[str] = Field(default_factory=list)
    abstract: str = ""
    year: Optional[int] = None
    doi: str = ""
    arxiv_id: str = ""
    url: str = ""
    source: str = ""


class ProviderOutcomeResponse(BaseModel):
    """Outcome of a single literature search provider."""

    provider: str
    status: str = "unknown"
    result_count: int = 0
    error: str = ""
    error_type: str = ""
    status_code: Optional[int] = None
    from_cache: bool = False
    elapsed_ms: float = 0.0
    transport: str = "http"


class SearchResponse(BaseModel):
    """Literature search response.

    ``results`` stays the primary payload; ``status`` and ``errors`` make a
    provider outage distinguishable from "no papers found".
    """

    results: List[SearchResultResponse] = Field(default_factory=list)
    total: int = 0
    status: str = "unknown"
    provider: str = ""
    providers: Dict[str, ProviderOutcomeResponse] = Field(default_factory=dict)
    errors: Dict[str, str] = Field(default_factory=dict)
    elapsed_ms: float = 0.0


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

class NoteCreateRequest(BaseModel):
    """Request to create a researcher note in a project."""

    content: str = Field(..., min_length=1, description="Note text.")
    project_id: str = Field("default", description="Project workspace ID.")
    title: str = Field("", description="Optional note title.")
    tags: List[str] = Field(default_factory=list, description="Extra tags.")


class NoteResponse(BaseModel):
    """A stored researcher note."""

    node_id: str
    content: str
    title: str = ""
    project_id: str = "default"
    tags: List[str] = Field(default_factory=list)
    timestamp: float = 0.0


class NoteListResponse(BaseModel):
    """List of notes in a project."""

    notes: List[NoteResponse] = Field(default_factory=list)
    total: int = 0


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------

class SkillInfoResponse(BaseModel):
    """Information about a registered skill."""

    name: str
    description: str = ""
    version: str = "0.1.0"
    tags: List[str] = Field(default_factory=list)
    input_schema: Dict[str, Any] = Field(default_factory=dict)
    output_schema: Dict[str, Any] = Field(default_factory=dict)


class SkillListResponse(BaseModel):
    """List of all registered skills."""

    skills: List[SkillInfoResponse] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    """API health check response."""

    status: str = "ok"
    version: str = ""
    graph_nodes: int = 0
    graph_edges: int = 0
    persistence: str = "memory"
    model_enabled: bool = False
    model_provider: Optional[str] = None
    model_id: Optional[str] = None
    degraded: bool = False


class ProjectCreateRequest(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=64)
    name: str = ""
    description: str = ""


class ProjectResponse(BaseModel):
    project_id: str
    name: str = ""
    description: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    nodes: int = 0
    edges: int = 0


class ProjectListResponse(BaseModel):
    projects: List[ProjectResponse] = Field(default_factory=list)


class RunStartRequest(BaseModel):
    query: str = Field(..., min_length=1)
    project_id: str = Field("default")
    token_budget: int = Field(2048, ge=1, le=100_000)


class RunEventResponse(BaseModel):
    event: str = "stage"
    run_id: str = ""
    stage: str = ""
    status: str = ""
    message: str = ""
    ts: float = 0.0
    data: Dict[str, Any] = Field(default_factory=dict)


class RunResponse(BaseModel):
    run_id: str
    project_id: str = "default"
    session_id: str = "default"
    query: str = ""
    status: str = "queued"
    success: Optional[bool] = None
    engine: str = "sequential"
    stage: str = ""
    intent: Optional[str] = None
    plan: List[str] = Field(default_factory=list)
    research_plan: Dict[str, Any] = Field(default_factory=dict)
    tasks: List[Dict[str, Any]] = Field(default_factory=list)
    sources: List[Dict[str, Any]] = Field(default_factory=list)
    citations: List[Dict[str, Any]] = Field(default_factory=list)
    started_at: float = 0.0
    ended_at: Optional[float] = None
    answer: str = ""
    verdict: str = ""
    cited_nodes: List[str] = Field(default_factory=list)
    packed_node_ids: List[str] = Field(default_factory=list)
    excluded_node_ids: List[str] = Field(default_factory=list)
    alerts: List[Dict[str, Any]] = Field(default_factory=list)
    validations: List[Dict[str, Any]] = Field(default_factory=list)
    context_usage: Dict[str, Any] = Field(default_factory=dict)
    external_search: Optional[Dict[str, Any]] = None
    errors: List[str] = Field(default_factory=list)
    degraded: bool = False
    repairs: int = 0
    events: List[Dict[str, Any]] = Field(default_factory=list)
    graph_nodes: int = 0
    graph_edges: int = 0


class RunListResponse(BaseModel):
    runs: List[RunResponse] = Field(default_factory=list)


class ModelUsageResponse(BaseModel):
    """Cumulative model gateway usage for this process."""

    enabled: bool = False
    provider: Optional[str] = None
    model: Optional[str] = None
    usage: Dict[str, Any] = Field(default_factory=dict)
    by_model: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
