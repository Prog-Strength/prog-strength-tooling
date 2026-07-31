"""Shared test fixtures.

The suite must never read the operator's own `PST_*` environment. `config.py`
resolves flags, then env vars, then defaults — so an exported `PST_TOKEN` in the
developer's shell silently satisfies a test that was asserting the *missing*
token path, and the command then proceeds to make a real request against the
configured environment (which defaults to **prod**). That is how
`test_resync_missing_token_exits_nonzero` came to fail on a clean checkout with
a live 404 from the production API rather than the expected error panel.

Clearing the whole `PST_*` namespace here, for every test, makes the suite
depend only on what each test sets explicitly. Tests that want a value pass it
through `runner.invoke(env=...)` or `monkeypatch.setenv`, both of which layer on
top of this cleared baseline.
"""

from __future__ import annotations

import os

import pytest

#: Prefix covering every environment variable this CLI reads (PST_ENV,
#: PST_TOKEN, PST_API_URL, PST_AWS_PROFILE, PST_LOG_LEVEL). Matched by prefix
#: rather than listed, so a future variable is isolated the day it is added.
PST_PREFIX = "PST_"


@pytest.fixture(autouse=True)
def _isolate_pst_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every PST_* variable so the operator's shell can't reach a test."""
    for name in [key for key in os.environ if key.startswith(PST_PREFIX)]:
        monkeypatch.delenv(name, raising=False)
