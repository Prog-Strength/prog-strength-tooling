"""Health checks for the backend services via their GET /health endpoints.

All three services (api, agent, mcp) expose an identical, unauthenticated
/health that returns {"service", "version", "message"} with HTTP 200. This
module probes one service and summarizes the result as a ServiceStatus the
renderer can display.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from .config import SERVICES, Environment
from .logsetup import get_logger, kv

log = get_logger(__name__)


@dataclass(frozen=True)
class ServiceStatus:
    """The outcome of probing one service's /health endpoint."""

    name: str  # the configured service key (api/agent/mcp)
    url: str  # the /health URL probed
    up: bool
    version: str | None  # self-reported version, from the health body when up
    service: str | None  # the service's self-reported name
    latency_ms: float | None
    detail: str  # "service is healthy", or the error/reason when down


def check(name: str, base_url: str, timeout: float = 5.0) -> ServiceStatus:
    """Probe one service's /health endpoint and summarize the result.

    Up means HTTP 200 with a JSON body; anything else (timeout, connection
    error, non-200, non-JSON) is reported as down with a human-readable reason
    rather than raised — a status sweep should always render a full table.
    """
    url = base_url.rstrip("/") + "/health"
    log.debug("probing %s", kv(service=name, url=url, timeout_s=timeout))
    start = time.monotonic()
    try:
        resp = httpx.get(url, timeout=timeout)
    except httpx.HTTPError as exc:
        return ServiceStatus(name, url, False, None, None, None, str(exc))
    latency_ms = (time.monotonic() - start) * 1000

    if resp.status_code != 200:
        return ServiceStatus(name, url, False, None, None, latency_ms, f"HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        return ServiceStatus(name, url, False, None, None, latency_ms, "non-JSON health body")

    log.debug(
        "probe ok %s",
        kv(service=name, version=body.get("version"), latency_ms=round(latency_ms, 1)),
    )
    return ServiceStatus(
        name=name,
        url=url,
        up=True,
        version=body.get("version"),
        service=body.get("service"),
        latency_ms=latency_ms,
        detail=body.get("message", ""),
    )


def check_environment(env: Environment, timeout: float = 5.0) -> list[ServiceStatus]:
    """Probe every service the environment defines, in SERVICES display order."""
    return [check(name, env.services[name], timeout) for name in SERVICES if name in env.services]
