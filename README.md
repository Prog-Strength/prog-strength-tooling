# prog-strength-tooling

A personal [Typer](https://typer.tiangolo.com/) CLI (`pst`) for probing queries
and maintenance tasks against the prog-strength stack. It talks to the
prog-strength-api admin endpoints over HTTP, and to CloudWatch Logs for the
deployed services' logs — it is an operator tool, not part of the running
system.

Three command groups so far:

- **`memory`** — inspect the agent vector memory stored per user, so you can
  see what we remember about a user and confirm retrieval is working as
  expected.
- **`logs`** — pull the server-side log lines for a request id, so a failed
  call reported from the web or mobile client leads straight to what the
  backend did.
- **`whoop`** — diagnose and repair WHOOP recovery ingestion: `doctor` runs a
  seven-check diagnosis from CloudWatch evidence (plus admin API checks if a
  token and `--user` are supplied), `resync` forces a re-ingest.

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

### Getting an admin token

Every command that requires an admin endpoint (`pst memory`, `pst whoop
resync`) checks for a token **before** making a request and stops with
instructions if there isn't one — you never get a bare `401`. `pst status`
needs no token; health endpoints are public. `pst whoop doctor` is the
exception: its two admin-derived checks use a token if you give it one, but
degrade to *skipped* rather than stopping the command when you don't (see
below).

| Environment | How to get a JWT |
|---|---|
| `prod` | Sign in to the Prog Strength web app as an admin, then copy `localStorage.ps_access_token` from the browser devtools. |
| `local` | With `DEV_AUTH=true` on the api: `curl -sX POST http://localhost:8080/auth/dev/token -H 'Content-Type: application/json' -d '{"email": "you@example.com"}'` |

Then supply it with `export PST_TOKEN=<admin-jwt>` (once per shell) or
`--token <admin-jwt>` (per command). Tokens expire — a `403` from the server
means the account isn't on the allowlist, a `401` means the JWT is stale, so
get a fresh one.

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
| AWS profile (`logs`, `whoop doctor`) | `--profile` | `PST_AWS_PROFILE` | _(AWS default chain)_ |
| Log level | `-v` / `-vv` / `-q` | `PST_LOG_LEVEL` | `info` |

Export once per shell:

```bash
export PST_TOKEN=eyJhbGciOi...        # your admin JWT
# optional: target local instead of prod
export PST_ENV=local
```

### Logging & diagnostics

Every command logs what it is doing to **stderr** — stdout stays clean, so
`--json | jq` is unaffected. The default is INFO: which environment and region
resolved, which log groups are being scanned, and how long each phase took.

```bash
pst whoop doctor            # INFO — phases and durations
pst -v whoop doctor         # DEBUG — config precedence, per-page paging, per-check results
pst -vv whoop doctor        # + botocore/httpx wire logs
pst -q whoop doctor         # warnings and errors only
export PST_LOG_LEVEL=debug  # same as -v, once per shell (an explicit flag wins)
```

When a command seems to hang, `-v` is the first thing to reach for: it shows
the CloudWatch page-by-page progress and the configured HTTP timeout, which
distinguishes "waiting out a 30s timeout" from "genuinely stuck". Admin tokens
are never logged at any level — only their last four characters and length.

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

### Find a request's server-side logs

```bash
pst logs trace <request-id>                    # all 3 services, last 24h
pst logs trace <request-id> --since 7d         # widen the window
pst logs trace <request-id> --service api      # one service (repeatable)
pst logs trace <request-id> --raw              # original lines, unparsed
pst logs trace <request-id> --json             # structured, for scripting
```

The workflow: the web or mobile client hits an error, the response carries a
`request_id` (in the error envelope body, and on the `X-Request-ID` header),
and you need the server side of that call. This searches the CloudWatch log
groups `prog-strength-infra` creates — `/prog-strength/{api,agent,mcp}` in
`us-east-2` — and prints every matching line on one timestamp-ordered
timeline, normalizing the api's JSON (Go `slog`) and the agent/mcp's text
(Python `logging`) into the same table.

**Auth is AWS, not `PST_TOKEN`.** CloudWatch is read with your own AWS
credentials — the EC2 instance role is deliberately write-only, so reads are
an operator action. Supply a profile with `--profile` or `PST_AWS_PROFILE`;
the identity needs `logs:FilterLogEvents` on those groups.

