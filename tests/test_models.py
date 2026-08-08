"""Model resilience: the dump/search shapes must tolerate optional metadata
fields being absent. Production returns memories (e.g. backfilled ones) with
no source_session_id and no embedding metadata; an inspection tool must render
them, not crash the whole dump on one sparse row."""

from prog_strength_tooling.models import Match, Memory, MemoryList, SearchResult


def test_memory_without_source_session_id():
    # Exact shape that crashed against production: no source_session_id.
    m = Memory.model_validate(
        {
            "distilled_text": "Has benched 225 for reps",
            "user_id": "u1",
            "embedding_model": "voyage-3",
            "embedding_dim": 1024,
            "created_at": "2026-06-22T04:51:58.782966505Z",
        }
    )
    assert m.source_session_id == ""


def test_memory_with_only_core_fields():
    # The bare minimum an inspection tool should still render.
    m = Memory.model_validate(
        {"distilled_text": "x", "user_id": "u1", "created_at": "2026-06-22T04:51:58Z"}
    )
    assert m.source_session_id == ""
    assert m.embedding_model == ""
    assert m.embedding_dim == 0
    # No provenance at all — the row still parses, and source_id says so
    # rather than inventing an id the operator would then try to look up.
    assert m.source_type == ""
    assert m.source_workout_id == ""
    assert m.source_id == ""


def test_chat_memory_traces_back_to_its_session():
    m = Memory.model_validate(
        {
            "distilled_text": "prefers morning sessions",
            "user_id": "u1",
            "source_type": "chat_session",
            "source_session_id": "9f2a",
            "created_at": "2026-06-22T04:51:58Z",
        }
    )
    assert m.source_type == "chat_session"
    assert m.source_id == "9f2a"


def test_workout_memory_traces_back_to_its_activity():
    # The API omits source_session_id entirely for note-sourced rows (the
    # schema CHECK forbids both FKs being set), so source_id must fall
    # through to the activity id rather than reporting "-".
    m = Memory.model_validate(
        {
            "distilled_text": "left shoulder cranky on presses",
            "user_id": "u1",
            "source_type": "workout_note",
            "source_workout_id": "31bc",
            "created_at": "2026-06-22T04:51:58Z",
        }
    )
    assert m.source_type == "workout_note"
    assert m.source_id == "31bc"


def test_activity_note_memory_traces_back_to_its_activity():
    # Runs, hikes, walks and rides all land on 'activity_note' — migration 045
    # keeps the sport in activities.activity_type, not the discriminator.
    m = Memory.model_validate(
        {
            "distilled_text": "hills felt easy",
            "user_id": "u1",
            "source_type": "activity_note",
            "source_workout_id": "77de",
            "created_at": "2026-06-22T04:51:58Z",
        }
    )
    assert m.source_type == "activity_note"
    assert m.source_id == "77de"


def test_memory_list_tolerates_sparse_row():
    result = MemoryList.model_validate(
        {
            "memories": [
                {
                    "distilled_text": "sparse",
                    "user_id": "u1",
                    "created_at": "2026-06-22T04:51:58Z",
                }
            ]
        }
    )
    assert len(result.memories) == 1


def test_match_without_source_session_id():
    s = SearchResult.model_validate(
        {
            "threshold": 0.7,
            "matches": [
                {
                    "text": "benches 225",
                    "distance": 0.42,
                    "created_at": "2026-06-22T04:51:58Z",
                }
            ],
        }
    )
    assert isinstance(s.matches[0], Match)
    assert s.matches[0].source_session_id == ""
    # An api predating provenance on Match omits all three fields at once.
    assert s.matches[0].source_type == ""
    assert s.matches[0].source_id == ""


def test_match_traces_back_to_its_source():
    s = SearchResult.model_validate(
        {
            "threshold": 0.7,
            "matches": [
                {
                    "text": "hills felt easy",
                    "distance": 0.31,
                    "source_type": "activity_note",
                    "source_session_id": "",
                    "source_workout_id": "77de",
                    "created_at": "2026-06-22T04:51:58Z",
                },
                {
                    "text": "prefers mornings",
                    "distance": 0.44,
                    "source_type": "chat_session",
                    "source_session_id": "9f2a",
                    "source_workout_id": "",
                    "created_at": "2026-06-22T04:51:58Z",
                },
            ],
        }
    )
    assert s.matches[0].source_type == "activity_note"
    assert s.matches[0].source_id == "77de"
    assert s.matches[1].source_type == "chat_session"
    assert s.matches[1].source_id == "9f2a"
