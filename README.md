# prog-strength-tooling

A personal [Typer](https://typer.tiangolo.com/) CLI (`pst`) for probing queries
and maintenance tasks against the prog-strength stack. It talks to the
prog-strength-api admin endpoints over HTTP — it is an operator tool, not part
of the running system.

The first command group is **`memory`**: inspect the agent vector memory stored
per user, so you can see what we remember about a user and confirm retrieval is
working as expected.

> **Note:** the api repo also ships a Go operator CLI, `memctl`, over the same
> two endpoints. `pst` is the Python home for tooling going forward (richer
> output, room to grow); the two coexist.

## Install

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.12.

```bash
uv sync                       # create the venv and install deps
uv run pst --help             # run without activating
# or install the `pst` entry point on your PATH:
uv tool install --editable .
```

## Configuration

The admin endpoints are gated: you need an **admin JWT** — a normal user token
whose email is in the API's admin allowlist (the same token `memctl` uses).

### Picking an environment

Service URLs come from a registry of **named environments**, each mapping the
backend services (`api`, `agent`, `mcp`) to their base URLs. The default is
**`prod`**, so `pst` talks to production out of the box.

| Environment | api | agent | mcp |
|---|---|---|---|
| `prod` (default) | `api.progstrength.fitness` | `agent.progstrength.fitness` | `mcp.progstrength.fitness` |
| `local` | `localhost:8080` | `localhost:8001` | `localhost:8000` |

Select one with `--env <name>` (or `PST_ENV`). For the memory commands you can
also pass an explicit one-off API URL with `--api <url>` (or `PST_API_URL`).
Resolution precedence for the api URL, highest first:

1. `--api <url>` — explicit URL (flag)
2. `--env <name>` — named environment (flag)
3. `PST_API_URL` — explicit URL (env var)
4. `PST_ENV` — named environment (env var)
5. default environment (`prod`)

> **Adding an environment** is a one-line entry in `NAMED_ENVIRONMENTS`
> (`src/prog_strength_tooling/config.py`); the `--env` flag, `PST_ENV`, and CLI
> help all pick it up automatically.

### Settings

| Setting | Flag | Env var | Default |
|---|---|---|---|
| Environment | `--env` | `PST_ENV` | `prod` |
| Explicit base URL | `--api` | `PST_API_URL` | _(derived from environment)_ |
| Admin JWT | `--token` | `PST_TOKEN` | _(none — required)_ |

Export once per shell:

```bash
export PST_TOKEN=eyJhbGciOi...        # your admin JWT
# optional: target local instead of prod
export PST_ENV=local
```

## Usage

### Check backend service status

```bash
pst status                      # all services in prod
pst status --env local          # target a different environment
pst status --json               # raw JSON for scripting
pst status --timeout 2          # per-service timeout (seconds)
```

Probes the `api`, `agent`, and `mcp` `/health` endpoints and reports whether
each is **UP/DOWN**, its version, and latency. No token required (health is
public). **Exits non-zero if any service is down**, so it gates scripts:

```bash
pst status && ./deploy.sh       # only deploy if everything is healthy
```

### List a user's stored memories

```bash
pst memory list --user <user-id>
pst memory list --user <user-id> --limit 50 --offset 50
pst memory list --user <user-id> --json        # raw JSON for scripting
```

Shows each distilled memory with its source session, embedding model/dimension,
creation time, and whether it's still **active** or has been **superseded** by a
newer distillation.

### Probe retrieval (what the agent would recall)

```bash
pst memory search --user <user-id> --query "how much do I bench?"
pst memory search --user <user-id> --query "leg day" --k 5 --threshold 0.7
pst memory search --user <user-id> --query "leg day" --threshold 0    # full sweep, no cap
```

`search` calls the **same retrieval path the agent uses**, so the results
mirror production recall rather than a separate query. The output header shows
the active distance cap (threshold) the service actually applied.

Threshold/k semantics match the api contract:

- **omit** `--threshold` / `--k` → the server applies its configured default.
- **`--threshold 0`** → explicit full sweep (no distance cap), distinct from
  omitting it.

Lower distance = closer match.

## How it maps to the API

| Command | Endpoint |
|---|---|
| `pst status` | `GET /health` on each of api, agent, mcp |
| `pst memory list` | `GET /admin/memories?user_id=…` |
| `pst memory search` | `POST /admin/memories/search` |

## Development

```bash
uv sync
uv run pytest        # tests (httpx mocked with respx — no server needed)
uv run ruff check .  # lint
```

### Layout

```
src/prog_strength_tooling/
  cli.py              # `pst` entry point; mounts sub-apps + single commands
  commands/memory.py  # `pst memory list` / `search`
  commands/status.py  # `pst status`
  client.py           # httpx client over the admin endpoints
  health.py           # GET /health probing for the status command
  models.py           # pydantic views of the API DTOs
  render.py           # rich tables + --json output
  config.py           # named environments (service URLs) + token resolution
```

Adding a new command group (e.g. `pst chat …`): add a module under `commands/`
with its own `typer.Typer()` app and mount it in `cli.py` with `add_typer`. For
a single-shot command, register it on the root app with `app.command(...)`.
