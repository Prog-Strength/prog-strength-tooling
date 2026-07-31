"""CloudWatch Logs access for the `pst logs` commands.

Searches the per-service log groups that prog-strength-infra creates
(`/prog-strength/{api,agent,mcp}`) for lines mentioning a request id, and
returns them as normalized LogRecords ready to render.

Why FilterLogEvents rather than Logs Insights: it is billed as a plain API
call instead of per-GB-scanned, it returns synchronously (no start-query /
poll / get-results loop), and a quoted filter pattern is a literal substring
match — which matches the api's JSON lines and the agent/mcp's text lines
identically, with no per-format query. Insights would only pay off if we
wanted server-side field extraction or aggregation, which a single-request
lookup does not.

Boto3 errors are translated into CloudWatchError with operator-facing
guidance: the three ways this realistically fails (no credentials, no
permission, wrong region) all look like "it didn't work" from the raw
exception, and the fix for each is different.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .config import ENV_AWS_PROFILE, LogsConfig
from .logparse import LogRecord, parse
from .window import Window

#: Longest request id we will send to CloudWatch. Ids are normally 32 hex
#: chars, but the api's middleware adopts *any* inbound X-Request-ID, so an
#: id set by a client or proxy can be an arbitrary string. This only guards
#: against pathological input, it does not impose a format.
MAX_REQUEST_ID_LEN = 256

#: Characters that would break out of the quoted filter pattern or span
#: lines. Rejected up front rather than escaped, since no real id has them.
_FORBIDDEN_ID_CHARS = frozenset('"\\\n\r\t ')


class CloudWatchError(Exception):
    """A CloudWatch query failed, with a message written for an operator."""


class InvalidRequestIDError(ValueError):
    """The supplied request id can't be used as a search term."""


@dataclass
class SearchOutcome:
    """Everything the renderer needs about one request-id search."""

    records: list[LogRecord] = field(default_factory=list)
    #: Service -> number of matching lines, including services with zero.
    counts: dict[str, int] = field(default_factory=dict)
    #: True when --limit cut the merged timeline short. Surfaced to the
    #: operator: a silently truncated timeline reads as a complete one.
    truncated: bool = False


def validate_request_id(request_id: str) -> str:
    """Normalize and sanity-check a request id.

    Deliberately does NOT enforce the 32-hex shape the services mint: the
    api's requestid middleware honours any inbound X-Request-ID header, so a
    client- or proxy-supplied id is valid and is exactly the case this command
    exists for. Only the characters that would corrupt the query are rejected.
    """
    cleaned = request_id.strip().strip('"').strip("'")
    if not cleaned:
        raise InvalidRequestIDError("request id is empty.")
    if len(cleaned) > MAX_REQUEST_ID_LEN:
        raise InvalidRequestIDError(
            f"request id is {len(cleaned)} characters; the maximum is {MAX_REQUEST_ID_LEN}."
        )
    bad = sorted(set(cleaned) & _FORBIDDEN_ID_CHARS)
    if bad:
        raise InvalidRequestIDError(
            f"request id contains unsupported character(s): {' '.join(repr(c) for c in bad)}."
        )
    return cleaned


def _build_client(cfg: LogsConfig):
    """Create a CloudWatch Logs client, translating setup failures.

    boto3 is imported lazily so that `pst status`, `pst memory`, and even
    `pst --help` don't pay its import cost (a few hundred ms) for commands
    that never touch AWS.
    """
    import boto3
    from botocore.exceptions import BotoCoreError, ProfileNotFound

    try:
        session = boto3.Session(profile_name=cfg.profile, region_name=cfg.region)
        return session.client("logs")
    except ProfileNotFound as exc:
        raise CloudWatchError(
            f"AWS profile {cfg.profile!r} not found. Check ~/.aws/credentials, "
            f"or drop --profile to use the default credential chain."
        ) from exc
    except BotoCoreError as exc:
        raise CloudWatchError(f"could not initialize an AWS client: {exc}") from exc


