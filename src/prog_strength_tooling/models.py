"""Typed views of the api admin vector-memory responses.

These mirror the Go DTOs in prog-strength-api:
  - Memory  <- internal/vectormemory.handler memoryDTO (GET /admin/memories)
  - Match   <- internal/vectormemory.Match        (POST /admin/memories/search)

Both endpoints wrap their payload in the standard httpresp envelope
(`{"data": {...}}`), so the list/search responses unwrap `data` before
validating the inner shape.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class Memory(BaseModel):
    """One stored vector memory for a user (an /admin/memories dump row)."""

    distilled_text: str
    user_id: str
    created_at: datetime
    #: Optional provenance/metadata. Backfilled memories (and older API
    #: versions) may omit these entirely, so they default rather than making
    #: the whole dump fail on one sparse row.
    source_session_id: str = ""
    embedding_model: str = ""
    embedding_dim: int = 0
    #: Set when this memory has been superseded by a newer distillation;
    #: None/absent while the row is still the active memory.
    superseded_at: datetime | None = None


class Match(BaseModel):
    """One retrieval hit from the search probe (same path the agent recalls)."""

    text: str
    distance: float
    created_at: datetime
    #: May be absent for backfilled memories — default rather than crash.
    source_session_id: str = ""


class MemoryList(BaseModel):
    """Inner `data` shape of GET /admin/memories."""

    memories: list[Memory] = Field(default_factory=list)


class SearchResult(BaseModel):
    """Inner `data` shape of POST /admin/memories/search.

    `threshold` is the active distance cap the service actually applied —
    the server echoes it so the operator can see the cap in effect even when
    they did not pass --threshold.
    """

    threshold: float
    matches: list[Match] = Field(default_factory=list)


class WhoopConnection(BaseModel):
    """One user's WHOOP admin connection row.

    Mirrors the DTO returned by GET /admin/whoop/connections (as a list under
    `data.connections`) and GET /admin/whoop/connections/{userID} (a single
    object under `data`). `latest_recovery_date` is null until the first
    recovery row is ingested.
    """

    user_id: str
    whoop_user_id: int
    status: str
    scopes: str
    token_expires_at: str
    token_expired: bool
    connected_at: str
    updated_at: str
    latest_recovery_date: str | None = None
    recovery_row_count: int


class WhoopResyncOutcome(BaseModel):
    """Inner `data` shape of POST /admin/whoop/resync — a per-reason tally of
    what the resync did across the requested window."""

    upserted: int
    skipped_unscored: int
    skipped_no_cycle: int
    skipped_bad_date: int
