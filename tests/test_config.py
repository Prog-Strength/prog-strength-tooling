"""Config resolution: named-environment registry + flag → env → default
precedence, and the unknown-environment error."""

import pytest

from prog_strength_tooling.config import (
    NAMED_ENVIRONMENTS,
    ConfigError,
    MissingTokenError,
    resolve,
    resolve_admin,
    resolve_logs,
)
from prog_strength_tooling.logsetup import redact

PROD = NAMED_ENVIRONMENTS["prod"]["api"]
LOCAL = NAMED_ENVIRONMENTS["local"]["api"]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Each test starts with no PST_* vars set."""
    for var in ("PST_API_URL", "PST_ENV", "PST_TOKEN"):
        monkeypatch.delenv(var, raising=False)


def test_default_is_production():
    cfg = resolve(api=None, token=None)
    assert cfg.api_url == PROD == "https://api.progstrength.fitness"
    assert cfg.token is None


def test_env_flag_selects_named_environment():
    cfg = resolve(api=None, token=None, env="local")
    assert cfg.api_url == LOCAL


def test_pst_env_var_selects_named_environment(monkeypatch):
    monkeypatch.setenv("PST_ENV", "local")
    assert resolve(api=None, token=None).api_url == LOCAL


def test_pst_api_url_var_overrides_default(monkeypatch):
    monkeypatch.setenv("PST_API_URL", "http://staging.example.com")
    assert resolve(api=None, token=None).api_url == "http://staging.example.com"


def test_api_flag_overrides_everything(monkeypatch):
    monkeypatch.setenv("PST_API_URL", "http://staging.example.com")
    monkeypatch.setenv("PST_ENV", "local")
    cfg = resolve(api="http://flag.example.com", token=None, env="prod")
    assert cfg.api_url == "http://flag.example.com"


def test_env_flag_beats_pst_api_url_var(monkeypatch):
    # Flag-based selection outranks an env-var URL (precedence rule 2 > 3).
    monkeypatch.setenv("PST_API_URL", "http://staging.example.com")
    assert resolve(api=None, token=None, env="local").api_url == LOCAL


def test_pst_api_url_beats_pst_env(monkeypatch):
    # Explicit URL env var outranks named-env env var (rule 3 > 4).
    monkeypatch.setenv("PST_API_URL", "http://staging.example.com")
    monkeypatch.setenv("PST_ENV", "local")
    assert resolve(api=None, token=None).api_url == "http://staging.example.com"


def test_unknown_env_flag_raises():
    with pytest.raises(ConfigError) as exc:
        resolve(api=None, token=None, env="nope")
    assert "nope" in str(exc.value)
    assert "prod" in str(exc.value)  # lists valid names


def test_unknown_pst_env_var_raises(monkeypatch):
    monkeypatch.setenv("PST_ENV", "nope")
    with pytest.raises(ConfigError):
        resolve(api=None, token=None)


def test_token_from_flag_and_env(monkeypatch):
    assert resolve(api=None, token="flag-tok").token == "flag-tok"
    monkeypatch.setenv("PST_TOKEN", "env-tok")
    assert resolve(api=None, token=None).token == "env-tok"


def test_base_url_strips_trailing_slash():
    cfg = resolve(api="http://api.example.com/", token="t")
    assert cfg.base_url == "http://api.example.com"


# --- resolve_admin (the token gate on admin-only commands) --------------


def test_resolve_admin_requires_a_token():
    with pytest.raises(MissingTokenError):
        resolve_admin(api=None, token=None)


def test_missing_token_error_is_a_config_error():
    # Command handlers catch ConfigError; the gate must not slip past them.
    with pytest.raises(ConfigError):
        resolve_admin(api=None, token=None)


def test_missing_token_message_says_how_to_supply_and_obtain_one():
    with pytest.raises(MissingTokenError) as exc:
        resolve_admin(api=None, token=None)
    message = str(exc.value)
    assert "PST_TOKEN" in message  # how to supply it
    assert "--token" in message
    assert "admin allowlist" in message  # what kind of token
    assert "/auth/dev/token" in message  # how to obtain one


def test_resolve_admin_accepts_flag_or_env_token(monkeypatch):
    assert resolve_admin(api=None, token="flag-tok").token == "flag-tok"
    monkeypatch.setenv("PST_TOKEN", "env-tok")
    cfg = resolve_admin(api=None, token=None, env="local")
    assert cfg.token == "env-tok"
    assert cfg.api_url == LOCAL  # still follows the normal URL precedence


def test_resolve_admin_reports_unknown_env_before_missing_token():
    # A bad --env is the more specific error; don't mask it with the token gate.
    with pytest.raises(ConfigError) as exc:
        resolve_admin(api=None, token=None, env="nope")
    assert not isinstance(exc.value, MissingTokenError)
    assert "nope" in str(exc.value)


# --- resolve_environment (used by `pst status`) -------------------------


def test_resolve_environment_default_is_prod():
    from prog_strength_tooling.config import resolve_environment

    env = resolve_environment(None)
    assert env.name == "prod"
    assert env.services["api"] == PROD
    assert set(env.services) == {"api", "agent", "mcp"}


def test_resolve_environment_flag_and_pst_env(monkeypatch):
    from prog_strength_tooling.config import resolve_environment

    assert resolve_environment("local").name == "local"
    monkeypatch.setenv("PST_ENV", "local")
    assert resolve_environment(None).name == "local"


def test_resolve_environment_unknown_raises():
    from prog_strength_tooling.config import resolve_environment

    with pytest.raises(ConfigError):
        resolve_environment("staging")


def test_resolve_logs_logs_the_environment_at_info(caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="prog_strength_tooling"):
        resolve_logs("prod", None, "my-profile", None)

    line = " ".join(r.getMessage() for r in caplog.records)
    assert "env=prod" in line
    assert "region=us-east-2" in line
    assert "profile=my-profile" in line


def test_resolve_admin_never_logs_the_token(caplog):
    import logging

    secret = "eyJhbGciOiJIUzI1NiJ9.super-secret-payload.signature"
    with caplog.at_level(logging.DEBUG, logger="prog_strength_tooling"):
        resolve_admin(None, secret, "prod")

    messages = [record.getMessage() for record in caplog.records]
    for message in messages:
        assert secret not in message
        assert "super-secret-payload" not in message

    # The negative loop above is trivially satisfied if resolve() logged
    # nothing at all about the token -- which it would if the log.debug call
    # in config.resolve were ever deleted. Prove the redacted form was
    # actually logged, not merely absent, so this test can fail for the
    # right reason.
    redacted = redact(secret)
    assert any("token=" in message and redacted in message for message in messages)