def _describe_failure(exc: Exception, group: str, cfg: LogsConfig) -> str:
    """Turn a botocore exception into something an operator can act on."""
    from botocore.exceptions import ClientError, NoCredentialsError, PartialCredentialsError

    profile_hint = (
        f"--profile <name> or {ENV_AWS_PROFILE}=<name>"
        if not cfg.profile
        else f"profile {cfg.profile!r}"
    )

    if isinstance(exc, (NoCredentialsError, PartialCredentialsError)):
        return (
            "no usable AWS credentials. This command reads CloudWatch with your own "
            f"AWS identity (not a PST_TOKEN) — supply one with {profile_hint}."
        )

    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"AccessDeniedException", "AccessDenied", "UnauthorizedException"}:
            return (
                f"access denied reading {group}. The identity behind {profile_hint} needs "
                f"logs:FilterLogEvents on that group in {cfg.region}."
            )
        if code == "ResourceNotFoundException":
            return (
                f"log group {group} does not exist in {cfg.region}. Either the service has "
                f"never shipped a log line, or the region is wrong."
            )
        if code in {"ExpiredTokenException", "ExpiredToken", "InvalidClientTokenId"}:
            return f"AWS credentials for {profile_hint} have expired — refresh them and retry."
        if code == "ThrottlingException":
            return f"CloudWatch throttled the query for {group}. Retry in a moment."
        return f"CloudWatch rejected the query for {group}: {exc}"

    return f"could not query {group}: {exc}"


def _search_group(client, service: str, group: str, request_id: str, window: Window, limit: int):
    """Fetch matching events from one log group.

    The quoted filter pattern is a literal, case-sensitive substring match
    server-side; the client-side re-check is belt-and-braces so a pattern
    surprise can never widen the result into unrelated requests.

    Botocore exceptions propagate as-is; the caller owns translating them,
    since only it knows which group and profile were in play.
    """
    records: list[LogRecord] = []
    paginator = client.get_paginator("filter_log_events")
    pages = paginator.paginate(
        logGroupName=group,
        startTime=window.start_ms,
        endTime=window.end_ms,
        filterPattern=f'"{request_id}"',
        # Stop paging once we have enough. One MORE than the limit, so the
        # merge step can still tell "exactly limit lines exist" from "there
        # were more and we cut them" — capping at exactly `limit` here would
        # make every truncated result look complete.
        PaginationConfig={"MaxItems": limit + 1},
    )

    for page in pages:
        for event in page.get("events", []):
            message = event.get("message", "")
            if request_id not in message:
                continue
            records.append(
                parse(
                    service=service,
                    timestamp=datetime.fromtimestamp(event["timestamp"] / 1000, tz=UTC),
                    message=message,
                    stream=event.get("logStreamName", ""),
                )
            )

    return records


def search(cfg: LogsConfig, request_id: str, window: Window, limit: int) -> SearchOutcome:
    """Search every configured log group for a request id, merged by time.

    Groups are queried concurrently — a request id normally lives in exactly
    one of them (the services accept an inbound X-Request-ID but don't forward
    one, so ids do not propagate between them today), which means the other
    queries are pure latency. Fanning out is what saves the operator from
    having to know which service minted the id.

    A failure against one group fails the whole command rather than being
    swallowed: a partial timeline that looks complete is worse than an error,
    because "no lines from mcp" would read as evidence.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    client = _build_client(cfg)
    groups = list(cfg.log_groups.items())

    def run(item: tuple[str, str]) -> tuple[str, list[LogRecord]]:
        service, group = item
        try:
            return service, _search_group(client, service, group, request_id, window, limit)
        except (ClientError, BotoCoreError) as exc:
            raise CloudWatchError(_describe_failure(exc, group, cfg)) from exc

    # boto3 clients are safe to call from multiple threads; only creating them
    # is not, and that already happened above.
    with ThreadPoolExecutor(max_workers=max(len(groups), 1)) as pool:
        results = list(pool.map(run, groups))

    counts = {service: len(records) for service, records in results}
    merged = [record for _, records in results for record in records]
    merged.sort(key=lambda r: r.timestamp)

    truncated = len(merged) > limit
    return SearchOutcome(records=merged[:limit], counts=counts, truncated=truncated)
