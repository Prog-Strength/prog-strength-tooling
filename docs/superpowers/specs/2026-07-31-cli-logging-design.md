# CLI logging & diagnostics — design

**Date:** 2026-07-31
**Status:** approved, ready for planning

## Problem

`pst whoop doctor` hangs with no output, and the operator has no way to tell
*where*. The CLI has no logging at all: every module writes only final rendered
results, so a command that spends 90 seconds inside a CloudWatch paginator looks
identical to one that has deadlocked.

Two distinct defects are in play, and the design fixes both:

1. **No observability.** Nothing reports which phase is running, how long it
   took, or what configuration it resolved.
2. **A real performance bug.** `commands/whoop.py:doctor` calls
   `whooplogs.scan_deliveries()` and then `whooplogs.scan_syncs()`. Each
   paginates `filter_log_events` over the *same* 7-day window with the *same*
   `"whoop"` filter pattern, sequentially, and — unlike
   `cloudwatch._search_group`, which caps at `limit + 1` — **neither passes a
   `MaxItems` cap**. That is two uncapped full scans of a busy log group, back
   to back, in silence.

## Goals

- Every command emits INFO-level phase/duration logs by default, and far more
  detail under an operator-set debug level.
- Logs go to **stderr only**, so `--json` on stdout stays pipeable.
- No log line ever contains an admin token or an `Authorization` header.
- `pst whoop doctor` cannot hang unboundedly, and a truncated diagnosis is
  never presented as a complete one.

## Non-goals

- Structured/JSON log output or a log-shipping integration. `key=value` message
  tails are enough to grep; a logging framework is not warranted here.
- Instrumenting the API, agent, or MCP services. This is operator tooling only.
- Changing exit codes or any existing rendered stdout, apart from the new
  truncation banner.

---

## 1. New module: `logsetup.py`

Imports nothing from the package, keeping the dependency graph acyclic. It
deliberately does **not** import `render.py` (which pulls in `whoop`, `health`,
and `models`); it constructs its own `Console(stderr=True)` for the handler.
Both consoles write to `sys.stderr`, which is fine.

Public surface:

