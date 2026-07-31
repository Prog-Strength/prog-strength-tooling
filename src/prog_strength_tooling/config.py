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

Commands that hit an admin-gated endpoint call `resolve_admin` instead of
`resolve`, which fails fast with MISSING_TOKEN_MESSAGE before any request is
made. The check lives here so every current and future admin command gets the
same up-front error, and so the guidance text has one home.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .logsetup import get_logger, kv, redact

log = get_logger(__name__)

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
ENV_AWS_PROFILE = "PST_AWS_PROFILE"

#: Region hosting the CloudWatch log groups (prog-strength-infra deploys the
#: whole stack to us-east-2). Pinned rather than left to the AWS default
#: chain: an operator's default region is often us-east-1, and querying the
#: wrong region returns an empty result rather than an error — silence that
#: looks exactly like "this request id doesn't exist".
AWS_REGION = "us-east-2"

#: Log groups are named /prog-strength/<service> by prog-strength-infra's
#: modules/logging, one per docker-compose service shipping via the awslogs
#: driver. The same naming rule applies to any service added there later.
LOG_GROUP_PREFIX = "/prog-strength"

#: Environments whose services ship logs to CloudWatch. Local containers log
#: to stdout only, so there is nothing to query — the logs command says so
#: rather than returning a confusing empty result.
CLOUDWATCH_ENVIRONMENTS = frozenset({"prod"})


#: Shown whenever an admin-gated command runs without a token. It answers both
#: questions an operator returning to the CLI has — how do I pass a token, and
#: where do I get one — so the failure is self-service. Keep lines under ~72
#: chars: they render inside a bordered panel on an 80-col terminal.
MISSING_TOKEN_MESSAGE = f"""\
This command calls the admin-gated API endpoints, so it needs a Prog
Strength admin token: a normal user JWT whose email is in the API's
admin allowlist (the same token memctl uses).

Supply one, either way:

  export {ENV_TOKEN}=<admin-jwt>     once per shell
  pst ... --token <admin-jwt>      per command

Don't have one? Get a JWT for an allowlisted account:

  prod    sign in to the Prog Strength web app as an admin, then copy
          localStorage.ps_access_token from the browser devtools.

  local   with DEV_AUTH=true on the api:
          curl -sX POST {NAMED_ENVIRONMENTS["local"]["api"]}/auth/dev/token \\
               -H 'Content-Type: application/json' \\
               -d '{{"email": "you@example.com"}}'"""


class ConfigError(Exception):
    """Invalid configuration, e.g. an unknown environment name."""


class MissingTokenError(ConfigError):
    """An admin-gated command ran with no token from flag or env.

    A ConfigError subclass so existing command-level handlers catch it; the
    CLI renders it specially because the guidance is multi-line.
    """

    def __init__(self, message: str = MISSING_TOKEN_MESSAGE) -> None:
        super().__init__(message)


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
    log.debug(
        "resolving environment %s",
        kv(flag=env, env_var=os.getenv(ENV_ENV), default=DEFAULT_ENVIRONMENT, chosen=name),
    )
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
    log.debug(
        "resolved api config %s",
        kv(
            api_flag=api,
            env_flag=env,
            api_env_var=os.getenv(ENV_API_URL),
            api_url=api_url,
            token=redact(resolved_token),
            token_source=("flag" if token else "env" if os.getenv(ENV_TOKEN) else "none"),
        ),
    )
    log.info("api %s", kv(url=api_url))
    return Config(api_url=api_url, token=resolved_token)


@dataclass(frozen=True)
class LogsConfig:
    """Resolved settings for a CloudWatch log query."""

    environment: str
    region: str
    #: AWS named profile, or None to use boto3's default credential chain.
    profile: str | None
    #: Service name -> log group name, in SERVICES display order.
    log_groups: dict[str, str]


def log_group_for(service: str) -> str:
    """Log group name for a service, per the infra naming convention."""
    return f"{LOG_GROUP_PREFIX}/{service}"


def resolve_logs(
    env: str | None,
    services: list[str] | tuple[str, ...] | None = None,
    profile: str | None = None,
    region: str | None = None,
) -> LogsConfig:
    """Resolve which log groups to query, in which region, as whom.

    Unlike the memory commands this needs no admin JWT — CloudWatch is reached
    with the operator's own AWS credentials, not the API's auth. (The EC2
    instance role is deliberately write-only; reads are an operator action.)

    Raises ConfigError for an environment that ships no logs, or an unknown
    service name, so both fail before any AWS call goes out.
    """
    environment = resolve_environment(env)
    if environment.name not in CLOUDWATCH_ENVIRONMENTS:
        shippers = ", ".join(sorted(CLOUDWATCH_ENVIRONMENTS))
        raise ConfigError(
            f"environment {environment.name!r} does not ship logs to CloudWatch "
            f"(only: {shippers}). Local containers log to stdout — "
            f"use `docker compose logs` instead."
        )

    selected = tuple(services) if services else SERVICES
    unknown = [s for s in selected if s not in SERVICES]
    if unknown:
        valid = ", ".join(SERVICES)
        raise ConfigError(f"unknown service {unknown[0]!r}. Valid: {valid}.")

    # Preserve SERVICES order regardless of flag order, so output is stable.
    chosen = set(selected)
    resolved_profile = profile or os.getenv(ENV_AWS_PROFILE) or None
    resolved_region = region or AWS_REGION
    log.info(
        "cloudwatch %s",
        kv(
            env=environment.name,
            region=resolved_region,
            profile=resolved_profile or "default-chain",
            groups=",".join(log_group_for(s) for s in SERVICES if s in chosen),
        ),
    )
    return LogsConfig(
        environment=environment.name,
        region=resolved_region,
        profile=resolved_profile,
        log_groups={s: log_group_for(s) for s in SERVICES if s in chosen},
    )


def resolve_admin(api: str | None, token: str | None, env: str | None = None) -> Config:
    """resolve() for admin-gated commands — no token is a hard, up-front error.

    Every command that hits an admin endpoint resolves through here, so the
    operator learns a token is required (and how to get one) before a request
    goes out, instead of via a 401 from the server.
    """
    cfg = resolve(api, token, env)
    if not cfg.token:
        raise MissingTokenError()
    return cfg
