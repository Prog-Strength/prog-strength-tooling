"""Health probing: up/down classification and per-service summarization.
httpx is mocked at the transport layer with respx."""

import httpx
import respx

from prog_strength_tooling.config import Environment
from prog_strength_tooling.health import check, check_environment


def _health(service, version):
    return httpx.Response(
        200, json={"service": service, "version": version, "message": "service is healthy"}
    )


@respx.mock
def test_check_up_reports_version():
    respx.get("https://api.test/health").mock(
        return_value=_health("Prog Strength Backend", "v1.2.3")
    )
    s = check("api", "https://api.test")
    assert s.up is True
    assert s.version == "v1.2.3"
    assert s.service == "Prog Strength Backend"
    assert s.latency_ms is not None
    assert s.url == "https://api.test/health"


@respx.mock
def test_check_non_200_is_down():
    respx.get("https://api.test/health").mock(return_value=httpx.Response(503))
    s = check("api", "https://api.test")
    assert s.up is False
    assert s.version is None
    assert "503" in s.detail


@respx.mock
def test_check_connection_error_is_down():
    respx.get("https://api.test/health").mock(side_effect=httpx.ConnectError("refused"))
    s = check("api", "https://api.test")
    assert s.up is False
    assert s.latency_ms is None  # never got a response
    assert "refused" in s.detail


@respx.mock
def test_check_environment_probes_all_in_order():
    respx.get("https://api.test/health").mock(return_value=_health("api", "v1"))
    respx.get("https://agent.test/health").mock(return_value=_health("agent", "v2"))
    respx.get("https://mcp.test/health").mock(return_value=httpx.Response(500))
    env = Environment(
        name="test",
        services={
            "api": "https://api.test",
            "agent": "https://agent.test",
            "mcp": "https://mcp.test",
        },
    )
    results = check_environment(env)
    assert [r.name for r in results] == ["api", "agent", "mcp"]  # SERVICES order
    assert [r.up for r in results] == [True, True, False]
