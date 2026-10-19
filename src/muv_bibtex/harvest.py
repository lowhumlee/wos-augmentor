"""Phases 1–2: organization harvest and identifier-based expansion.

``harvest`` (phase 1)
    For every publication year in range, run the ``OG=`` organization-enhanced
    query (plus optional ``AD=`` route when enabled).  Retrieval is complete by
    construction: totals come from a quota-free ``count=0/SR`` probe; if a year
    exceeds the split threshold we subdivide by month (DA= ranges) and then by
    document type; each leaf query is paged to completion with the
    retrieved==RecordsFound assertion inside :meth:`WosClient.search_all`.
    Full records (FR — or SR-first per ``harvest_view_mode=auto``) are stored
    raw *and* flattened; author identifiers (ResearcherID / ORCID / DAISng ID)
    are harvested into ``identifiers.jsonl`` together with an affiliation-
    confirmation flag (flag only — nothing is filtered out).

``expand`` (phase 2)
    For every harvested identifier, search WoS for **all** publications bearing
    it (RI=/OI=/DA= chunked queries, year-split).  Results are kept even when
    the affiliation field no longer mentions the organization — per the
    operator's requirement that non-org-connected co-authorship records are
    included (toggle ``include_non_org_connected_records``); they carry
    ``org_affiliated=false`` so stats can report both scopes.

Resumability: per-year segments in ``_done.json`` + cached HTTP responses.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import AppConfig
from .errors import CompletenessError
from .http import RequestCounter, ResponseCache, run_parallel
from .query_builder import (
    chunk_ids_by_query_length,
    dt_clause,
    format_researcherid,
    identifier_query,
    org_query,
)
from .record_fields import (
    AuthorHit,
    extract_addresses,
    extract_authors,
    affiliation_confirmed,
    best_affiliation_string,
    dig,
    identifiers_map,
    normalize_doi,
    parse_ut,
    pub_years,
    title_of,
    ut_digits,
)
from .store import Store, append_jsonl_segment, utc_now_iso, write_jsonl
from .wos_client import WosClient

log = logging.getLogger("muv.harvest")


# --------------------------------------------------------------------------- #
@dataclass
class HarvestStats:
    years_total: int = 0
    years_done_from_ledger: int = 0
    records_raw: int = 0
    unique_uids: int = 0
    splits_by_month: int = 0
    splits_by_dt: int = 0
    incomplete_years: list[str] = field(default_factory=list)
    identifiers: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _seg_key(prefix: str, payload: str) -> str:
    return f"{prefix}:{hashlib.sha1(payload.encode()).hexdigest()[:16]}"


# --------------------------------------------------------------------------- #
# Record flattening (raw JSON stays in the HTTP cache; we store what we need)
# --------------------------------------------------------------------------- #
def flatten_record(rec: dict[str, Any], cfg: AppConfig, source: str, query: str) -> dict[str, Any] | None:
    """Turn a raw REC dict into our canonical intermediate row (lossy-light)."""
    paths = cfg.wos_api["response_paths"]
    uid = parse_ut(str(dig(rec, paths["uid"]) or ""))
    if not uid:
        log.warning("record without UID in %s query [%.80s…] — skipped", source, query)
        return None
    authors = extract_authors(rec, paths)
    addresses = extract_addresses(rec, paths)
    ids = identifiers_map(rec, paths)
    py, ea = pub_years(rec, paths)
    tc = dig(rec, paths["tc_dedup_total"])
    row: dict[str, Any] = {
        "ut": uid,
        "ut_digits": ut_digits(uid),
        "source": source,                      # harvest | expand | address_route
        "query_sha1": hashlib.sha1(query.encode()).hexdigest()[:16],
        "retrieved_at_utc": utc_now_iso(),
        "title": title_of(rec, paths, "item"),
        "journal": title_of(rec, paths, "container"),
        "publication_year_final": py,
        "early_access_year": ea,
        "volume": _txt(dig(rec, paths["volume"])),
        "issue": _txt(dig(rec, paths["issue"])),
        "page_begin": _txt(dig(rec, paths["pages_begin"])),
        "page_end": _txt(dig(rec, paths["pages_end"])),
        "page_count": _txt(dig(rec, paths["page_count"])),
        "pubtype": _txt(dig(rec, paths["pubtype"])),
        "doctypes": [_txt(d) for d in _lst(dig(rec, paths["doctypes"])) if d],
        "coverdate": _txt(dig(rec, paths["coverdate"])),
        "sortdate": _txt(dig(rec, paths["sortdate"])),
        "doi": normalize_doi(ids.get("DOI")),
        "issn": ids.get("ISSN"),
        "eissn": ids.get("EISSN"),
        "isbn": ids.get("ISBN"),
        "times_cited_cc_dedup": int(tc) if str(tc or "").isdigit() else None,
        "open_access_gold": _yesno(dig(rec, paths["oas_gold"])),
        "refs_count": _txt(dig(rec, paths["refs_count"])),
        "languages": [_txt(l) for l in _lst(dig(rec, paths["language"])) if l],
        "category_subjects": [
            {"code": _txt(dig(s, "$.code") or attr_of(s, "code")), "ascatype": attr_of(s, "ascatype")}
            for s in _lst(dig(rec, paths["subjects"]))
        ],
        "keywords_author": [_txt(k) for k in _lst(dig(rec, paths["keywords_author"])) if _txt(k)],
        "keywords_plus": [_txt(k) for k in _lst(dig(rec, paths["keywords_plus"])) if _txt(k)],
        "publisher": _txt(dig(rec, paths["publisher_name"])),
        "publisher_address": _txt(dig(rec, paths["publisher_address"])),
        "abstract": _first_para(dig(rec, paths["abstract_text"])) or _first_para(dig(rec, paths["abstract_text_alt"])),
        "grant_agencies": [_txt(g) for g in _lst(dig(rec, paths["grant_agency"])) if g],
        "authors": [
            {
                "seq": a.seq,
                "display_name": a.display_name,
                "wos_standard": a.wos_standard,
                "last_name": a.last_name,
                "first_name": a.first_name,
                "researcher_id": a.researcher_id,
                "orcid": a.orcid,
                "daisng_id": a.daisng_id,
                "reprint": a.reprint,
                "addr_nos": a.addr_nos,
                "org_affiliated": affiliation_confirmed(a, addresses, cfg.org_match_terms()),
            }
            for a in authors
        ],
        "addresses": [
            {"addr_no": e.addr_no, "full_address": e.full_address,
             "organizations": e.organizations, "city": e.city, "country": e.country}
            for e in sorted(addresses.values(), key=lambda x: str(x.addr_no))
        ],
        "org_affiliated_any": any(
            affiliation_confirmed(a, addresses, cfg.org_match_terms()) for a in authors
        ),
        "bibtex_affiliation": best_affiliation_string(authors, addresses, cfg.org_match_terms()),
    }
    return row


def _txt(v: Any) -> str | None:
    from .record_fields import text_of

    return text_of(v)


def attr_of(node: Any, name: str) -> str | None:
    from .record_fields import attr

    v = attr(node, name)
    return str(v) if v is not None else None


def _lst(v: Any) -> list[Any]:
    from .record_fields import as_list

    return as_list(v)


def _yesno(v: Any) -> bool | None:
    s = (_txt(v) or "").lower()
    if s in {"y", "yes", "true", "1"}:
        return True
    if s in {"n", "no", "false", "0"}:
        return False
    return None


def _first_para(v: Any) -> str | None:
    """abstract_text.p may be str or list of paragraph strings."""
    parts = _lst(v)
    texts = [t for t in (_txt(p) for p in parts) if t]
    if not texts:
        return _txt(v)
    return " ".join(texts)


# --------------------------------------------------------------------------- #
# Phase 1 — harvest
# --------------------------------------------------------------------------- #
MONTH_PAIRS = [(1, 3), (4, 6), (7, 9), (10, 12)]
COMMON_DT = ["Article", "Review", "Letter", "Editorial Material", "Proceedings Paper",
             "Article Early Access", "Meeting Abstract", "Book Review", "Book", "Erratum", "Data Paper"]


def _leaf_queries(cfg: AppConfig, year: int, variants: list[str]) -> list[dict[str, str]]:
    """Year → month quarters → (if still too big, handled by caller) DT splits."""
    og = cfg.query_tag("organization_enhanced")
    py = cfg.query_tag("publication_year")
    leaves = [{"label": f"year={year}", "query": org_query(variants, og, year=year, py_tag=py)}]
    return leaves


def run_harvest(
    cfg: AppConfig,
    wos: WosClient,
    store: Store,
    *,
    dry_run: bool = False,
    concurrency: int | None = None,
) -> HarvestStats:
    stats = HarvestStats()
    ledger = store.ledger("harvest")
    rec_path = store.file("harvest", "records.jsonl.gz")
    idf_path = store.file("harvest", "identifiers.jsonl")
    count_path = store.file("harvest", "year_counts.csv")
    if cfg.toggles.force_refresh and not dry_run:
        ledger = _reset_ledger(store, "harvest")
        for p in (rec_path, idf_path):
            if p.exists():
                p.unlink()
    if not dry_run and not rec_path.exists():
        write_jsonl(rec_path, [], meta={"phase": "harvest", "org": cfg.organization.display_name})
        write_jsonl(Path(count_path).with_suffix(".jsonl"), [], meta={"phase": "harvest-counts"})

    variants = list(cfg.organization.name_variants)
    if cfg.organization.address_route.enabled and cfg.organization.address_route.query:
        variants.append(cfg.organization.address_route.query)
    years = cfg.effective_years()
    stats.years_total = len(years)
    conc = concurrency or cfg.resources.concurrency
    counts_rows: list[dict[str, Any]] = []

    def one_year(year: int) -> dict[str, Any]:
        key = _seg_key("harvest", f"{year}|{variants}")
        result: dict[str, Any] = {"year": year, "found": 0, "retrieved": 0, "status": "ok"}
        if not dry_run and ledger.is_done(key):
            stats.years_done_from_ledger += 1
            result["status"] = "skipped(done)"
            return result
        q = _leaf_queries(cfg, year, variants)[0]["query"]
        try:
            probe = wos.count_records(q)
        except CompletenessError as exc:
            result["status"] = f"probe-error: {exc}"
            return result
        result["found"] = probe.records_found
        if dry_run:
            return result
        if probe.records_found == 0:
            ledger.mark_done(key, found=0)
            return result
        retrieved = 0
        rows: list[dict[str, Any]] = []
        id_rows: list[dict[str, Any]] = []
        view = _effective_view(cfg, wos)
        try:
            if probe.records_found > int(cfg.wos_api.get("limits", {}).get("year_split_cap", 4500)):
                raise _NeedsSplit(f"year {year}: {probe.records_found} records > split cap")
            for rec in wos.search_all(q, option_view=view):
                flat = flatten_record(rec, cfg, "harvest", q)
                if flat:
                    rows.append(flat)
                    retrieved += 1
        except _NeedsSplit:
            stats.splits_by_month += 1
            for m1, m2 in MONTH_PAIRS:
                mq = org_query(variants, cfg.query_tag("organization_enhanced"),
                               year=year, py_tag=cfg.query_tag("publication_year"),
                               month_span=(m1, m2))
                mprobe = wos.count_records(mq)
                if mprobe.records_found == 0:
                    continue
                if mprobe.records_found > int(cfg.wos_api.get("limits", {}).get("year_split_cap", 4500)):
                    # tertiary split by document type
                    stats.splits_by_dt += 1
                    for dt in COMMON_DT:
                        dq = mq + " AND " + dt_clause(cfg.query_tag("document_type"), [dt])
                        dprobe = wos.count_records(dq)
                        if dprobe.records_found == 0:
                            continue
                        for rec in wos.search_all(dq, option_view=view):
                            flat = flatten_record(rec, cfg, "harvest", dq)
                            if flat:
                                rows.append(flat)
                                retrieved += 1
                        result["found"] += dprobe.records_found
                else:
                    for rec in wos.search_all(mq, option_view=view):
                        flat = flatten_record(rec, cfg, "harvest", mq)
                        if flat:
                            rows.append(flat)
                            retrieved += 1
        # completeness assertion at segment level
        if retrieved != probe.records_found:
            result["status"] = (
                f"INCOMPLETE: retrieved {retrieved} != RecordsFound {probe.records_found}"
            )
            stats.incomplete_years.append(str(year))
            log.error("year %d incomplete (%d/%d) — NOT marking done; rerun will retry",
                      year, retrieved, probe.records_found)
        else:
            ledger.mark_done(key, found=probe.records_found, retrieved=retrieved)
        for row in rows:
            for a in row["authors"]:
                for kind, tag in (("researcher_id", "RI"), ("orcid", "OI"), ("daisng_id", "DA")):
                    if a.get(kind):
                        id_rows.append({
                            "identifier_type": tag,
                            "identifier": a[kind],
                            "author_display_name": a["display_name"],
                            "author_last_first": _last_first(a),
                            "ut": row["ut"],
                            "org_affiliated_author": a["org_affiliated"],
                        })
        if rows:
            append_jsonl_segment(rec_path, rows)
        if id_rows:
            append_jsonl_segment(idf_path, id_rows)
        result["retrieved"] = retrieved
        return result

    results = run_parallel(one_year, years, concurrency=conc)
    for r in results:
        counts_rows.append(r)
        stats.records_raw += r.get("retrieved", 0)
    if dry_run:
        total_found = sum(r.get("found", 0) for r in results)
        remaining = wos.remaining_annual_records()
        est = wos.estimate_quota_cost(total_found)
        print("\n=== DRY-RUN: match counts per year (no records retrieved) ===")
        for r in results:
            if r["found"]:
                print(f"  {r['year']}: {r['found']:>6}  ({r['status']})")
        print(f"\nTotal matches: {total_found}")
        print(f"Estimated full-record quota consumption: ~{est} records")
        print(f"Remaining annual record quota (header):  {remaining if remaining is not None else 'unknown (header not yet observed)'}")
        if remaining is not None and cfg.quota.warn_when_estimated_exceeds_remaining and est > remaining:
            print(f"\n*** WARNING: estimate ({est}) EXCEEDS remaining quota ({remaining}). ***")
            print("    Reduce year range (--start-year/--end-year), or retrieve short views only.")
        elif remaining is not None:
            print(f"Quota headroom after run: ~{remaining - est} records")
        write_jsonl(count_path.with_suffix(".jsonl"), counts_rows, meta={"phase": "harvest-dryrun"})
        return stats

    # dedupe UIDs within harvest scope for reporting
    uids = {row["ut"] for row in _iter_existing(rec_path)}
    stats.unique_uids = len(uids)
    id_summary: dict[str, int] = {}
    for row in _iter_existing(idf_path, gz=False):
        id_summary[row["identifier_type"]] = id_summary.get(row["identifier_type"], 0) + 1
    stats.identifiers = id_summary
    write_jsonl(count_path.with_suffix(".jsonl"), counts_rows, meta={"phase": "harvest"})
    _write_year_csv(count_path, counts_rows)
    log.info(
        "harvest done: raw=%d unique=%d monthsplits=%d dtsplits=%d incomplete=%s",
        stats.records_raw, stats.unique_uids, stats.splits_by_month, stats.splits_by_dt,
        stats.incomplete_years or "none",
    )
    return stats


def _effective_view(cfg: AppConfig, wos: WosClient) -> str:
    """auto ⇒ FR for real retrieval (SR payloads lack orcid/r_id reliably).

    The SR-first optimisation applies to probes only (count/dry-run); full
    records are fetched once and cached, matching fetch_full_records_once.
    """
    mode = cfg.toggles.harvest_view_mode
    if mode == "fr":
        return "FR"
    if mode == "sr":
        return "SR"
    return "FR"


class _NeedsSplit(Exception):
    pass


def _last_first(a: dict[str, Any]) -> str:
    if a.get("wos_standard"):
        return a["wos_standard"]
    if a.get("last_name"):
        return f"{a['last_name']}, {a.get('first_name') or ''}".strip().rstrip(",")
    return a.get("display_name") or ""


def _iter_existing(path: Path, gz: bool | None = None):
    from .store import read_rows

    if not Path(path).exists():
        return []
    return read_rows(Path(path))


def _reset_ledger(store: Store, phase: str):
    ledg = store.ledger(phase)
    ledg.segments = {}
    ledg._flush()
    return ledg


def _write_year_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("year,found,retrieved,status\n")
        for r in rows:
            fh.write(f"{r['year']},{r.get('found',0)},{r.get('retrieved',0)},\"{r.get('status','')}\"\n")


# --------------------------------------------------------------------------- #
# Phase 2 — expand
# --------------------------------------------------------------------------- #
def run_expand(cfg: AppConfig, wos: WosClient, store: Store, *, limit_identifiers: int | None = None) -> dict[str, Any]:
    """Identifier-driven expansion with provenance tracking."""
    ledger = store.ledger("expand")
    rec_path = store.file("expand", "records.jsonl.gz")
    prov_path = store.file("expand", "provenance.jsonl")
    if cfg.toggles.force_refresh:
        ledger = _reset_ledger(store, "expand")
        for p in (rec_path, prov_path):
            if p.exists():
                p.unlink()
    if not rec_path.exists():
        write_jsonl(rec_path, [], meta={"phase": "expand"})

    # collect unique identifiers from harvest
    ident_map: dict[tuple[str, str], dict[str, Any]] = {}
    for row in _iter_existing(store.file("harvest", "identifiers.jsonl"), gz=False):
        k = (row["identifier_type"], row["identifier"])
        cur = ident_map.setdefault(k, {**row, "hit_count": 0})
        cur["hit_count"] += 1
    if not ident_map:
        log.warning("expand: no identifiers harvested — nothing to expand (check harvest output)")
        return {"identifiers": 0, "records_new": 0}

    order = sorted(ident_map.values(), key=lambda r: -r["hit_count"])
    if limit_identifiers:
        order = order[:limit_identifiers]

    tags = {
        "RI": cfg.query_tag("researcher_id"),
        "OI": cfg.query_tag("orcid"),
        "DA": cfg.query_tag("daisng_id"),
    }
    max_chars = int(cfg.expansion.chunk_max_query_chars)
    known_uts = {r["ut"] for r in _iter_existing(store.file("harvest", "records.jsonl.gz"))}
    prov_rows: list[dict[str, Any]] = []
    done_segments = 0
    view = _effective_view(cfg, wos)

    by_type: dict[str, list[dict[str, Any]]] = {"RI": [], "OI": [], "DA": []}
    for entry in order:
        by_type.setdefault(entry["identifier_type"], []).append(entry)

    years = cfg.effective_years()
    for idtype, entries in by_type.items():
        if not entries:
            continue
        tag = tags[idtype]
        formatted: dict[str, str] = {}
        for e in entries:
            if idtype == "RI":
                formatted[e["identifier"]] = format_researcherid(e.get("author_last_first", ""), e["identifier"])
            else:
                formatted[e["identifier"]] = e["identifier"]
        ids = [formatted[e["identifier"]] for e in entries]
        id_by_formatted = {v: k for k, v in formatted.items()}
        for year in years:
            chunks = chunk_ids_by_query_length(tag, ids, max_chars, year=year,
                                               py_tag=cfg.query_tag("publication_year"))
            for ci, chunk in enumerate(chunks):
                segkey = _seg_key("expand", f"{idtype}|{year}|{ci}|" + "|".join(sorted(chunk)))
                if ledger.is_done(segkey):
                    done_segments += 1
                    continue
                q = identifier_query(tag, chunk, year=year, py_tag=cfg.query_tag("publication_year"))
                try:
                    probe = wos.count_records(q)
                except CompletenessError as exc:
                    log.warning("expand probe failed (%s/%d): %s", idtype, year, exc)
                    continue
                if probe.records_found == 0:
                    ledger.mark_done(segkey, found=0)
                    continue
                got = 0
                rows_here: list[dict[str, Any]] = []
                for rec in wos.search_all(q, option_view=view):
                    flat = flatten_record(rec, cfg, "expand", q)
                    if flat:
                        rows_here.append(flat)
                        got += 1
                        matched_ids = _matched_ids_in_record(flat, chunk, id_by_formatted, tag)
                        for mid in matched_ids:
                            prov_rows.append({
                                "ut": flat["ut"],
                                "identifier_type": idtype,
                                "identifier": mid,
                                "expansion_query_sha1": hashlib.sha1(q.encode()).hexdigest()[:16],
                                "expansion_year": year,
                                "org_affiliated_in_this_record": flat["org_affiliated_any"],
                                "already_in_harvest": flat["ut"] in known_uts,
                            })
                if got != probe.records_found:
                    log.error("expand segment %s/%d chunk%d incomplete %d/%d — will retry next run",
                              idtype, year, ci, got, probe.records_found)
                else:
                    ledger.mark_done(segkey, found=probe.records_found, retrieved=got)
                if rows_here:
                    append_jsonl_segment(rec_path, rows_here)
    if prov_rows:
        append_jsonl_segment(prov_path, prov_rows)
    summary = {
        "identifiers_used": len(order),
        "segments_skipped_done": done_segments,
        "records_expanded_rows": sum(1 for _ in _iter_existing(rec_path)),
        "provenance_rows": len(prov_rows),
    }
    log.info("expand done: %s", summary)
    return summary


def _matched_ids_in_record(flat: dict[str, Any], chunk: list[str], id_by_formatted: dict[str, str], tag: str) -> list[str]:
    """Which queried identifiers actually appear on this record's authors?"""
    want = {c for c in chunk}
    hits: list[str] = []
    for a in flat["authors"]:
        val = None
        if tag.upper() == "RI":
            rid = a.get("researcher_id")
            if rid:
                val = format_researcherid(_last_first(a), rid)
        elif tag.upper() == "OI":
            val = a.get("orcid")
        elif tag.upper() == "DA":
            val = a.get("daisng_id")
        if val and val in want:
            orig = id_by_formatted.get(val, val)
            if orig not in hits:
                hits.append(orig)
    return hits
