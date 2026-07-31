"""Thin httpx client over the api admin vector-memory endpoints.

Wraps the two admin routes the agent's memory subsystem exposes:
  GET  /admin/memories          -> list a user's stored memories
  POST /admin/memories/search   -> retrieval probe (same path the agent uses)

Both are admin-gated, so the bearer token must belong to a user whose email
is in the API admin allowlist. Non-2xx responses become an APIError carrying
the status and the server's error message; transport failures become a
ClientError. Commands catch these and print a one-line message + exit 1.
"""

from __future__ import annotations

import time

import httpx

from .config import Config
from .logsetup import get_logger, kv, redact
from .models import MemoryList, SearchResult, WhoopConnection, WhoopResyncOutcome

log = get_logger(__name__)


class ClientError(Exception):
    """A request could not be completed (no token, connection refused, etc.)."""


class APIError(Exception):
    """The server returned a non-2xx response."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"{status_code}: {message}")


def _request(client: httpx.Client, method: str, path: str, **kwargs) -> httpx.Response:
    """Issue a request, logging the outcome with its duration.

    The duration is the single most useful field here: a command that appears
    hung is usually sitting inside one of these calls waiting out the client
    timeout, and the WARNING on failure prints how long it actually waited.
    The Authorization header is never logged — only the path, status, and time.

    The DEBUG line below splats `params` into `kv` alongside `method`/`path`.
    That's safe today because every caller's params/body keys (limit, offset,
    user_id, query, k, threshold, ...) are request fields, never "method" or
    "path" themselves — but it would raise TypeError if a future caller ever
    added a query param literally named "method" or "path".
    """
    log.debug("request %s", kv(method=method, path=path, **kwargs.get("params", {}) or {}))
    start = time.monotonic()
    try:
        resp = client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        elapsed_ms = round((time.monotonic() - start) * 1000, 1)
        log.warning(
            "%s %s failed %s",
            method,
            path,
            kv(error=type(exc).__name__, elapsed_ms=elapsed_ms),
        )
        raise
    elapsed_ms = round((time.monotonic() - start) * 1000, 1)
    log.info(
        "%s %s %s",
        method,
        path,
        kv(status=resp.status_code, bytes=len(resp.content), elapsed_ms=elapsed_ms),
    )
    return resp


class MemoryClient:
    """Client for the admin vector-memory surface.

    Usable as a context manager so the underlying httpx.Client is closed:
        with MemoryClient(cfg) as c:
            c.list_memories(user_id="...")
    """

    def __init__(self, cfg: Config, timeout: float = 30.0) -> None:
        if not cfg.token:
            raise ClientError("no admin token. Pass --token or set PST_TOKEN to an admin JWT.")
        self._client = httpx.Client(
            base_url=cfg.base_url,
            headers={"Authorization": f"Bearer {cfg.token}"},
            timeout=timeout,
        )
        log.debug(
            "%s ready %s",
            type(self).__name__,
            kv(base_url=cfg.base_url, timeout_s=timeout, token=redact(cfg.token)),
        )

    def __enter__(self) -> "MemoryClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # --- endpoints ------------------------------------------------------

    def list_memories(self, user_id: str, limit: int = 100, offset: int = 0) -> MemoryList:
        """GET /admin/memories — a user's stored memories (paged)."""
        params: dict[str, str | int] = {"limit": limit, "offset": offset}
        if user_id:
            params["user_id"] = user_id
        data = self._get("/admin/memories", params)
        return MemoryList.model_validate(data)

    def search(
        self,
        user_id: str,
        query: str,
        k: int | None = None,
        threshold: float | None = None,
    ) -> SearchResult:
        """POST /admin/memories/search — retrieval probe.

        k / threshold preserve the server's pointer-omission contract: a None
        value is omitted from the body so the server applies its configured
        default, while an explicit 0 IS sent (threshold 0 = full sweep, no
        cap). Build the body with only the set fields so None != 0.
        """
        body: dict[str, object] = {"query": query, "user_id": user_id}
        if k is not None:
            body["k"] = k
        if threshold is not None:
            body["threshold"] = threshold
        data = self._post("/admin/memories/search", body)
        return SearchResult.model_validate(data)

    # --- transport ------------------------------------------------------

    def _get(self, path: str, params: dict[str, str | int]) -> dict:
        try:
            resp = _request(self._client, "GET", path, params=params)
        except httpx.HTTPError as exc:
            raise ClientError(str(exc)) from exc
        return self._unwrap(resp)

    def _post(self, path: str, body: dict[str, object]) -> dict:
        try:
            resp = _request(self._client, "POST", path, json=body)
        except httpx.HTTPError as exc:
            raise ClientError(str(exc)) from exc
        return self._unwrap(resp)

    @staticmethod
    def _unwrap(resp: httpx.Response) -> dict:
        """Validate status and return the envelope's `data` object.

        The api wraps every payload in {"service","version","message","data"}
        on success and {"error": ...} on failure. We surface the server's
        error string when present, falling back to the raw body.
        """
        if resp.is_success:
            envelope = resp.json()
            return envelope.get("data") or {}

        message = resp.text
        try:
            message = resp.json().get("error", message)
        except ValueError:
            pass
        raise APIError(resp.status_code, message)


