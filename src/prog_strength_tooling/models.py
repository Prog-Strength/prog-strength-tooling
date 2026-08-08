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
    #: What this memory was distilled from: "chat_session", "workout_note", or
    #: "activity_note" (every endurance sport — run, hike, walk, ride — shares
    #: the last one; api migration 045 keeps the sport in activities.activity_type
    #: rather than denormalizing it into the discriminator).
    source_type: str = ""
    #: The two provenance FKs. Exactly one is populated per row — the api's
    #: schema CHECK ties which one to source_type — and the DTO tags both
    #: omitempty, so the unused one arrives absent rather than null.
    source_session_id: str = ""
    source_workout_id: str = ""
    #: Optional metadata. Backfilled memories (and older API versions) may omit
    #: these entirely, so they default rather than making the whole dump fail
    #: on one sparse row.
    embedding_model: str = ""
    embedding_dim: int = 0
    #: Set when this memory has been superseded by a newer distillation;
    #: None/absent while the row is still the active memory.
    superseded_at: datetime | None = None

    @property
    def source_id(self) -> str:
        """The id this memory traces back to — a chat session or an activity.

        Which FK holds it depends on source_type, so callers that just want
        "the id to look up" shouldn't have to branch on the discriminator.
        Empty for backfilled rows that carry no provenance at all; those
        genuinely have nothing to trace back to.
        """
        return self.source_session_id or self.source_workout_id


class Match(BaseModel):
    """One retrieval hit from the search probe (same path the agent recalls)."""

    text: str
    distance: float
    created_at: datetime
    #: Provenance, mirroring Memory's. May be absent for backfilled memories,
    #: and absent wholesale against an api older than the release that added
    #: source_type/source_workout_id to Match — default rather than crash.
    source_type: str = ""
    source_session_id: str = ""
    source_workout_id: str = ""

    @property
    def source_id(self) -> str:
        """The id this hit traces back to — a chat session or an activity.

        Same contract as Memory.source_id: exactly one FK is populated, so
        callers get "the id to look up" without branching on source_type.
        """
        return self.source_session_id or self.source_workout_id


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
