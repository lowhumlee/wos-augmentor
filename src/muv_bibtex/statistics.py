"""Phase 4: bibliometric statistics from the deduplicated corpus.

Two scopes are always reported side by side (requirement: include expansion
records, but keep them visible):

* ``org_affiliated`` — records where some author address confirms the
  organization (the classic "institutional output");
* ``all_included``   — everything retrieved incl. identifier-expanded records
  whose affiliation field does not mention the organization.

Indicators implemented (pure functions in :mod:`statistics`-style helpers,
unit-tested): annual counts, total/mean/median citations, highly-cited count
(top-1% by year via percentile threshold), uncited share, collaboration
strength (fraction with external co-authors), international collaboration,
open-access share, document-type & language distributions, journal production
(TOP journals), keyword frequency, h-index and its variants (m, g, tc/max).

InCites-derived indicators (category-normalised CNCI etc.) are attached during
the ``stats`` command when the InCites key is available; without it they stay
empty — **never estimated**.
"""
from __future__ import annotations

import csv
import json
import logging
import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .config import AppConfig
from .store import Store, read_rows, utc_now_iso

log = logging.getLogger("muv.stats")


# --------------------------------------------------------------------------- #
# Pure indicator functions
# --------------------------------------------------------------------------- #
def h_index(citations: Iterable[int]) -> int:
    cs = sorted((c for c in citations if c is not None), reverse=True)
    h = 0
    for i, c in enumerate(cs, start=1):
        if c >= i:
            h = i
        else:
            break
    return h


def m_quotient(h: int, years_active: int) -> float | None:
    return round(h / years_active, 3) if years_active > 0 else None


def g_index(citations: Iterable[int]) -> int:
    cs = sorted((c for c in citations if c is not None), reverse=True)
    total, g = 0, 0
    for i, c in enumerate(cs, start=1):
        total += c
        if total >= i * i:
            g = i
    return g


def median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return float(s[mid]) if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def percentile_threshold(values: list[float], pct: float) -> float | None:
    """Linear-interpolation percentile (pct in 0..100)."""
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * pct / 100.0
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] + (s[c] - s[f]) * (k - f)


def top_percent_count(citations: list[int], pct: float) -> tuple[int, float | None]:
    thr = percentile_threshold([float(c) for c in citations], 100 - pct)
    if thr is None:
        return (0, None)
    return (sum(1 for c in citations if c >= thr), thr)


def collaboration_metrics(records: list[dict[str, Any]], org_terms: list[str]) -> dict[str, Any]:
    """Fractions of papers with any external co-author / foreign co-author."""
    n_collab = n_intl = n_single = 0
    for r in records:
        addrs = r.get("addresses") or []
        orgs = {o.lower() for a in addrs for o in (a.get("organizations") or [])}
        countries = {(a.get("country") or "").lower() for a in addrs if a.get("country")}
        texts = " ".join(list(orgs) + [a.get("full_address", "").lower() for a in addrs])
        internal = any(t in texts for t in org_terms)
        has_external_orgs = len([o for o in orgs if o]) > (1 if internal else 0) or not internal
        n_authors = len(r.get("authors") or [])
        if n_authors <= 1 and len(addrs) <= 1:
            n_single += 1
        if n_authors > 1 and has_external_orgs:
            n_collab += 1
        home_country = {(a.get("country") or "").lower() for a in addrs
                        if any(t in (a.get("full_address", "").lower()) for t in org_terms)}
        foreign = countries - home_country - {""}
        if foreign:
            n_intl += 1
    n = len(records) or 1
    return {
        "single_author": n_single,
        "collaboration_with_external": n_collab,
        "international_collaboration": n_intl,
        "collaboration_rate": round(n_collab / n, 4),
        "international_rate": round(n_intl / n, 4),
    }


