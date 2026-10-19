"""Web of Science Expanded API client — thin, config-driven wrapper.

All endpoint paths / parameter names / response field paths come from
``config/api_map.yaml`` (loaded into ``AppConfig.wos_api``).  This module adds:

* ``count_records``   — cheap total for one query (``count=0`` probe; used by
  ``count``/dry-run; per the spec a zero-count search still returns QueryResult);
* ``search_all``      — pages a query to completion with completeness checks
  (retrieved == RecordsFound) and yields raw REC dicts;
* view-mode handling  — ``auto`` = try quota-free ``SR`` first for harvest,
  fall back to ``FR`` when SR payloads lack needed fields;
* POST fallback when the GET URL would exceed ``limits.max_query_chars``.

Record *parsing* never happens here — see :mod:`record_fields`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from .config import AppConfig
from .errors import ApiResponseError, CompletenessError
from .http import ApiClient, RequestCounter, ResponseCache
from .query_builder import build_page_plan, exceeds_offset_cap, validate_query_syntax
from .record_fields import as_list, dig

log = logging.getLogger("muv.wos")


@dataclass
class SearchResultMeta:
    """Metadata returned alongside paged records."""

    query: str
    records_found: int
    records_searched: int | None
    query_id: str | None
    retrieved: int
    option_view: str
    complete: bool


class WosClient:
    """High-level WoS Expanded API operations used by all phases."""

    def __init__(self, cfg: AppConfig, cache: ResponseCache, counter: RequestCounter | None = None):
        self.cfg = cfg
        self.map = cfg.wos_api
        p = self.map["params"]
        self.p = p
        self.paths = self.map["response_paths"]
        self.counter = counter or RequestCounter()
        qh = self.map.get("quota_headers", {})
        prefixes = as_list(qh.get("capture_all_matching_prefix")) or ["X-REC-", "X-REQ-"]
        self.http = ApiClient(
            base_url=self.map["base_url"],
            auth_header_name=self.map["auth_header"],
            api_key=cfg.wos_key.get_secret_value() if cfg.wos_key else None,
            cache=cache,
            quota_prefixes=[str(x) for x in prefixes],
            accept=self.map.get("accept", "application/json"),
            timeout_seconds=cfg.resources.timeout_seconds,
            max_retries=cfg.resources.max_retries,
            backoff_base_seconds=cfg.resources.backoff_base_seconds,
            backoff_max_seconds=cfg.resources.backoff_max_seconds,
            politeness_delay_ms=cfg.resources.politeness_delay_ms,
            counter=self.counter,
        )
        self.editions = self.map.get("editions_default")

    # ------------------------------------------------------------------ #
    # low-level calls
    # ------------------------------------------------------------------ #
    def _ep(self, name: str) -> str:
        return self.map["endpoints"][name]

    def _base_params(self, usr_query: str, count: int, first_record: int | None) -> dict[str, Any]:
        params: dict[str, Any] = {
            self.p["database_id"]: self.p["database_id_value"],
            self.p["usr_query"]: usr_query,
            self.p["count"]: count,
        }
        if first_record is not None:
            params[self.p["first_record"]] = first_record
        if self.editions:
            params[self.p["edition"]] = self.editions
        return params

    def _fetch_page(self, usr_query: str, count: int, first_record: int | None, option_view: str) -> dict[str, Any]:
        params = self._base_params(usr_query, count, first_record)
        params[self.p["option_view"]] = option_view
        max_chars = int(self.map.get("limits", {}).get("max_query_chars", 1800))
        if len(usr_query) > max_chars:
            log.info("query length %d > %d → using documented POST search", len(usr_query), max_chars)
            body = dict(params)
            body.pop(self.p["usr_query"])
            body["usrQuery"] = usr_query  # same param names inside the POST body per spec
            return self.http.post_json(self._ep("search_post"), body, label="wos.search.POST")
        return self.http.get_json(self._ep("search_get"), params=params, label="wos.search.GET")

    @staticmethod
    def _extract(payload: dict[str, Any], paths: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        recs = [r for r in as_list(dig(payload, paths["rec_list"])) if isinstance(r, dict)]
        qr = dig(payload, paths["query_result"], default={}) or {}
        meta = {
            "records_found": qr.get(as_name(paths["records_found"]), None),
            "records_searched": qr.get(as_name(paths["records_searched"]), None),
            "query_id": qr.get(as_name(paths["query_id"]), None),
        }
        # tolerate case variants (RecordsFound vs recordsfound) seen across API versions
        if meta["records_found"] is None:
            meta["records_found"] = _ci_get(qr, "RecordsFound")
        if meta["records_searched"] is None:
            meta["records_searched"] = _ci_get(qr, "RecordsSearched")
        if meta["query_id"] is None:
            meta["query_id"] = _ci_get(qr, "QueryID")
        return recs, meta

    # ------------------------------------------------------------------ #
    # public operations
    # ------------------------------------------------------------------ #
    def count_records(self, usr_query: str, *, option_view: str = "SR") -> SearchResultMeta:
        """Total-match probe that does **not** retrieve records.

        Uses the documented ``count=0`` request; QueryResult.RecordsFound still
        comes back, and per the OpenAPI docs SR requests do not consume the
        annual record quota.  This powers ``muv-bibtex count`` (dry-run).
        """
        warn = validate_query_syntax(usr_query)
        for w in warn:
            log.warning("query syntax check: %s :: %.90s", w, usr_query)
        payload = self._fetch_page(usr_query, 0, None, option_view)
        _, meta = self._extract(payload, self.paths)
        found = int(meta["records_found"] or 0)
        return SearchResultMeta(
            query=usr_query,
            records_found=found,
            records_searched=_to_int(meta["records_searched"]),
            query_id=str(meta["query_id"]) if meta["query_id"] else None,
            retrieved=0,
            option_view=option_view,
            complete=True,
        )

    def search_all(
        self,
        usr_query: str,
        *,
        option_view: str = "FR",
        on_page: Callable[[int, int], None] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Page through *usr_query* to completion; yield raw REC dicts.

        Completeness contract: number yielded must equal RecordsFound unless
        the offset cap forces truncation (then :class:`CompletenessError` so
        callers split the query instead of silently losing records).
        """
        page_size = min(int(self.cfg.resources.page_size), int(self.p.get("max_count", 100)))
        first_meta: SearchResultMeta | None = None
        yielded = 0
        offsets = [1]
        idx = 0
        while idx < len(offsets):
            off = offsets[idx]
            payload = self._fetch_page(usr_query, page_size, off, option_view)
            recs, meta = self._extract(payload, self.paths)
            found = int(meta["records_found"] or 0)
            if first_meta is None:
                first_meta = SearchResultMeta(
                    query=usr_query,
                    records_found=found,
                    records_searched=_to_int(meta["records_searched"]),
                    query_id=str(meta["query_id"]) if meta["query_id"] else None,
                    retrieved=0,
                    option_view=option_view,
                    complete=False,
                )
                if exceeds_offset_cap(found):
                    raise CompletenessError(
                        f"query matches {found} records but paging stops at "
                        f"firstRecord={self.p.get('max_first_record', 100000)}; split the query "
                        "(by year, then by month/document type)."
                    )
                offsets = build_page_plan(found, page_size).offsets
            for r in recs:
                yield r
                yielded += 1
            if on_page:
                on_page(yielded, found)
            idx += 1
            if not recs and yielded < found:
                raise CompletenessError(
                    f"empty page at offset {off} although RecordsFound={found}, yielded={yielded}"
                )
        if first_meta is not None and yielded != first_meta.records_found:
            raise CompletenessError(
                f"incomplete retrieval for query [{usr_query[:120]}…]: "
                f"yielded={yielded} != RecordsFound={first_meta.records_found}"
            )

    def fetch_by_ut(self, ut: str, *, option_view: str = "FR") -> dict[str, Any] | None:
        """Single record via the documented ``/id/{uniqueId}`` endpoint."""
        try:
            payload = self.http.get_json(
                self._ep("record_by_uid").replace("{unique_id}", ut),
                params={self.p["database_id"]: self.p["database_id_value"],
                        self.p["option_view"]: option_view},
                label=f"wos.record({ut})",
            )
        except ApiResponseError as exc:
            log.warning("record fetch failed for %s: %s", ut, exc)
            return None
        recs, _ = self._extract(payload, self.paths)
        if recs:
            return recs[0]
        # single-record responses may wrap differently; take any dict with static_data
        if isinstance(payload, dict):
            sd = dig(payload, "static_data")
            if sd:
                return payload
        return None

    # ---- quota helpers ------------------------------------------------ #
    def remaining_annual_records(self) -> int | None:
        header = self.map.get("quota_headers", {}).get("annual_records_remaining")
        return self.http.quota.remaining_annual_records(str(header) if header else None)

    def estimate_quota_cost(self, n_full_records: int) -> int:
        """Full-record (FR) retrieval consumes ~n records of annual quota.

        SR short-view requests are documented as not counting against quota;
        we therefore charge only FR retrievals.  Returned value is what the
        run will *approximately* consume.
        """
        return int(n_full_records)

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "WosClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# --------------------------------------------------------------------------- #
def as_name(dotted: str) -> str:
    """Last segment of a dotted response path (the immediate JSON key)."""
    return dotted.split(".")[-1].rstrip("[]")


def _ci_get(dct: Any, key: str) -> Any:
    if not isinstance(dct, dict):
        return None
    if key in dct:
        return dct[key]
    lk = key.lower()
    for k, v in dct.items():
        if k.lower() == lk:
            return v
    return None


def _to_int(v: Any) -> int | None:
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None
