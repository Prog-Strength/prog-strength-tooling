"""End-to-end `pst status` tests via Typer's runner, HTTP mocked with respx."""

import httpx
import respx
from typer.testing import CliRunner

from prog_strength_tooling.cli import app

runner = CliRunner()

# The prod environment's real hostnames — respx intercepts them.
API = "https://api.progstrength.fitness/health"
AGENT = "https://agent.progstrength.fitness/health"
MCP = "https://mcp.progstrength.fitness/health"


def _health(service, version):
    return httpx.Response(
        200, json={"service": service, "version": version, "message": "service is healthy"}
    )


@respx.mock
def test_status_all_up_exit_0():
    respx.get(API).mock(return_value=_health("Prog Strength Backend", "v2.1.0"))
    respx.get(AGENT).mock(return_value=_health("prog-strength-agent", "v1.4.0"))
    respx.get(MCP).mock(return_value=_health("prog-strength-mcp", "v1.1.0"))

    result = runner.invoke(app, ["status", "--env", "prod"])
    assert result.exit_code == 0
    assert "UP" in result.stdout
    assert "v2.1.0" in result.stdout
    assert "3/3 up" in result.stdout


@respx.mock
def test_status_any_down_exit_1():
    respx.get(API).mock(return_value=_health("Prog Strength Backend", "v2.1.0"))
    respx.get(AGENT).mock(return_value=httpx.Response(502))
    respx.get(MCP).mock(side_effect=httpx.ConnectError("refused"))

    result = runner.invoke(app, ["status", "--env", "prod"])
    assert result.exit_code == 1
    assert "DOWN" in result.stdout


@respx.mock
def test_status_json_output():
    respx.get(API).mock(return_value=_health("Prog Strength Backend", "v2.1.0"))
    respx.get(AGENT).mock(return_value=_health("prog-strength-agent", "v1.4.0"))
    respx.get(MCP).mock(return_value=_health("prog-strength-mcp", "v1.1.0"))

    result = runner.invoke(app, ["status", "--env", "prod", "--json"])
    assert result.exit_code == 0
    assert '"environment"' in result.stdout
    assert '"version"' in result.stdout
    assert "v1.4.0" in result.stdout


def test_status_unknown_env_exit_1():
    result = runner.invoke(app, ["status", "--env", "staging"])
    assert result.exit_code == 1
    assert "unknown environment" in result.output
