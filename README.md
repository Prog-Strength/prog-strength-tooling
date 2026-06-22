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

| Setting | Flag | Env var | Default |
|---|---|---|---|
| API base URL | `--api` | `PST_API_URL` | `http://localhost:8080` |
| Admin JWT | `--token` | `PST_TOKEN` | _(none — required)_ |

Flags win over env vars. Export once per shell:

```bash
export PST_API_URL=http://localhost:8080
export PST_TOKEN=eyJhbGciOi...        # your admin JWT
```

## Usage

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
  cli.py              # `pst` entry point; mounts resource sub-apps
  commands/memory.py  # `pst memory list` / `search`
  client.py           # httpx client over the admin endpoints
  models.py           # pydantic views of the API DTOs
  render.py           # rich tables + --json output
  config.py           # resolve api/token from flags → env → default
```

Adding a new command group (e.g. `pst chat …`): add a module under `commands/`
with its own `typer.Typer()` app and mount it in `cli.py` with `add_typer`.