- **`configure(verbosity: int, quiet: bool) -> None`** — installs a single
  `rich.logging.RichHandler` on the `prog_strength_tooling` logger.
  - `markup=False`, so a log line containing `/webhooks/whoop,` or a stray
    `[bracket]` is shown literally and never parsed as rich markup.
  - `show_path=False`, `log_time_format="%H:%M:%S"`.
  - At DEBUG the formatter gains a `[%(name)s]` prefix so the speaking module
    is visible.
  - Idempotent: calling it twice replaces the handler rather than doubling
    output (matters under Typer's `CliRunner` in tests).
- **`get_logger(name) -> logging.Logger`** — thin `logging.getLogger` wrapper so
  modules don't each hardcode the package prefix.
- **`timed(logger, event, **fields)`** — context manager. DEBUG on entry, INFO
  on exit with `elapsed_ms`. This is what makes "where did the 90 seconds go"
  answerable.
- **`Heartbeat(logger, interval=5.0)`** — `.tick(**fields)` is called on every
  page; it emits at most one INFO line per `interval` seconds. Plain log lines,
  not a rich live widget, so progress survives redirection and piping.
- **`kv(**fields) -> str`** — renders a greppable `key=value` tail.
- **`redact(token: str | None) -> str`** — `…a3f9 (len 214)`, or `absent`.

### Level precedence

Mirrors the flag → env → default doctrine already documented in `config.py`:

| Input | Result |
| --- | --- |
| `-q` / `--quiet` | WARNING |
| `-vv` | DEBUG for `prog_strength_tooling.*` **and** `botocore`, `urllib3`, `httpx`, `httpcore` |
| `-v` / `--verbose` | DEBUG for `prog_strength_tooling.*` only |
| none, `PST_LOG_LEVEL` set | that level (`debug`/`info`/`warning`/`error`, case-insensitive) |
| none | INFO |

An explicit flag beats `PST_LOG_LEVEL`. An unparseable `PST_LOG_LEVEL` falls
back to INFO and emits one WARNING naming the bad value — a typo'd env var must
not silence the tool.

## 2. Root callback in `cli.py`

`pst` has no `@app.callback()` today. Adding one attaches the verbosity flags to
every subcommand:

```bash
pst whoop doctor              # INFO
pst -v whoop doctor           # DEBUG (pst internals)
pst -vv whoop doctor          # + botocore/httpx wire logs
pst -q whoop doctor           # WARNING and above
PST_LOG_LEVEL=debug pst whoop doctor
```

The callback calls `logsetup.configure(...)`, then DEBUG-logs the `pst` version,
the Python version, and `sys.argv` **with any `--token <value>` redacted**.

`-v`/`-q` are free at the root: no existing global options exist, and the only
short flag in use anywhere is `-s` on `logs trace`.

## 3. Instrumentation map

The rule that keeps default output sane: **INFO is phase boundaries and
durations; rendered results stay in the renderer.** No INFO line duplicates
something a table already prints.

| Module | INFO | DEBUG |
| --- | --- | --- |
| `config.py` | `resolve_logs`: env, region, profile, selected log groups. `resolve`/`resolve_admin`: env and resolved `api_url` | full precedence trace (which of flag/env/default won) and token presence via `redact` |
| `window.py` | resolved absolute window | the parse of `--since` into a delta |
| `cloudwatch.py` | per-group scan start, heartbeat, completion (events/pages/elapsed) | boto3 session construction, each page's event count + elapsed, running total |
| `whooplogs.py` | one-pass scan start/heartbeat/completion; truncation WARNING | per-page detail; counts of lines skipped as non-matching |
| `client.py` | `POST /admin/whoop/resync -> 200 in 412ms`; WARNING with elapsed on transport failure | `base_url`, the **configured 30s timeout**, request params/body, response size |
| `health.py` | — (`pst status` already renders latency) | per-probe URL and outcome |
| `whoop.py` | — | one line per check: name, ok/skipped, and the evidence that decided it |

The `client.py` timeout line and the `whoop.py` per-check lines are the two most
directly useful additions for the reported symptom: the first distinguishes "hung"
from "waiting out a 30s timeout", the second says which of the seven checks
tripped and why.

`whoop.py` stays pure — it gains a module logger and DEBUG calls, but still
imports no boto3/httpx and makes no network calls.

## 4. The `whooplogs` fix

`scan_deliveries` and `scan_syncs` collapse into a single pass:

```python
def scan(cfg: LogsConfig, window: Window, max_events: int = MAX_EVENTS) -> WhoopScan
```

One `filter_log_events` pagination feeds both aggregations from the same event
stream, halving the AWS work outright.

```python
@dataclass(frozen=True)
class WhoopScan:
    deliveries: DeliveryScan
    syncs: SyncScan
    events_scanned: int
    pages: int
    truncated: bool
    covered_start: datetime | None   # actual MIN event timestamp seen
    covered_end: datetime | None     # actual MAX event timestamp seen
```

`DeliveryScan` and `SyncScan` keep their current shapes, so `whoop.diagnose()`
needs no signature change.

### The cap

`MAX_EVENTS = 20_000`, exposed as `--max-events` on `doctor`; `0` means
unlimited. On hitting the cap the scan stops paging, sets `truncated=True`, and
logs a WARNING.

**Coverage is measured, not assumed.** `FilterLogEvents` returns events ascending
by timestamp, so a capped scan keeps the *oldest* slice of the window — the
opposite of what a freshness check wants. Rather than encode that assumption, the
scan records the real min/max event timestamps it saw and the warning reports
that actual range against the requested one:

```
WARNING scan hit --max-events 20000 after 41 pages; covered
        2026-07-24T14:22Z..2026-07-26T09:10Z of the requested
        2026-07-24T14:22Z..2026-07-31T14:22Z. Narrow --since, or
        pass --max-events 0 to scan the whole window.
```

The old `scan_deliveries` / `scan_syncs` are **removed**, not kept as thin
wrappers — leaving them in place would let the double scan creep back in. They
are internal to the package with no external consumers.

### Surfacing truncation

`whoop.diagnose()` stays pure and unchanged. The command passes the scan to the
renderer:

```python
render_diagnosis(diagnosis, scan, as_json=as_json)
```

`render_diagnosis` prints a `! results truncated` banner above the table when
`scan.truncated`, and always adds a `"scan"` object (`events_scanned`, `pages`,
`truncated`, `covered_start`, `covered_end`) to the JSON payload — so a piped
`--json` consumer can distinguish a partial diagnosis from a complete one.

## 5. Testing

- **`tests/test_logsetup.py`** — the level-resolution matrix (flags vs
  `PST_LOG_LEVEL` vs default, including the bad-value fallback); `Heartbeat`
  fires at most once per interval given a stubbed clock; `redact` output; and an
  assertion that a token passed via `--token` never reaches a log record.
- **`tests/test_whooplogs.py`** — reworked onto `scan()`. Asserts **exactly one**
  paginator call serves both aggregations; a cap case asserts `truncated` is set
  and `covered_start`/`covered_end` reflect real event timestamps rather than the
  requested window bounds.
- **`tests/test_whoop_cli.py`** — `--max-events` plumbing, the truncation banner
  in both table and JSON output, and unchanged exit codes (0 healthy / 1 findings
  / 2 config-or-AWS error).
- **`caplog` assertions** on the key INFO lines in the doctor path (window
  resolved, scan complete), so the diagnostics themselves don't silently rot.

Existing tests must pass unmodified except `test_whooplogs.py` and
`test_whoop_cli.py`, whose changes are enumerated above.

## Compatibility

Exit codes, stderr/stdout separation, and every existing command's stdout stay
byte-identical, apart from the new truncation banner on `whoop doctor`. INFO logs
are new output on **stderr** for every command; `-q` restores the previous
silence.

## Commit strategy

One `feat` PR. Per `AGENTS.md`, the PR title drives the release:
`feat(logging): add info/debug logging and fix whoop doctor's double scan`.
