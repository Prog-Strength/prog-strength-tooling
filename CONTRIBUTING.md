# Contributing to prog-strength-tooling

Thanks for improving the Prog Strength operator CLI. This document covers how
changes get in: the branching model, commit conventions, and the checks that
gate every change.

## TL;DR

```bash
uv sync                                              # install deps + dev tools
uv run pre-commit install --install-hooks            # format/lint on commit
uv run pre-commit install --hook-type commit-msg --hook-type pre-push

git switch -c feat/my-change                         # branch off main
# ...make changes...
git commit -m "feat(memory): add --since filter"     # conventional commit
git push -u origin feat/my-change
gh pr create --base main                             # PR into the release branch
```

## Branching model

- **`main` is the release branch.** It is protected — no direct pushes. Every
  change lands through a pull request.
- **Work on a development branch** cut from `main` (`feat/…`, `fix/…`,
  `chore/…`) and open a PR back into `main`.
- **PRs are squash-merged.** The PR title becomes the single commit subject on
  `main`, so the **PR title must itself be a Conventional Commit** — that
  subject is what drives versioning (see below). CI enforces this.

## Conventional Commits

Commit messages — and PR titles — follow
[Conventional Commits](https://www.conventionalcommits.org/). This is not just
style: it is how releases will be versioned. A future
[semantic-release](https://semantic-release.gitbook.io/) pipeline derives the
next version and changelog for `pst` from these subjects.

Format: `type(optional-scope): subject` (lowercase subject, no trailing period).

| Type | When to use | Release effect |
|---|---|---|
| `feat` | a new command, flag, or capability | **minor** bump |
| `fix` | a bug fix to existing behavior | **patch** bump |
| `feat!` / `fix!` or a `BREAKING CHANGE:` footer | incompatible change (flag removed/renamed, output contract changed) | **major** bump |
| `docs`, `chore`, `ci`, `refactor`, `test`, `build`, `perf` | everything else | no release |

Common scopes: `memory` (the vector-memory commands), `config`, `client`,
`ci`. Pick the type for **what should happen on release**, not just what the
diff touches — a behavior change operators should get is `feat`/`fix`.

Examples:

```
feat(memory): add --since to filter the dump by date
fix(client): tolerate memories missing source_session_id
chore(ci): pin ruff-pre-commit to v0.15.18
```

The local `commit-msg` hook (`conventional-pre-commit`) rejects messages that
don't conform, and CI re-checks the PR title.

## Checks

The same three checks run locally (via pre-commit) and in CI on every PR — a
PR cannot merge to `main` if any fail:

| Check | Command | Tool |
|---|---|---|
| Formatting | `uv run black --check .` | [black](https://black.readthedocs.io/) (owns formatting; line length 100) |
| Lint | `uv run ruff check .` | [ruff](https://docs.astral.sh/ruff/) (lint only) |
| Tests | `uv run pytest` | pytest + respx (no live API needed) |

Run them all at once before pushing:

```bash
uv run pre-commit run --all-files   # format + lint on every file
uv run pytest                       # full suite (also runs on pre-push)
```

`black` and `ruff` auto-fix on commit; just re-stage the files they touch.

## Local hooks

The hooks are configured in `.pre-commit-config.yaml`:

- **on commit** — `black`, `ruff --fix`, and whitespace/EOF/TOML hygiene on the
  staged files.
- **on commit-msg** — Conventional Commit validation.
- **on push** — the full `pytest` suite, so you never push a red branch.

If a hook updates `.pre-commit-config.yaml` revisions, that's
`uv run pre-commit autoupdate` — commit it as `chore(ci): …`.

## Releases

Releases are fully automated by
[python-semantic-release](https://python-semantic-release.readthedocs.io/) —
**never bump the version or edit `CHANGELOG.md` by hand.** PSR owns
`[project].version` in `pyproject.toml`, `__version__` in
`src/prog_strength_tooling/__init__.py`, and the changelog.

When a PR squash-merges to `main`, the `Release` workflow
(`.github/workflows/release.yml`) runs:

1. Inspects the Conventional Commit subjects since the last `vX.Y.Z` tag.
2. If any are releasable, computes the next version:
   - `fix:` → patch (`0.1.0` → `0.1.1`)
   - `feat:` → minor (`0.1.0` → `0.2.0`)
   - `feat!:` / `BREAKING CHANGE:` → minor while in `0.x`
     (`major_on_zero = false`), major once past `1.0.0`
3. Bumps both version files, regenerates `CHANGELOG.md`, commits
   `chore(release): vX.Y.Z`, tags `vX.Y.Z`, and publishes a GitHub release.
4. `chore`/`docs`/`ci`-only pushes release nothing — that's by design.

So the **type you choose on a PR title decides the next version.** There is no
manual release step. (The tool is internal — releases are a tag + changelog +
GitHub release; nothing is published to PyPI.)

## Project layout

See [`README.md`](README.md) for the architecture and module map, and
[`AGENTS.md`](AGENTS.md) for the broader Prog Strength context and how the CLI
is meant to be used.
