"""Resolve the API base URL and admin token from flags, then environment.

Every command takes optional --api / --token flags. When a flag is omitted
(None), we fall back to the environment, then to a built-in default for the
URL. The token has no default: the admin endpoints are gated, so a missing
token is surfaced as a clear error at call time rather than guessed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: Fallback base URL when neither --api nor PST_API_URL is set. Matches the
#: api server's local default and memctl's defaultAPI.
DEFAULT_API_URL = "http://localhost:8080"

ENV_API_URL = "PST_API_URL"
ENV_TOKEN = "PST_TOKEN"


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


def resolve(api: str | None, token: str | None) -> Config:
    """Build a Config from explicit flags, falling back to env then defaults."""
    api_url = api or os.getenv(ENV_API_URL) or DEFAULT_API_URL
    resolved_token = token or os.getenv(ENV_TOKEN) or None
    return Config(api_url=api_url, token=resolved_token)