```bash
export PST_AWS_PROFILE=prog-strength-admin
```

All three services are searched by default. Not because one id spans them —
each service accepts an inbound `X-Request-ID` but doesn't forward one, so an
id lives in exactly one group today — but because an id from a client doesn't
tell you which service minted it, and guessing wrong looks exactly like "not
found".

Exit codes make it scriptable: **0** found lines, **1** searched fine but
matched nothing, **2** configuration or AWS error.

Windows are relative (`--since 90m|24h|7d`, default `24h`) or absolute
(`--start`/`--end`, UTC); the two can't be combined. CloudWatch retains 30
days, so nothing older than that is findable.

### Diagnose WHOOP ingestion

```bash
pst whoop doctor                        # 7-day window, log-only checks
pst whoop doctor --user <user-id>       # + connection & freshness (needs PST_TOKEN)
pst whoop doctor --since 24h            # narrower window
pst whoop doctor --json                 # structured, for scripting
```

`doctor` runs seven checks: five read the api's CloudWatch logs with your own
AWS credentials (deliveries arriving, on the served path, with an accepted
signature, producing syncs, landing rows); the other two (connection health,
data freshness) need an admin token **and** `--user`, and simply skip — not
fail — when either is missing. Exit codes match `pst logs`: **0** healthy,
**1** findings, **2** config/AWS/API error.

The five log checks are served from a single capped pass over the api log
group, `--max-events` (default **20,000**, `0` for unlimited). This exists
because the two checks used to paginate the same CloudWatch query twice, with
no ceiling — on a busy log group that made `doctor` indistinguishable from a
hang. If a run truncates, it prints a banner naming the oldest slice of the
window it actually covered (CloudWatch returns events oldest-first, so a
truncated scan misses the *most recent* activity, which matters when you're
checking freshness). Narrow `--since` or pass `--max-events 0` to see past it.

```bash
pst whoop resync --user <user-id>              # re-ingest the last 30 days
pst whoop resync --user <user-id> --days 7     # narrower window
```

`resync` is admin-gated the same way `pst memory` is — it needs `PST_TOKEN`
and resolves it up front with the same missing-token guidance.

## How it maps to the API

| Command | Endpoint |
|---|---|
| `pst status` | `GET /health` on each of api, agent, mcp |
| `pst memory list` | `GET /admin/memories?user_id=…` |
| `pst memory search` | `POST /admin/memories/search` |
| `pst logs trace` | AWS `logs:FilterLogEvents` on `/prog-strength/{api,agent,mcp}` |
| `pst whoop doctor` | AWS `logs:FilterLogEvents` on `/prog-strength/api`, + `GET /admin/whoop/connections/{user_id}` if `--user`/token given |
| `pst whoop resync` | `POST /admin/whoop/resync` |

## Development

```bash
uv sync
uv run pytest        # tests (httpx mocked with respx, AWS with botocore's
                     # Stubber — no server and no AWS account needed)
uv run ruff check .  # lint
```

### Layout

```
src/prog_strength_tooling/
  cli.py              # `pst` entry point; mounts sub-apps + single commands
  logsetup.py         # log level resolution, stderr handler, timing helpers
  commands/memory.py  # `pst memory list` / `search`
  commands/status.py  # `pst status`
  commands/logs.py    # `pst logs trace`
  commands/whoop.py   # `pst whoop doctor` / `resync`
  client.py           # httpx client over the admin endpoints
  health.py           # GET /health probing for the status command
  cloudwatch.py       # CloudWatch Logs queries for the logs command
  whoop.py            # the doctor's diagnosis engine (pure)
  whooplogs.py        # one capped CloudWatch pass for the doctor's evidence
  logparse.py         # raw log line -> LogRecord (pure, no I/O)
  window.py           # --since / --start / --end resolution
  models.py           # pydantic views of the API DTOs
  render.py           # rich tables + --json output
  config.py           # named environments (service URLs) + token resolution
```

Adding a new command group (e.g. `pst chat …`): add a module under `commands/`
with its own `typer.Typer()` app and mount it in `cli.py` with `add_typer`. For
a single-shot command, register it on the root app with `app.command(...)`.
