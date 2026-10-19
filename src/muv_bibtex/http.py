"""THE single HTTP layer for all Clarivate API access.

Responsibilities (kept in one place on purpose):

* build requests with the auth header name taken from ``api_map.yaml`` —
  the API key itself is injected here and nowhere else, never logged;
* on-disk response cache keyed by a hash of (method, url, params, body) so a
  rerun after an interruption performs **zero** repeat API calls;
* exponential backoff with jitter for HTTP 429 / 5xx / timeouts, honouring
  ``Retry-After`` when present;
* bounded concurrency (thread pool, conservative default);
* capture of every quota/rate-limit header that matches the configured
  prefixes (``X-REC-*``, ``X-REQ-*``) into a thread-safe snapshot;
* clear typed errors for 401/403 vs. other failures.

All higher modules (WoS client, InCites client, discover) go through
:class:`ApiClient`.  Nothing else may open a socket.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from typing import Any, Callable, Iterable, Mapping

import requests

from .errors import ApiAuthenticationError, ApiResponseError

log = logging.getLogger("muv.http")

#: JSON path used to store cached-response metadata next to the payload.
_META_SUFFIX = ".meta.json.gz"


# --------------------------------------------------------------------------- #
# Quota bookkeeping
# --------------------------------------------------------------------------- #
@dataclass
class QuotaSnapshot:
    """Latest observed values of all captured quota/rate headers."""

    values: dict[str, int] = field(default_factory=dict)
    observed_at: float = field(default_factory=time.time)

    def remaining_annual_records(self, header_name: str | None) -> int | None:
        if not header_name:
            return None
        return self.values.get(header_name.lower())

    def as_dict(self) -> dict[str, Any]:
        return {
            "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.observed_at)),
            "headers": dict(self.values),
        }


class RequestCounter:
    """Thread-safe tally of network vs. cache-hit requests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.network_requests = 0
        self.cache_hits = 0
        self.retries = 0

    def note_network(self) -> None:
        with self._lock:
            self.network_requests += 1

    def note_cache_hit(self) -> None:
        with self._lock:
            self.cache_hits += 1

    def note_retry(self) -> None:
        with self._lock:
            self.retries += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "network_requests": self.network_requests,
                "cache_hits": self.cache_hits,
                "retries": self.retries,
            }


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #
def request_hash(
    method: str, url: str, params: Mapping[str, Any] | None, body: Any | None
) -> str:
    """Deterministic hash identifying an API call for caching purposes."""
    h = hashlib.sha256()
    h.update(method.upper().encode())
    h.update(b"\x00")
    h.update(url.encode())
    h.update(b"\x00")
    if params:
        h.update(json.dumps(params, sort_keys=True, default=str).encode())
    h.update(b"\x00")
    if body is not None:
        h.update(json.dumps(body, sort_keys=True, default=str).encode())
    return h.hexdigest()


