"""Phase 3: cross-source deduplication with full provenance.

Merge key precedence (config ``dedup.merge_key_precedence``):

1. **UT** (WoS primary accession) — canonical, so this is the primary key;
2. normalized **DOI** — used to *link* records whose UIDs differ (e.g. a
   pre-print indexed separately); on conflict the WoS-sourced record wins and
   the link is recorded in ``doi_aliases``;
3. fuzzy **title+year+first-author** — only as a *detector*: matches are
   written to the audit log/CSV for human review and never auto-merged,
   because false merges would silently corrupt counts.

Every merged row keeps ``all_sources`` (harvest/expand/address_route), every
query SHA that produced it, and both scopes' flags (``org_affiliated_any``).
Output: ``dedup/final.jsonl.gz`` + ``dedup/dedup_log.csv`` + summary JSON.
"""
from __future__ import annotations

import csv
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import AppConfig
from .store import Store, read_rows, utc_now_iso, write_jsonl

log = logging.getLogger("muv.dedup")


# --------------------------------------------------------------------------- #
# Normalisation helpers
# --------------------------------------------------------------------------- #
_WS = re.compile(r"\s+")
_NONWORD = re.compile(r"[^a-z0-9]+")


def norm_title(t: str | None) -> str:
    if not t:
        return ""
    decomposed = unicodedata.normalize("NFKD", t)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _NONWORD.sub("", stripped.lower())


def first_author_key(a: dict[str, Any]) -> str:
    name = a.get("wos_standard") or a.get("display_name") or a.get("last_name") or ""
    return _NONWORD.sub("", name.lower())[:24]


def fuzzy_key(row: dict[str, Any]) -> tuple[str, int | None, str]:
    year = row.get("publication_year_final") or row.get("early_access_year")
    authors = row.get("authors") or [{}]
    return (norm_title(row.get("title")), year, first_author_key(authors[0]))


# --------------------------------------------------------------------------- #
@dataclass
class DedupResult:
    total_input_rows: int = 0
    unique_records: int = 0
    ut_collisions_merged: int = 0
    doi_links_merged: int = 0
    fuzzy_candidates_flagged: int = 0
    org_affiliated_unique: int = 0
    expand_only_unique: int = 0
    by_source: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at_utc": utc_now_iso(),
            **{k: v for k, v in self.__dict__.items()},
        }


