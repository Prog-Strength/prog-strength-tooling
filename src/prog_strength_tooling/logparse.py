"""Parse a raw CloudWatch log message into a displayable record.

The three services do not agree on a log format, because they were never
meant to be read together:

  api    JSON from Go's slog — {"time":…,"level":…,"msg":…,"request_id":…}
  api    plus unstructured `log.Printf` lines ("2026/07/30 12:00:00 activity
         import: request_id=… outcome=imported") that predate slog
  agent  text from Python logging —
  mcp    "2026-07-30 12:00:00,123 INFO uvicorn.access [request_id=…] GET /chat"

This module normalizes all of them to one LogRecord so a single merged
timeline is readable. Pure functions, no I/O: every format quirk is a cheap
unit test.

Parsing never raises. A line this module can't read is still a line the
operator needs to see — it comes back with level "-" and the body intact,
which is strictly better than dropping it or crashing the whole dump.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime

#: Python logging's default asctime + the format the agent/mcp configure:
#: "%(asctime)s %(levelname)s %(name)s [request_id=%(request_id)s] %(message)s".
#: DOTALL so a multi-line traceback stays attached to its own record.
_TEXT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:[,.]\d+)?\s+"
    r"(?P<level>[A-Z]+)\s+"
    r"(?P<logger>\S+)\s+"
    r"\[request_id=(?P<request_id>[^\]]*)\]\s*"
    r"(?P<message>.*)$",
    re.DOTALL,
)

#: Go's `log.Printf` default flags (LstdFlags) prefix "2026/07/30 12:00:00 ".
#: Stripped so these lines line up with the rest of the timeline, which is
#: ordered by CloudWatch's own timestamp anyway.
_GO_STDLIB_RE = re.compile(
    r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?\s+(?P<message>.*)$", re.DOTALL
)

#: slog keys already surfaced as their own LogRecord field — the rest of a
#: JSON record's attributes get appended to the message so nothing is lost.
_SLOG_RESERVED = {"time", "level", "msg", "request_id"}

#: Level shown for a line whose format we could not read.
UNKNOWN_LEVEL = "-"


@dataclass(frozen=True)
class LogRecord:
    """One normalized log line, ready to render."""

    service: str
    #: CloudWatch's own event timestamp, not the one inside the message. It is
    #: the only clock all three services share, so it is what the merged
    #: timeline sorts by — the in-message timestamps differ in format and
    #: precision, and one of the four line shapes has none at all.
    timestamp: datetime
    level: str
    message: str
    #: The original line, verbatim, for --raw and --json.
    raw: str
    #: Emitting logger ("uvicorn.access"), when the format carries one.
    logger: str = ""
    #: CloudWatch log stream — the container id that produced the line.
    stream: str = ""


def parse(service: str, timestamp: datetime, message: str, stream: str = "") -> LogRecord:
    """Normalize one raw CloudWatch message into a LogRecord.

    Tries each known shape in turn and falls back to the whole line. Never
    raises: see the module docstring.
    """
    raw = message.rstrip("\n")

    for reader in (_read_json, _read_text, _read_go_stdlib):
        parsed = reader(raw)
        if parsed is not None:
            level, body, logger = parsed
            return LogRecord(
                service=service,
                timestamp=timestamp,
                level=level,
                message=body,
                raw=raw,
                logger=logger,
                stream=stream,
            )

    return LogRecord(
        service=service,
        timestamp=timestamp,
        level=UNKNOWN_LEVEL,
        message=raw,
        raw=raw,
        stream=stream,
    )


def _read_json(raw: str) -> tuple[str, str, str] | None:
    """Go slog's JSON output. Returns (level, message, logger) or None."""
    stripped = raw.lstrip()
    if not stripped.startswith("{"):
        return None
    try:
        payload = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None

    message = str(payload.get("msg", ""))
    # Everything slog attached beyond the reserved keys — user_id, outcome,
    # err, and friends. These carry most of the diagnostic value, so they are
    # appended rather than dropped.
    extras = " ".join(
        f"{key}={_scalar(value)}" for key, value in payload.items() if key not in _SLOG_RESERVED
    )
    body = f"{message} {extras}".strip() if extras else message
    return str(payload.get("level", UNKNOWN_LEVEL)), body, ""


def _read_text(raw: str) -> tuple[str, str, str] | None:
    """Python logging output from the agent and mcp."""
    match = _TEXT_RE.match(raw)
    if not match:
        return None
    return match.group("level"), match.group("message"), match.group("logger")


def _read_go_stdlib(raw: str) -> tuple[str, str, str] | None:
    """The api's remaining unstructured `log.Printf` lines."""
    match = _GO_STDLIB_RE.match(raw)
    if not match:
        return None
    return UNKNOWN_LEVEL, match.group("message"), ""


def _scalar(value: object) -> str:
    """Render a slog attribute value compactly for the message tail."""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return json.dumps(value)
    return json.dumps(value, separators=(",", ":"))
