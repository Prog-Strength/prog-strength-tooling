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

import httpx

from .config import Config
from .models import MemoryList, SearchResult


class ClientError(Exception):
    """A request could not be completed (no token, connection refused, etc.)."""


class APIError(Exception):
    """The server returned a non-2xx response."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"{status_code}: {message}")


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
            resp = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise ClientError(str(exc)) from exc
        return self._unwrap(resp)

    def _post(self, path: str, body: dict[str, object]) -> dict:
        try:
            resp = self._client.post(path, json=body)
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