def compute_stats(cfg: AppConfig, rows: list[dict[str, Any]]) -> dict[str, Any]:
    cites = [int(r["times_cited_cc_dedup"] or 0) for r in rows]
    years = Counter()
    for r in rows:
        y = r.get("publication_year_final") or r.get("early_access_year")
        if y:
            years[int(y)] += 1
    hc, thr = top_percent_count(cites, cfg.statistics.highly_cited_top_percent)
    uncited = sum(1 for c in cites if c == 0)
    hs = h_index(cites)
    active_years = (max(years) - min(years) + 1) if years else 0
    dtypes = Counter()
    for r in rows:
        for d in (r.get("doctypes") or ([r.get("pubtype")] if r.get("pubtype") else [])):
            if d:
                dtypes[d] += 1
    langs = Counter()
    for r in rows:
        for l in (r.get("languages") or []):
            langs[l] += 1
    journals = Counter((r.get("journal") or "").strip() for r in rows if r.get("journal"))
    kws = Counter()
    for r in rows:
        for k in (r.get("keywords_author") or []):
            kws[k.strip().lower()] += 1
    oa = sum(1 for r in rows if r.get("open_access_gold"))
    total_tc = sum(cites)
    out = {
        "total_publications": len(rows),
        "by_year": dict(sorted(years.items())),
        "times_cited": {
            "total": total_tc,
            "mean": round(total_tc / len(rows), 3) if rows else None,
            "median": median([float(c) for c in cites]),
            "max": max(cites) if cites else None,
        },
        "highly_cited": {
            "definition": f"top {cfg.statistics.highly_cited_top_percent}% of all retrieved "
                          f"papers by times-cited (corpus-relative threshold)",
            "threshold_value": thr,
            "count": hc,
            "share": round(hc / (len(rows) or 1), 4),
        },
        "uncited": {"count": uncited, "share": round(uncited / (len(rows) or 1), 4)},
        "h_index": hs,
        "g_index": g_index(cites),
        "m_quotient": m_quotient(hs, active_years),
        "tc_per_max_hybrid": round(total_tc / hs, 2) if hs else None,
        "document_types": dict(dtypes.most_common()),
        "languages": dict(langs.most_common()),
        "top_journals": dict(journals.most_common(cfg.statistics.top_journal_list_size)),
        "author_keywords_top": dict(kws.most_common(cfg.statistics.keyword_frequency_top_n)),
        "open_access": {"gold_count": oa, "share": round(oa / (len(rows) or 1), 4)},
        "collaboration": collaboration_metrics(rows, cfg.org_match_terms()),
    }
    return out


# --------------------------------------------------------------------------- #
def run_stats(cfg: AppConfig, store: Store, *, incites_by_ut: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    final_path = store.file("dedup", "final.jsonl.gz")
    if not final_path.exists():
        raise FileNotFoundError(f"{final_path} missing — run 'dedup' first")
    rows = read_rows(final_path)
    org_rows = [r for r in rows if r.get("org_affiliated_any")]
    report: dict[str, Any] = {
        "generated_at_utc": utc_now_iso(),
        "snapshot_note": "citation counts are point-in-time snapshots at retrieval",
        "organization": cfg.organization.display_name,
        "time_span": [cfg.time_span.start_year, cfg.time_span.end_year],
        "scopes": {
            "all_included": compute_stats(cfg, rows),
            "org_affiliated": compute_stats(cfg, org_rows),
        },
        "incites": {"available": bool(incites_by_ut)},
    }

    # ---- InCites-enriched aggregates (only when we actually have them) ----- #
    if incites_by_ut:
        matched = 0
        jif_vals: list[float] = []
        band_counter: Counter = Counter()
        avg_cnci_sum, avg_cnci_n = 0.0, 0
        for r in rows:
            row = incites_by_ut.get((r.get("ut_digits") or "").upper())
            if not row:
                continue
            matched += 1
            try:
                jif_vals.append(float(row["IMPACT_FACTOR"]))
            except (KeyError, TypeError, ValueError):
                pass
            if row.get("_derived_band"):
                band_counter[row["_derived_band"]] += 1
            try:
                avg_cnci_sum += float(row["AVG_CNCI"])
                avg_cnci_n += 1
            except (KeyError, TypeError, ValueError):
                pass
        report["incites"].update({
            "records_matched": matched,
            "records_unmatched": len(rows) - matched,
            "impact_factor_mean_of_record_jifs": round(sum(jif_vals) / len(jif_vals), 3) if jif_vals else None,
            "percentile_bands_distribution": dict(band_counter),
            "avg_cnci_mean": round(avg_cnci_sum / avg_cnci_n, 3) if avg_cnci_n else None,
            "note": "jcrquartile columns elsewhere are DERIVED bands, not official JCR labels",
        })
    else:
        log.warning("No InCites metrics supplied — indicator columns left empty "
                    "(never estimated). Set INCITES_API_KEY and rerun 'stats'.")

    # ---- outputs ---------------------------------------------------------- #
    stats_dir = store.phase_dir("stats")
    (stats_dir / "summary.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    _write_csv(stats_dir / "publications_by_year.csv", report["scopes"]["all_included"]["by_year"],
               report["scopes"]["org_affiliated"]["by_year"])
    _write_records_csv(stats_dir / "records_flat.csv", rows)
    _write_markdown(stats_dir / "report.md", report, cfg)
    log.info("stats written to %s", stats_dir)
    return report


def _write_csv(path: Path, all_by_year: dict, org_by_year: dict) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["year", "all_included", "org_affiliated"])
        for y in sorted(set(all_by_year) | set(org_by_year)):
            w.writerow([y, all_by_year.get(y, 0), org_by_year.get(y, 0)])


