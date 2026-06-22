"""Resolve backend service URLs and the admin token from flags, then env.

An *environment* is a named set of backend service base URLs (api, agent,
mcp). The registry NAMED_ENVIRONMENTS makes adding a new environment a
one-line entry — `--env`, `PST_ENV`, and the CLI help all read from it.
Selecting an environment picks the whole set; each command uses the services
it needs (`pst memory` uses the api URL, `pst status` checks all three).

Base-URL precedence for the api (used by the memory commands), highest first:

  1. --api <url>          explicit URL override (flag)
  2. --env <name>         named environment (flag)
  3. PST_API_URL          explicit URL override (env var)
  4. PST_ENV              named environment (env var)
  5. DEFAULT_ENVIRONMENT  the built-in default (production)

All resolution happens here (not via Typer's envvar=) so the precedence above
is the single source of truth and stays testable without the CLI.

The token has no default: the admin endpoints are gated, so a missing token is
surfaced as a clear error at call time rather than guessed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: The backend services every environment defines, in display order.
SERVICES: tuple[str, ...] = ("api", "agent", "mcp")

#: Known environments: name -> {service: base URL}. Add a new environment by
#: adding one entry here; --env, PST_ENV, and CLI help all pick it up.
NAMED_ENVIRONMENTS: dict[str, dict[str, str]] = {
    "prod": {
        "api": "https://api.progstrength.fitness",
        "agent": "https://agent.progstrength.fitness",
        "mcp": "https://mcp.progstrength.fitness",
    },
    "local": {
        "api": "http://localhost:8080",
        "agent": "http://localhost:8001",
        "mcp": "http://localhost:8000",
    },
}

#: Environment used when no flag or env var selects one.
DEFAULT_ENVIRONMENT = "prod"

ENV_API_URL = "PST_API_URL"
ENV_ENV = "PST_ENV"
ENV_TOKEN = "PST_TOKEN"


class ConfigError(Exception):
    """Invalid configuration, e.g. an unknown environment name."""


@dataclass(frozen=True)
class Config:
    """Resolved connection settings for the memory commands."""

    api_url: str
    #: Admin JWT — a normal user token whose email is in the API admin
    #: allowlist. None when neither flag nor env supplied one.
    token: str | None

    @property
    def base_url(self) -> str:
        """API base with any trailing slash stripped, for clean joins."""
        return self.api_url.rstrip("/")


@dataclass(frozen=True)
class Environment:
    """A named set of backend service base URLs."""

    name: str
    services: dict[str, str]


def services_for(name: str | None) -> dict[str, str] | None:
    """Map a named environment to its service URLs; None when name is unset.

    Raises ConfigError for a non-empty name absent from the registry, listing
    the valid names so the operator can correct it.
    """
    if not name:
        return None
    try:
        return NAMED_ENVIRONMENTS[name]
    except KeyError:
        valid = ", ".join(sorted(NAMED_ENVIRONMENTS))
        raise ConfigError(f"unknown environment {name!r}. Valid: {valid}.") from None


def resolve_environment(env: str | None) -> Environment:
    """Resolve the active environment (--env -> PST_ENV -> default) + its URLs."""
    name = env or os.getenv(ENV_ENV) or DEFAULT_ENVIRONMENT
    services = services_for(name)
    assert services is not None  # name is always non-empty here
    return Environment(name=name, services=services)


def resolve(api: str | None, token: str | None, env: str | None = None) -> Config:
    """Build a Config for the memory commands following the documented precedence."""
    api_url = (
        api
        or (services_for(env) or {}).get("api")
        or os.getenv(ENV_API_URL)
        or (services_for(os.getenv(ENV_ENV)) or {}).get("api")
        or NAMED_ENVIRONMENTS[DEFAULT_ENVIRONMENT]["api"]
    )
    resolved_token = token or os.getenv(ENV_TOKEN) or None
    return Config(api_url=api_url, token=resolved_token)