class WhoopAdminClient:
    """Client for the admin WHOOP integration surface.

    Wraps the three admin routes the API's WHOOP subsystem exposes:
      GET  /admin/whoop/connections           -> every user's connection row
      GET  /admin/whoop/connections/{userID}  -> one user's connection (404 if none)
      POST /admin/whoop/resync                -> re-ingest a user's recovery window

    Structurally identical to MemoryClient: admin-gated (bearer token), the same
    envelope-unwrapping and error mapping, and usable as a context manager so the
    underlying httpx.Client is closed:
        with WhoopAdminClient(cfg) as c:
            c.list_connections()
    """

    def __init__(self, cfg: Config, timeout: float = 30.0) -> None:
        if not cfg.token:
            raise ClientError("no admin token. Pass --token or set PST_TOKEN to an admin JWT.")
        self._client = httpx.Client(
            base_url=cfg.base_url,
            headers={"Authorization": f"Bearer {cfg.token}"},
            timeout=timeout,
        )
        log.debug(
            "%s ready %s",
            type(self).__name__,
            kv(base_url=cfg.base_url, timeout_s=timeout, token=redact(cfg.token)),
        )

    def __enter__(self) -> "WhoopAdminClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # --- endpoints ------------------------------------------------------

    def list_connections(self) -> list[WhoopConnection]:
        """GET /admin/whoop/connections — every user's WHOOP connection row."""
        data = self._get("/admin/whoop/connections", {})
        return [WhoopConnection.model_validate(c) for c in data.get("connections", [])]

    def get_connection(self, user_id: str) -> WhoopConnection:
        """GET /admin/whoop/connections/{userID} — one user's connection (404 if none)."""
        data = self._get(f"/admin/whoop/connections/{user_id}", {})
        return WhoopConnection.model_validate(data)

    def resync(self, user_id: str, window_days: int) -> WhoopResyncOutcome:
        """POST /admin/whoop/resync — re-ingest a user's recovery window."""
        body: dict[str, object] = {"user_id": user_id, "window_days": window_days}
        data = self._post("/admin/whoop/resync", body)
        return WhoopResyncOutcome.model_validate(data)

    # --- transport ------------------------------------------------------

    def _get(self, path: str, params: dict[str, str | int]) -> dict:
        try:
            resp = _request(self._client, "GET", path, params=params)
        except httpx.HTTPError as exc:
            raise ClientError(str(exc)) from exc
        return MemoryClient._unwrap(resp)

    def _post(self, path: str, body: dict[str, object]) -> dict:
        try:
            resp = _request(self._client, "POST", path, json=body)
        except httpx.HTTPError as exc:
            raise ClientError(str(exc)) from exc
        return MemoryClient._unwrap(resp)
