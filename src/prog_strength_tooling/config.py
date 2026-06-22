"""Resolve the API base URL and admin token from flags, then environment.

The base URL is chosen from a registry of named environments
(NAMED_ENVIRONMENTS) so adding a new environment later is a one-line entry —
the --env flag, PST_ENV, and help text all read from the registry. Selection
precedence, highest first:

  1. --api <url>          explicit URL override (flag)
  2. --env <name>         named environment (flag)
  3. PST_API_URL          explicit URL override (env var)
  4. PST_ENV              named environment (env var)
  5. DEFAULT_ENVIRONMENT  the built-in default (production)

All resolution happens here rather than via Typer's envvar= so the precedence
above is the single source of truth and stays testable without the CLI.

The token has no default: the admin endpoints are gated, so a missing token is
surfaced as a clear error at call time rather than guessed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: Known environments: name -> API base URL. Add a new environment by adding a
#: line here; the --env flag, PST_ENV, and CLI help all pick it up automatically.
NAMED_ENVIRONMENTS: dict[str, str] = {
    "prod": "https://api.progstrength.fitness",
    "local": "http://localhost:8080",
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
    """Resolved connection settings shared by every command."""

    api_url: str
    #: Admin JWT — a normal user token whose email is in the API admin
    #: allowlist. None when neither flag nor env supplied one.
    token: str | None

    @property
    def base_url(self) -> str:
        """API base with any trailing slash stripped, for clean joins."""
        return self.api_url.rstrip("/")


def _lookup_env(name: str | None) -> str | None:
    """Map a named environment to its URL; None when name is unset.

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


def resolve(api: str | None, token: str | None, env: str | None = None) -> Config:
    """Build a Config following the documented flag → env → default precedence."""
    api_url = (
        api
        or _lookup_env(env)
        or os.getenv(ENV_API_URL)
        or _lookup_env(os.getenv(ENV_ENV))
        or NAMED_ENVIRONMENTS[DEFAULT_ENVIRONMENT]
    )
    resolved_token = token or os.getenv(ENV_TOKEN) or None
    return Config(api_url=api_url, token=resolved_token)