def _write_records_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    cols = ["ut", "doi", "publication_year_final", "title", "journal",
            "times_cited_cc_dedup", "org_affiliated_any", "issn", "eissn", "source"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols + ["n_authors", "first_author", "researcher_ids", "orcids"])
        for r in rows:
            authors = r.get("authors") or []
            w.writerow([r.get(c, "") for c in cols] + [
                len(authors),
                (authors[0].get("display_name") if authors else ""),
                ";".join(a.get("researcher_id") or "" for a in authors if a.get("researcher_id")),
                ";".join(a.get("orcid") or "" for a in authors if a.get("orcid")),
            ])


def _write_markdown(path: Path, rep: dict[str, Any], cfg: AppConfig) -> None:
    def scope_block(name: str) -> list[str]:
        s = rep["scopes"][name]
        lines = [f"## Scope: {name}", ""]
        lines.append(f"- Publications: **{s['total_publications']}**")
        lines.append(f"- Total citations (WoS Core Collection, deduplicated): "
                     f"**{s['times_cited']['total']}**  "
                     f"(mean {s['times_cited']['mean']}, median {s['times_cited']['median']})")
        lines.append(f"- h-index: **{s['h_index']}**, g-index: {s['g_index']}, "
                     f"m-quotient: {s['m_quotient']}")
        hc = s["highly_cited"]
        lines.append(f"- Highly cited ({hc['definition']}): **{hc['count']}** "
                     f"(threshold ≥ {hc['threshold_value']})")
        lines.append(f"- Uncited: {s['uncited']['count']} ({s['uncited']['share']*100:.1f}%)")
        col = s["collaboration"]
        lines.append(f"- Collaboration rate (external co-authors): {col['collaboration_rate']*100:.1f}%; "
                     f"international: {col['international_rate']*100:.1f}%")
        lines.append(f"- Gold open access: {s['open_access']['gold_count']} "
                     f"({s['open_access']['share']*100:.1f}%)")
        top_years = sorted(s["by_year"].items(), key=lambda kv: -kv[1])[:5]
        lines.append("- Peak years: " + ", ".join(f"{y} ({n})" for y, n in top_years))
        lines.append("")
        lines.append("### Top journals")
        for j, n in list(s["top_journals"].items())[:10]:
            lines.append(f"- {j}: {n}")
        lines.append("")
        lines.append("### Document types")
        for d, n in list(s["document_types"].items())[:10]:
            lines.append(f"- {d}: {n}")
        lines.append("")
        return lines

    md = [f"# Bibliometric report — {rep['organization']}", "",
          f"Generated: {rep['generated_at_utc']}  ", f"Time span: {rep['time_span'][0]}–{rep['time_span'][1]}",
          "", "> " + rep["snapshot_note"], "",
          "> Scopes: `all_included` contains every deduplicated record including",
          "> identifier-expanded non-affiliated co-authorships; `org_affiliated` is",
          "> the subset whose author addresses confirm the organization.", ""]
    md += scope_block("all_included")
    md += scope_block("org_affiliated")
    inc = rep.get("incites", {})
    md.append("## InCites enrichment")
    if inc.get("available"):
        md.append(f"- Records matched to InCites metrics: {inc.get('records_matched')} "
                  f"(unmatched: {inc.get('records_unmatched')})")
        md.append(f"- Mean of per-record journal impact factors: {inc.get('impact_factor_mean_of_record_jifs')}")
        md.append(f"- Category-normalised citation impact (AVG_CNCI mean): {inc.get('avg_cnci_mean')}")
        md.append(f"- Percentile band distribution (derived, not official JCR quartiles): "
                  f"{inc.get('percentile_bands_distribution')}")
        md.append(f"- Note: {inc.get('note')}")
    else:
        md.append("- Not available: INCITES_API_KEY unset or metrics retrieval failed.")
        md.append("- Impact-factor / quartile fields were left EMPTY (never estimated).")
    path.write_text("\n".join(md) + "\n", encoding="utf-8")