class ResponseCache:
    """gzip-compressed JSON cache: ``<dir>/<sha>.json.gz`` + sidecar metadata."""

    def __init__(self, cache_dir: Path, *, force_refresh: bool = False) -> None:
        self.cache_dir = Path(cache_dir)
        self.force_refresh = force_refresh
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json.gz"

    def get(self, key: str) -> dict[str, Any] | None:
        if self.force_refresh:
            return None
        p = self._path(key)
        if not p.is_file():
            return None
        try:
            with gzip.open(p, "rt", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("cache entry %s unreadable (%s); refetching", p.name, exc)
            return None

    def put(self, key: str, payload: dict[str, Any], meta: Mapping[str, Any]) -> None:
        p = self._path(key)
        tmp = p.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        tmp.replace(p)
        with gzip.open(p.parent / (key + _META_SUFFIX), "wt", encoding="utf-8") as fh:
            json.dump(dict(meta), fh, ensure_ascii=False)

    def stats(self) -> tuple[int, int]:
        """Return (entry_count, total_bytes)."""
        entries = list(self.cache_dir.glob("*.json.gz"))
        entries = [e for e in entries if not e.name.endswith(_META_SUFFIX)]
        return len(entries), sum(e.stat().st_size for e in entries)


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class ApiClient:
    """Low-level, cached, retrying JSON client for one API product.

    Args:
        base_url: Product base URL (from api_map.yaml).
        auth_header_name: Header carrying the key (``X-ApiKey`` for both
            Clarivate products per their OpenAPI definitions).
        api_key: The secret. Kept private; never logged (also scrubbed by the
            logging redaction filter as defence-in-depth).
        cache: Shared :class:`ResponseCache` (one per work dir is fine).
        quota_prefixes: Header prefixes whose integer values are captured,
            e.g. ``["X-REC-", "X-REQ-"]``.
        timeout_seconds / max_retries / backoff_base_seconds /
        backoff_max_seconds / politeness_delay_ms: reliability budget.
        counter: shared :class:`RequestCounter` across clients of one run.
    """

    RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        *,
        base_url: str,
        auth_header_name: str,
        api_key: str | None,
        cache: ResponseCache,
        quota_prefixes: Iterable[str] = ("X-REC-", "X-REQ-"),
        accept: str = "application/json",
        timeout_seconds: float = 60.0,
        max_retries: int = 6,
        backoff_base_seconds: float = 1.0,
        backoff_max_seconds: float = 120.0,
        politeness_delay_ms: int = 150,
        counter: RequestCounter | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth_header_name = auth_header_name
        self._api_key = api_key or ""
        self.cache = cache
        self.quota_prefixes = tuple(p.lower() for p in quota_prefixes)
        self.accept = accept
        self.timeout = timeout_seconds
        self.max_retries = max_retries
        self.backoff_base = backoff_base_seconds
        self.backoff_max = backoff_max_seconds
        self.politeness = politeness_delay_ms / 1000.0
        self.counter = counter or RequestCounter()
        self.session = session or requests.Session()
        self.quota = QuotaSnapshot()
        self._quota_lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._last_request_ts = 0.0

    # -- helpers ------------------------------------------------------------ #
    def _headers(self) -> dict[str, str]:
        # The ONLY place the key is materialised into a request.
        return {
            self.auth_header_name: self._api_key,
            "Accept": self.accept,
            "User-Agent": "muv-bibtex/0.1 (research bibliometrics tool)",
        }

    def _capture_quota(self, headers: Mapping[str, str]) -> None:
        parsed: dict[str, int] = {}
        for k, v in headers.items():
            lk = k.lower()
            if any(lk.startswith(p) for p in self.quota_prefixes):
                try:
                    parsed[lk] = int(v)
                except (TypeError, ValueError):
                    continue
        if parsed:
            with self._quota_lock:
                self.quota.values.update(parsed)
                self.quota.observed_at = time.time()

    def _sleep_politeness(self) -> None:
        if self.politeness <= 0:
            return
        with self._rate_lock:
            wait = self._last_request_ts + self.politeness - time.monotonic()
            self._last_request_ts = max(time.monotonic(), self._last_request_ts + self.politeness)
        if wait > 0:
            time.sleep(wait)

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        """Exponential backoff with full jitter; honours Retry-After."""
        if retry_after:
            try:
                ra = float(retry_after)
                return min(max(ra, 0.5), self.backoff_max)
            except ValueError:
                pass  # HTTP-date form: fall through to computed backoff
        base = min(self.backoff_base * (2**attempt), self.backoff_max)
        return random.uniform(base / 2.0, base)

    # -- core request ------------------------------------------------------- #
    def request_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        method: str = "GET",
        json_body: Any | None = None,
        use_cache: bool = True,
        label: str = "",
    ) -> dict[str, Any]:
        """Perform one cached, retried request and return the parsed JSON body.

        Args:
            path: Endpoint path appended to ``base_url`` (may contain already
                substituted segments, e.g. ``/query/ABC-123``).
            params: Query parameters (``None`` values dropped by caller).
            method: ``GET`` or ``POST``.
            json_body: Body for POST requests.
            use_cache: Set False for deliberately fresh probes.
            label: Human tag used in log lines only.

        Raises:
            CredentialsError-like :class:`ApiAuthenticationError` on 401/403;
            :class:`ApiResponseError` on persistent failure or bad JSON.
        """
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        url = f"{self.base_url}{path}"
        key = request_hash(method, url, clean_params, json_body)

        if use_cache:
            hit = self.cache.get(key)
            if hit is not None:
                self.counter.note_cache_hit()
                log.debug("cache hit %s %s", method, label or path)
                return hit

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._sleep_politeness()
            try:
                self.counter.note_network()
                resp = self.session.request(
                    method=method,
                    url=url,
                    headers=self._headers(),
                    params=clean_params or None,
                    json=json_body,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:  # timeouts, conn errors
                last_exc = exc
                delay = self._backoff(attempt, None)
                log.warning(
                    "network error on %s (%s); retry in %.1fs [%d/%d]",
                    label or path, exc.__class__.__name__, delay, attempt + 1, self.max_retries,
                )
                self.counter.note_retry()
                if attempt < self.max_retries:
                    time.sleep(delay)
                    continue
                raise ApiResponseError(
                    f"network failure calling {label or path}: {exc}", status_code=None
                ) from exc

            self._capture_quota(resp.headers)

            if resp.status_code in (401, 403):
                scope_hint = (
                    "Invalid or missing API key."
                    if resp.status_code == 401
                    else "Valid key but not entitled for this product/endpoint (subscription scope)."
                )
                raise ApiAuthenticationError(
                    f"HTTP {resp.status_code} on {label or path}: {scope_hint} "
                    f"Body: {resp.text[:300]}",
                    status_code=resp.status_code,
                    body=resp.text[:2000],
                )

            if resp.status_code in self.RETRY_STATUSES:
                delay = self._backoff(attempt, resp.headers.get("Retry-After"))
                log.warning(
                    "HTTP %d on %s; backing off %.1fs [%d/%d]",
                    resp.status_code, label or path, delay, attempt + 1, self.max_retries,
                )
                self.counter.note_retry()
                last_exc = ApiResponseError(
                    f"HTTP {resp.status_code} on {label or path}", status_code=resp.status_code
                )
                if attempt < self.max_retries:
                    time.sleep(delay)
                    continue
                raise last_exc

            if resp.status_code >= 400:
                # Non-retryable API error (bad query syntax etc.) — surface body.
                raise ApiResponseError(
                    f"HTTP {resp.status_code} on {label or path}: {resp.text[:500]}",
                    status_code=resp.status_code,
                    body=resp.text[:5000],
                )

            try:
                payload = resp.json()
            except ValueError as exc:
                raise ApiResponseError(
                    f"non-JSON response (HTTP {resp.status_code}) on {label or path}: "
                    f"{resp.text[:200]!r}",
                    status_code=resp.status_code,
                ) from exc

            if isinstance(payload, dict):
                self.cache.put(
                    key,
                    payload,
                    meta={
                        "url": url,
                        "method": method,
                        "params": clean_params,
                        "status": resp.status_code,
                        "quota_headers": dict(self.quota.values),
                        "fetched_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "label": label,
                    },
                )
            return payload

        raise ApiResponseError(f"exhausted retries on {label or path}: {last_exc}")  # pragma: no cover

    # -- convenience -------------------------------------------------------- #
    def get_json(self, path: str, **kw: Any) -> dict[str, Any]:
        return self.request_json(path, method="GET", **kw)

    def post_json(self, path: str, json_body: Any, **kw: Any) -> dict[str, Any]:
        return self.request_json(path, method="POST", json_body=json_body, **kw)

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "ApiClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self.close()


def run_parallel(fn: Callable[[Any], Any], items: list[Any], *, concurrency: int) -> list[Any]:
    """Map *fn* over *items* using at most *concurrency* threads, order preserved.

    Exceptions propagate from the first failing task (fail-fast) — callers
    decide whether to catch per-item.
    """
    if not items:
        return []
    if concurrency <= 1:
        return [fn(it) for it in items]
    with ThreadPoolExecutor(max_workers=min(concurrency, len(items))) as pool:
        return list(pool.map(fn, items))


__all__ = [
    "ApiClient",
    "QuotaSnapshot",
    "RequestCounter",
    "ResponseCache",
    "request_hash",
    "run_parallel",
]
