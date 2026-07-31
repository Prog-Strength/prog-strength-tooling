# Prog Strength Tooling — Agent & Human Contributor Guide

This file is for AI coding agents (Claude Code, Copilot, Codex, Gemini, …) and
humans contributing to `prog-strength-tooling`. Read it before touching code.
[`README.md`](README.md) has the architecture and module map;
[`CONTRIBUTING.md`](CONTRIBUTING.md) has the full branching/commit rules — this
file is the orientation layer above both.

## What Prog Strength is

**Prog Strength** is a single-user fitness tracker built around one question:
*am I actually getting stronger?* It tracks weightlifting, bodyweight,
nutrition, and Garmin-imported running, exposed through a web app, an iOS app,
and an AI chat agent that all read one backend:

- **Backend** — a Go HTTP API over SQLite (Litestream → S3), fronted by an MCP
  server so an LLM agent reads/writes the same data the human surfaces do.
- **Agent** — a FastAPI service wrapping Claude with the MCP tools; it keeps a
  per-user **vector memory** (distilled facts about the user) it can recall.

The product is small on purpose. This repo is **operator tooling**, not a
user-facing surface — it does not ship to end users.

## What this CLI is for

`pst` is a personal [Typer](https://typer.tiangolo.com/) CLI for **operators**
(currently: the owner) to do three things against a running Prog Strength
deployment:

- **Probe** — inspect live state to answer "what does the system actually
  hold?" The first command group, `pst memory`, dumps and searches the agent's
  per-user vector memory.
- **Test** — confirm a subsystem behaves as expected against a real
  environment (e.g. that retrieval recalls what it should for a given user).
- **Routine maintenance** — operational chores that are easier as a typed
  command than a hand-rolled `curl` with a JWT.

It talks to the API over HTTP — mostly **admin-gated endpoints** — and is the
Python sibling of the Go `memctl` in `prog-strength-api` (the two coexist;
`pst` is the home for tooling going forward).

### How operators run it

- **Environments** are a named registry (`config.py: NAMED_ENVIRONMENTS`).
  Default is **`prod`** (`https://api.progstrength.fitness`); `--env local`
  (or `PST_ENV`) targets a local API; `--api <url>` is a one-off override.
- **Auth** is an **admin JWT** — a normal user token whose email is in the
  API's admin allowlist — supplied via `--token` or `PST_TOKEN`. Never hardcode
  or commit a token. Commands that hit an admin endpoint resolve config through
  `config.resolve_admin`, which raises `MissingTokenError` (rendered as the
  `missing admin token` panel) before any request goes out — use it, not
  `config.resolve`, for any new admin-gated command. Public-endpoint commands
  like `pst status` stay token-free.

```bash
pst status                                    # are api/agent/mcp up? what versions? (no token)
export PST_TOKEN=...                          # admin JWT (for memory commands)
pst memory list   --user <id>                 # what we store about a user
pst memory search --user <id> --query "leg day"   # what the agent would recall
pst memory list   --user <id> --json          # raw JSON for scripting
```

## How to contribute (the rules)

Full detail in [`CONTRIBUTING.md`](CONTRIBUTING.md); the essentials:

1. **Branch + PR.** `main` is the protected release branch. Cut a `feat/…` /
   `fix/…` / `chore/…` branch from `main` and open a PR back into it. No direct
   pushes to `main`.
2. **Conventional Commits drive releases.** Commit messages and the PR title
   follow `type(scope): subject`. PRs squash-merge, so the **PR title** becomes
   the release-driving subject a future semantic-release pipeline reads:
   `feat` → minor, `fix` → patch, `feat!`/`BREAKING CHANGE` → major, everything
   else (`chore`/`docs`/`ci`/`refactor`/`test`) → no release.
3. **Never bump the version by hand.** python-semantic-release derives the
   version, `CHANGELOG.md`, tag, and GitHub release from the Conventional
   Commit history on `main` (`feat` → minor, `fix` → patch; stays in `0.x`).
   It owns `[project].version` and `__version__` — don't edit those or the
   changelog manually. See [`CONTRIBUTING.md`](CONTRIBUTING.md#releases).
4. **Install the hooks** after cloning so checks run before code leaves your
   machine:
   ```bash
   uv sync
   uv run pre-commit install --install-hooks
   uv run pre-commit install --hook-type commit-msg --hook-type pre-push
   ```
5. **The checks** (local pre-commit and CI both enforce these — green before
   merge):
   ```bash
   uv run black --check .   # formatting (black owns it; line length 100)
   uv run ruff check .      # lint only
   uv run pytest            # tests; respx mocks httpx, no live API needed
   ```

## Conventions & scope guidance

- **uv-managed, src layout.** Source under `src/prog_strength_tooling/`, tests
  under `tests/`. Add runtime deps to `[project.dependencies]` and dev tools to
  `[dependency-groups].dev`; commit the updated `uv.lock`.
- **Keep modules small and single-purpose.** A new command group is a module
  under `commands/` with its own `typer.Typer()` app, mounted in `cli.py` via
  `add_typer`. Network/parse code (`client.py`, `models.py`) never imports the
  CLI or a console; rendering lives in `render.py`.
- **Be resilient to live data.** This tool hits real, evolving APIs. Prefer
  models that tolerate missing optional fields (default them) over ones that
  crash a whole dump on one sparse row — surface state, don't fail on it.
- **Never auto-spend or auto-mutate.** Probing is read-only by default;
  destructive maintenance commands must be explicit and confirm before acting.
  Don't add background polling or anything that calls a paid API automatically.
- **Stay an operator tool.** Don't add end-user product features here; that
  belongs in the API / web / mobile / agent repos.