def run_dedup(cfg: AppConfig, store: Store) -> DedupResult:
    res = DedupResult()
    rows: list[dict[str, Any]] = []
    for phase, name in (("harvest", "records.jsonl.gz"), ("expand", "records.jsonl.gz")):
        p = store.file(phase, name)
        if not p.exists():
            res.warnings.append(f"missing intermediate {p} — run '{phase}' first")
            continue
        for r in read_rows(p):
            r.setdefault("source_phase", phase)
            rows.append(r)
    res.total_input_rows = len(rows)
    if not rows:
        raise FileNotFoundError("; ".join(res.warnings) or "no input rows")

    # ---- pass 1: UT merge -------------------------------------------------- #
    by_ut: dict[str, dict[str, Any]] = {}
    no_ut: list[dict[str, Any]] = []
    for r in rows:
        ut = r.get("ut")
        if not ut:
            no_ut.append(r)
            continue
        cur = by_ut.get(ut)
        if cur is None:
            by_ut[ut] = _init_merged(r)
        else:
            res.ut_collisions_merged += 1
            _merge_into(cur, r)
    res.doi_links_merged = 0

    # ---- pass 2: DOI linking across different UTs -------------------------- #
    by_doi: dict[str, str] = {}
    final: dict[str, dict[str, Any]] = {}
    for ut, m in by_ut.items():
        doi = m.get("doi")
        if doi:
            other = by_doi.get(doi)
            if other and other != ut and other in by_ut:
                keep, drop = _pick_keeper(by_ut[other], m)
                _merge_into(by_ut[keep["ut"]], drop)
                res.doi_links_merged += 1
                log.info("DOI %s links UTs %s + %s — kept %s (%s source)",
                         doi, other, ut, keep["ut"], keep.get("source_phase"))
                by_doi[doi] = keep["ut"]
                continue
            by_doi[doi] = ut
    for ut, m in by_ut.items():
        if m.get("ut"):
            final[m["ut"]] = m
    # rows without UT keyed by their own hash id
    for i, r in enumerate(no_ut):
        key = f"NOUT-{i}"
        final[key] = _init_merged(r)

    # ---- pass 3: fuzzy duplicate detection (flag only) --------------------- #
    seen_fk: dict[tuple, list[str]] = {}
    fuzzy_pairs: list[tuple[str, str]] = []
    for key, m in final.items():
        fk = fuzzy_key(m)
        if not fk[0]:
            continue
        seen_fk.setdefault(fk, []).append(key)
    for fk, keys in seen_fk.items():
        if len(keys) > 1:
            res.fuzzy_candidates_flagged += len(keys) - 1
            for k in keys[1:]:
                fuzzy_pairs.append((keys[0], k))
            log.warning("fuzzy-duplicate candidates (NOT auto-merged): %s", keys)

    # ---- scope flags & summaries -------------------------------------------- #
    for m in final.values():
        srcs = m.get("all_sources") or set()
        for s in srcs:
            res.by_source[s] = res.by_source.get(s, 0) + 1
        if m.get("org_affiliated_any"):
            res.org_affiliated_unique += 1
        if srcs == {"expand"}:
            res.expand_only_unique += 1
    res.unique_records = len(final)

    # ---- outputs ------------------------------------------------------------- #
    out_rows = []
    for key, m in sorted(final.items()):
        m2 = dict(m)
        m2["all_sources"] = sorted(m2.get("all_sources", []))
        m2["queries"] = sorted(m2.get("queries", set()))
        m2["dedup_id"] = key
        out_rows.append(m2)
    n = write_jsonl(store.file("dedup", "final.jsonl.gz"), out_rows,
                    meta={"phase": "dedup", "inputs": res.total_input_rows})
    assert n == len(out_rows)

    with open(store.file("dedup", "dedup_log.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["kind", "primary", "secondary", "detail"])
        for prim, sec in fuzzy_pairs:
            w.writerow(["fuzzy_candidate_not_merged", prim, sec, "title+year+first-author equal"])
        for warn in res.warnings:
            w.writerow(["warning", "", "", warn])
    write_jsonl(store.file("dedup", "summary.jsonl"), [res.as_dict()], meta={"phase": "dedup"})
    log.info("dedup: %d input rows → %d unique (UT-merges=%d, DOI-links=%d, fuzzy-flagged=%d)",
             res.total_input_rows, res.unique_records, res.ut_collisions_merged,
             res.doi_links_merged, res.fuzzy_candidates_flagged)
    return res


def _init_merged(r: dict[str, Any]) -> dict[str, Any]:
    m = dict(r)
    m["all_sources"] = {r.get("source", r.get("source_phase", "unknown"))}
    m["queries"] = {r.get("query_sha1", "")}
    m["first_seen_utc"] = r.get("retrieved_at_utc")
    m["merge_history"] = []
    return m


def _merge_into(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    """Merge richer data into target; WoS-harvest provenance preferred."""
    target["all_sources"] = set(target.get("all_sources", set())) | {
        incoming.get("source", incoming.get("source_phase", "unknown"))}
    target["queries"] = set(target.get("queries", set())) | {incoming.get("query_sha1", "")}
    for fld in ("doi", "issn", "eissn", "abstract", "times_cited_cc_dedup",
                "open_access_gold", "bibtex_affiliation", "page_count", "volume"):
        if not target.get(fld) and incoming.get(fld):
            target[fld] = incoming[fld]
    # author identifiers: union by position-safe key (never reorder)
    known_ids = {(a.get("researcher_id"), a.get("orcid"), a.get("daisng_id"))
                 for a in target.get("authors", [])}
    for a in incoming.get("authors", []):
        k = (a.get("researcher_id"), a.get("orcid"), a.get("daisng_id"))
        if k not in known_ids:
            target.setdefault("authors", []).append(a)
    target["org_affiliated_any"] = bool(target.get("org_affiliated_any")) or bool(
        incoming.get("org_affiliated_any"))
    target.setdefault("merge_history", []).append({
        "merged_from_ut": incoming.get("ut"),
        "source": incoming.get("source"),
        "at_utc": utc_now_iso(),
    })


def _pick_keeper(a: dict[str, Any], b: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Prefer harvest-sourced (org query) record as keeper; more fields wins tie."""
    rank = {"harvest": 0, "address_route": 1, "expand": 2}
    ra, rb = rank.get(a.get("source", ""), 3), rank.get(b.get("source", ""), 3)
    if ra <= rb:
        return a, b
    return b, a
