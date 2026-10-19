"""Pure WoS ``usrQuery`` construction — no network access, fully unit-testable.

All field tags (OG=, PY=, DT=, RI=, OI=, DA=, AD=, UT=) come from
``api_map.yaml: wos.query_tags`` because they are *not* enumerated in the
OpenAPI spec (NEEDS-CHECK; verified at runtime by ``discover --verify-tags``).

Documented constraints honoured here (VERIFIED-FROM-DOCS):
* ``count`` must be 0..100;
* paging offset ``firstRecord`` is bounded by 100000 → callers must split any
  query whose ``RecordsFound`` approaches that cap (per-year, then per-month or
  per-document-type);
* GET URL length is not documented → conservative ``max_query_chars`` guard
  with POST fallback handled by the client, not here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


def quote_term(term: str) -> str:
    """Quote a search term for WoS. Internal double quotes are escaped."""
    return '"' + term.replace('"', '\\"') + '"'


def join_or(terms: list[str], tag: str) -> str:
    """``OG=(("A") OR ("B"))`` style group for one field tag."""
    if not terms:
        raise ValueError(f"join_or: empty term list for tag {tag!r}")
    inner = " OR ".join(f"({quote_term(t)})" for t in terms)
    return f"{tag}=({inner})"


def combine_and(*clauses: str) -> str:
    parts = [c for c in clauses if c]
    return " AND ".join(parts)


def org_query(
    variants: list[str],
    og_tag: str,
    *,
    year: int | None = None,
    py_tag: str = "PY",
    month_span: tuple[int, int] | None = None,
    extra_clauses: list[str] | None = None,
) -> str:
    """Organization-enhanced query for one variant list, optionally year/month-split.

    Month splitting uses the publication-year tag with an explicit interval via
    ``DA=`` only when a span is supplied; otherwise plain ``PY=year``.
    """
    clause = join_or(variants, og_tag)
    if year is not None:
        if month_span is not None:
            start_m, end_m = month_span
            clause = combine_and(
                clause,
                f"{py_tag}={year}",
                f"DA=(({year}-{start_m:02d}-01) TO ({year}-{end_m:02d}-31))",
            )
        else:
            clause = combine_and(clause, f"{py_tag}={year}")
    if extra_clauses:
        clause = combine_and(clause, *extra_clauses)
    return clause


def dt_clause(dt_tag: str, doctypes: list[str]) -> str:
    """``DT=(Article OR Review ...)`` group used for secondary splitting."""
    inner = " OR ".join(doctypes)
    return f"{dt_tag}=({inner})"


def identifier_query(
    tag: str,
    ids: list[str],
    *,
    year: int | None = None,
    py_tag: str = "PY",
) -> str:
    """Identifier-search query: ``RI=((A) OR (B)) [AND PY=yyyy]``.

    WoS ResearcherID search format is ``RI=<Lastname, Firstname>(RID-number)``;
    ORCID search format is ``OI=<orcid>``.  Callers pass already-formatted
    strings (see :func:`format_researcherid` / :func:`format_orcid`).
    """
    clause = join_or(ids, tag)
    if year is not None:
        clause = combine_and(clause, f"{py_tag}={year}")
    return clause


def format_researcherid(last_first: str, rid: str) -> str:
    """Compose the WoS ``RI=`` value: ``Doe, John ``(A-4571-2019)``.

    If the display name is unknown, the RID alone still searches correctly on
    the current platform; we keep the two-part form as documented.
    """
    rid_clean = rid.strip()
    if last_first:
        return f"{last_first.strip()}({rid_clean})"
    return rid_clean


def format_orcid(orcid: str) -> str:
    """Normalise an ORCID to bare ``0000-0000-0000-0000`` form for ``OI=``."""
    digits = re.sub(r"[^0-9Xx]", "", orcid.upper().replace("ORCID:", ""))
    if len(digits) == 17 and digits.endswith("X"):
        digits = digits[:16] + "X"
    if len(digits) != 16:
        raise ValueError(f"invalid ORCID: {orcid!r}")
    return "-".join(digits[i : i + 4] for i in range(0, 16, 4)).upper()


def chunk_ids_by_query_length(
    tag: str,
    ids: list[str],
    max_chars: int,
    *,
    year: int | None = None,
    py_tag: str = "PY",
) -> list[list[str]]:
    """Split an id list into chunks whose rendered query stays under *max_chars*.

    Each chunk renders as ``identifier_query(tag, chunk, year=...)``; the year
    suffix is included in the length budget so every emitted query is safe.
    """
    if max_chars < 80:
        raise ValueError("max_chars too small to build any query")
    budget_overhead = len(f'{tag}=()') + (len(f" AND {py_tag}=") + 4 if year is not None else 0) + 8
    chunks: list[list[str]] = []
    current: list[str] = []
    current_len = budget_overhead
    for ident in ids:
        piece = len(ident) + 6  # ("…") wrapper ≈ 6 chars
        if current and current_len + piece > max_chars:
            chunks.append(current)
            current, current_len = [], budget_overhead
        current.append(ident)
        current_len += piece
    if current:
        chunks.append(current)
    return chunks


# --------------------------------------------------------------------------- #
# Paging plan
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PagePlan:
    """Offsets for one query given its RecordsFound total."""

    first_record_param: str
    count: int
    offsets: list[int]
    records_found: int

    def expected_pages(self) -> int:
        return len(self.offsets)


def build_page_plan(
    records_found: int, page_size: int, first_record_param: str = "firstRecord"
) -> PagePlan:
    """Offsets 1, 1+page, 1+2page … covering *records_found* (WoS is 1-based)."""
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    if records_found < 0:
        raise ValueError("records_found must be >= 0")
    offsets = list(range(1, records_found + 1, page_size)) if records_found else []
    return PagePlan(first_record_param, page_size, offsets, records_found)


MAX_FIRST_RECORD = 100_000  # VERIFIED-FROM-DOCS: firstRecord bound in the WoS spec


def exceeds_offset_cap(records_found: int) -> bool:
    """True when a single query cannot page past the documented offset cap."""
    return records_found > MAX_FIRST_RECORD


# --------------------------------------------------------------------------- #
# Static validation of user-supplied query syntax (best-effort, offline)
# --------------------------------------------------------------------------- #
_TAG_RE = re.compile(r"\b([A-Z]{2})\s*=\s*\(")
_KNOWN_TAGS = {"TI", "AB", "AU", "GP", "OG", "AD", "PY", "DT", "SO", "UT", "RI", "OI", "DA", "LA", "CA"}


def validate_query_syntax(query: str) -> list[str]:
    """Return a list of human-readable warnings (never raises).

    Checks balanced parentheses/quotes and flags unknown two-letter field tags
    so operators see typos before spending quota.
    """
    warnings: list[str] = []
    if query.count("(") != query.count(")"):
        warnings.append("unbalanced parentheses")
    if query.count('"') % 2:
        warnings.append("unbalanced quotation marks")
    for m in _TAG_RE.finditer(query):
        tag = m.group(1)
        if tag not in _KNOWN_TAGS:
            warnings.append(f"unknown field tag {tag}= (check api_map query_tags)")
    if re.search(r"\bAND\s+AND\b|\bOR\s+OR\b", query):
        warnings.append("duplicated boolean operator")
    return warnings
