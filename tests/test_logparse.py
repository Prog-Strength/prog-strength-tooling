"""Normalizing the four log-line shapes the three services actually emit.

Pure functions, no mocking needed — which is the point of keeping parsing out
of the CloudWatch layer.
"""

import json
from datetime import UTC, datetime

from prog_strength_tooling.logparse import UNKNOWN_LEVEL, parse

TS = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)
RID = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"


def _parse(message, service="api"):
    return parse(service=service, timestamp=TS, message=message, stream="stream-1")


def test_go_slog_json_line():
    line = json.dumps(
        {
            "time": "2026-07-30T12:00:00.123Z",
            "level": "ERROR",
            "msg": "nutrition lookup failed",
            "request_id": RID,
            "user_id": "u-42",
            "err": "upstream timeout",
        }
    )
    record = _parse(line)
    assert record.level == "ERROR"
    assert "nutrition lookup failed" in record.message
    # slog attributes beyond the reserved keys carry the diagnosis — they must
    # survive into the rendered message.
    assert "user_id=u-42" in record.message
    assert "err=upstream timeout" in record.message
    # request_id is reserved: it's the search term, not new information.
    assert "request_id=" not in record.message
    assert record.raw == line


def test_go_slog_json_without_extras():
    line = json.dumps({"time": "2026-07-30T12:00:00Z", "level": "INFO", "msg": "served"})
    record = _parse(line)
    assert record.level == "INFO"
    assert record.message == "served"


def test_python_text_line_from_agent():
    line = f"2026-07-30 12:00:00,123 INFO uvicorn.access [request_id={RID}] GET /chat 200"
    record = _parse(line, service="agent")
    assert record.level == "INFO"
    assert record.logger == "uvicorn.access"
    assert record.message == "GET /chat 200"
    assert record.service == "agent"


def test_python_text_line_keeps_multiline_traceback():
    line = (
        f"2026-07-30 12:00:00,123 ERROR prog_strength_mcp.server [request_id={RID}] boom\n"
        "Traceback (most recent call last):\n"
        '  File "x.py", line 1, in <module>'
    )
    record = _parse(line, service="mcp")
    assert record.level == "ERROR"
    assert record.message.startswith("boom")
    assert "Traceback" in record.message


def test_go_stdlib_printf_line_strips_timestamp_prefix():
    line = f"2026/07/30 12:00:00 activity import: request_id={RID} outcome=storage_failed"
    record = _parse(line)
    # No level in this format — it must still come through readable.
    assert record.level == UNKNOWN_LEVEL
    assert record.message.startswith("activity import:")
    assert "outcome=storage_failed" in record.message


def test_unrecognized_line_survives_intact():
    line = "some proxy wrote this with no structure at all"
    record = _parse(line)
    assert record.level == UNKNOWN_LEVEL
    assert record.message == line


def test_malformed_json_falls_back_instead_of_raising():
    line = '{"level":"ERROR","msg":"truncated'
    record = _parse(line)
    assert record.level == UNKNOWN_LEVEL
    assert record.message == line


def test_json_array_is_not_treated_as_a_record():
    record = _parse('["not", "an", "object"]')
    assert record.level == UNKNOWN_LEVEL


def test_timestamp_comes_from_cloudwatch_not_the_message():
    # The in-message clock says 09:00 but CloudWatch's event time is 12:00.
    # Ordering must follow CloudWatch — it's the only clock all three share.
    line = json.dumps({"time": "2026-07-30T09:00:00Z", "level": "INFO", "msg": "x"})
    assert _parse(line).timestamp == TS


def test_trailing_newline_stripped_from_raw():
    record = _parse("plain line\n")
    assert record.raw == "plain line"
