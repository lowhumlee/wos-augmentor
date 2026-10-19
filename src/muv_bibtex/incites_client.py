"""InCites Document-Level Metrics API client (v2.0).

Endpoints, parameters and response field paths come from
``config/api_map.yaml: incites`` — extracted verbatim from the live OpenAPI
definition committed at ``docs/incites_openapi_snapshot.json``.  No endpoint is
guessed; if your subscription does not populate an indicator we leave the
field empty and log a warning (see ``optional_indicators``).

Important documented constraints honoured here:
* institution-level endpoints resolve the institution **from the API key** —
  there is no institution parameter (confirmed with the operator: the key's
  profile is MU Varna);
* ``year`` must be >= 1980 (InCites coverage start);
* ``recordcount`` for institution paging is 1..100;
* POST document metrics accepts up to 5000 UTs per request; GET is guarded to
  ``max_uts_per_get`` by us because URL-length behaviour is undocumented;
* ``ver=2`` on every call ("Always use 2").
"""
from __future__ import annotations

import logging
from typing import Any

from .config import AppConfig
from .errors import ApiResponseError
from .http import ApiClient, RequestCounter, ResponseCache
from .record_fields import as_list, dig

log = logging.getLogger("muv.incites")


class IncitesClient:
    """Typed access to InCites document/institution metrics."""

    def __init__(self, cfg: AppConfig, cache: ResponseCache, counter: RequestCounter | None = None):
        self.cfg = cfg
        self.map = cfg.incites_api
        self.p = self.map["params"]
        self.paths = self.map["response_paths"]
        qh = self.map.get("quota_headers", {})
        prefixes = as_list(qh.get("capture_all_matching_prefix")) or ["X-REC-", "X-REQ-"]
        self.http = ApiClient(
            base_url=self.map["base_url"],
            auth_header_name=self.map["auth_header"],
            api_key=cfg.incites_key.get_secret_value() if cfg.incites_key else None,
            cache=cache,
            quota_prefixes=[str(x) for x in prefixes],
            accept=self.map.get("accept", "application/json"),
            timeout_seconds=cfg.resources.timeout_seconds,
            max_retries=cfg.resources.max_retries,
            backoff_base_seconds=cfg.resources.backoff_base_seconds,
            backoff_max_seconds=cfg.resources.backoff_max_seconds,
            politeness_delay_ms=max(cfg.resources.politeness_delay_ms, 300),
            counter=counter or RequestCounter(),
        )

    @property
    def available(self) -> bool:
        return self.cfg.incites_key is not None

    # ------------------------------------------------------------------ #
    def _ep(self, name: str) -> str:
        path = self.map["endpoints"][name]
        return path.replace("{format}", self.map.get("format_segment", "json"))

    def _rval_rows(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Flatten ``api[].rval[]`` envelopes into one row list."""
        rows: list[dict[str, Any]] = []
        for block in as_list(dig(payload, self.paths["envelope"])):
            if isinstance(block, dict):
                rows.extend(r for r in as_list(dig(block, "rval")) if isinstance(r, dict))
        return rows

    # ---- health / verification ---------------------------------------- #
    def last_updated(self) -> dict[str, Any]:
        """InCitesLastUpdated — proves key validity + data vintage (also used
        by ``discover`` as the runtime reachability check)."""
        payload = self.http.get_json(self._ep("last_updated"), params={"ver": self.p["ver"]},
                                     label="incites.last_updated")
        rows = self._rval_rows(payload)
        return rows[0] if rows else payload

    def check_valid_uts(self, uts: list[str]) -> dict[str, bool]:
        """CheckValidUTs: which of these UTs exist in InCites' WOS core?"""
        if not self.available or not uts:
            return {}
        out: dict[str, bool] = {}
        chunk = int(self.p.get("max_uts_per_get", 1000))
        ep = self._ep("check_valid_uts")
        for i in range(0, len(uts), chunk):
            part = uts[i : i + chunk]
            payload = self.http.get_json(
                ep, params={"UT": ",".join(part), "ver": self.p["ver"]},
                label=f"incites.check_uts[{len(part)}]",
            )
            for row in self._rval_rows(payload):
                ut = str(row.get(self.paths["accession_number"], "")).strip()
                val = row.get("VALID_UT", row.get("valid_ut"))
                if ut:
                    out[ut] = str(val).lower() in {"true", "1", "y"}
        return out

    # ---- document metrics -------------------------------------------- #
    def document_metrics_by_ut(self, uts: list[str]) -> dict[str, dict[str, Any]]:
        """Per-UT indicators via the documented POST endpoint (<=5000 UTs/req).

        Returns ``{UT: {TIMES_CITED, IMPACT_FACTOR, JNCI, AVG_CNCI, OPEN_ACCESS,
        PERCENTILE:[...], DOCUMENT_TYPE}}``; missing indicators are simply
        absent keys — callers warn and leave outputs empty.
        """
        if not self.available:
            log.warning("INCITES_API_KEY not set — document metrics skipped; "
                        "JIF/quartile fields will remain empty (never estimated).")
            return {}
        result: dict[str, dict[str, Any]] = {}
        chunk = int(self.p.get("max_uts_per_request", 5000))
        ep = self._ep("metrics_by_ut_post")
        body_key = self.p["ut"]
        for i in range(0, len(uts), chunk):
            part = uts[i : i + chunk]
            try:
                payload = self.http.post_json(
                    ep, {body_key: part, "ver": self.p["ver"]},
                    label=f"incites.doc_metrics[{len(part)} UTs]",
                )
            except ApiResponseError as exc:
                log.error("document metrics chunk failed (%d UTs): %s", len(part), exc)
                continue
            for row in self._rval_rows(payload):
                ut = str(row.get(self.paths["accession_number"], "")).strip()
                if not ut:
                    continue
                result[normalize_ut_key(ut)] = row
        missing = [u for u in (normalize_ut_key(x) for x in uts) if u not in result]
        if missing:
            log.warning(
                "InCites returned no metrics row for %d/%d UTs (not indexed, or indicator "
                "outside subscription scope). Affected fields stay EMPTY. First few: %s",
                len(missing), len(uts), missing[:5],
            )
        return result

    # ---- institution metrics ----------------------------------------- #
    def institution_record_count(self, year: int) -> int:
        """DocumentLevelMetricsByInstitutionIdRecordCount for one year (>=1980)."""
        if year < self.cfg.time_span.min_incites_year:
            log.debug("year %s below InCites coverage start — skipping count", year)
            return 0
        payload = self.http.get_json(
            self._ep("metrics_by_institution_count"),
            params={"year": year, "ver": self.p["ver"]},
            label=f"incites.inst_count[{year}]",
        )
        rows = self._rval_rows(payload)
        total = 0
        for r in rows:
            v = dig(r, self.paths["record_count_value"].split(".")[-1])
            try:
                total += int(v)
            except (TypeError, ValueError):
                continue
        return total

    def institution_metrics(self, year: int) -> list[dict[str, Any]]:
        """Paged document-level metrics for the whole institution-year (1..100/req)."""
        if not self.available:
            return []
        if year < self.cfg.time_span.min_incites_year:
            return []
        ep = self._ep("metrics_by_institution")
        rec_max = int(self.p.get("max_record_count", 100))
        start = int(self.p.get("starting_record", 1))
        out: list[dict[str, Any]] = []
        while True:
            payload = self.http.get_json(
                ep,
                params={"year": year, self.p["starting_record"]: start,
                        self.p["record_count"]: rec_max, "ver": self.p["ver"]},
                label=f"incites.inst_metrics[{year}@{start}]",
            )
            rows = self._rval_rows(payload)
            if not rows:
                break
            out.extend(rows)
            if len(rows) < rec_max:
                break
            start += rec_max
        return out

    # ---- indicator helpers (used by stats) --------------------------- #
    @staticmethod
    def extract_indicator(row: dict[str, Any], paths: dict[str, Any], name: str) -> Any:
        key = paths[name]
        if "[]." in key or key.endswith("[]"):
            vals = as_list(dig(row, key))
            return vals or None
        return row.get(key.split(".")[-1])

    def percentile_band(self, row: dict[str, Any]) -> dict[str, Any]:
        """Best-percentile CATPERCENTILE → derived Q-band.

        NOTE (flagged in README/logs): this is a *derived percentile band*,
        NOT the official JCR quartile label — InCites v2.0 exposes no quartile
        field. Band boundaries come from config ``statistics.quartile_from_percentile``.
        """
        rows = as_list(dig(row, self.paths["percentile"]))
        best = None
        for pr in rows:
            if not isinstance(pr, dict):
                continue
            if str(dig(pr, "IS_BEST") or dig(pr, "$.IS_BEST") or "").lower() == "true":
                best = pr
                break
        if best is None and rows and isinstance(rows[0], dict):
            best = rows[0]
        if best is None:
            return {}
        cp = dig(best, "CATPERCENTILE")
        try:
            cp_f = float(cp)
        except (TypeError, ValueError):
            cp_f = None
        band = None
        if cp_f is not None:
            mapping = self.cfg.statistics.quartile_from_percentile or {}
            for label, hi in sorted(mapping.items(), key=lambda kv: float(kv[1])):
                if cp_f <= float(hi):
                    band = label
                    break
        return {
            "catpercentile": cp_f,
            "derived_band": band,
            "subject": dig(best, "SUBJECT"),
            "code": dig(best, "CODE"),
            "cnci": dig(best, "CNCI"),
            "is_best": True,
            "source_note": "derived from InCites CATPERCENTILE; not official JCR quartile",
        }


def normalize_ut_key(ut: str) -> str:
    """InCites ACCESSION_NUMBER has no 'WOS:' prefix and may carry spaces/dashes."""
    s = ut.strip().upper()
    if s.startswith("WOS:"):
        s = s[4:]
    return s.replace(" ", "")
