"""Config resolution: flags win, then env, then the URL default."""

from prog_strength_tooling.config import DEFAULT_API_URL, resolve


def test_defaults_when_nothing_set(monkeypatch):
    monkeypatch.delenv("PST_API_URL", raising=False)
    monkeypatch.delenv("PST_TOKEN", raising=False)
    cfg = resolve(api=None, token=None)
    assert cfg.api_url == DEFAULT_API_URL
    assert cfg.token is None


def test_env_used_when_flags_absent(monkeypatch):
    monkeypatch.setenv("PST_API_URL", "http://api.example.com")
    monkeypatch.setenv("PST_TOKEN", "env-token")
    cfg = resolve(api=None, token=None)
    assert cfg.api_url == "http://api.example.com"
    assert cfg.token == "env-token"


def test_flags_override_env(monkeypatch):
    monkeypatch.setenv("PST_API_URL", "http://api.example.com")
    monkeypatch.setenv("PST_TOKEN", "env-token")
    cfg = resolve(api="http://flag.example.com", token="flag-token")
    assert cfg.api_url == "http://flag.example.com"
    assert cfg.token == "flag-token"


def test_base_url_strips_trailing_slash():
    cfg = resolve(api="http://api.example.com/", token="t")
    assert cfg.base_url == "http://api.example.com"
