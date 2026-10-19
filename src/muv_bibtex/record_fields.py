"""Defensive extraction of fields from raw WoS JSON records.

Every JSON path is resolved through the caller-supplied ``api_map`` paths;
this module only implements the *traversal semantics*:

* ``path`` grammar: dot-separated steps, ``[]`` suffix means "ensure list",
  ``[type=X]`` filter step for identifier arrays (implemented by helpers);
* WoS collapses single-element arrays to scalars → :func:`as_list` normalises;
* attributes on dict payloads live under key ``"$"`` in some JSON renderings —
  checked first, then direct keys;
* missing values are returned as ``None``/``[]`` — never guessed.

The functions here are pure and unit-tested against recorded fixtures.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable


# --------------------------------------------------------------------------- #
# Traversal primitives
# --------------------------------------------------------------------------- #
def dig(obj: Any, path: str, default: Any = None) -> Any:
    """Follow a dotted path with optional ``[]`` list-normalisation markers.

    Examples:
        ``dig(rec, "static_data.summary.pub_info.pubyear")``
        ``dig(rec, "static_data.summary.names.name[]")`` -> always a list
    """
    cur = obj
    for step in path.split("."):
        want_list = step.endswith("[]")
        key = step[:-2] if want_list else step
        if isinstance(cur, dict):
            cur = cur.get(key, default if not want_list else [])
        elif isinstance(cur, list):
            # A single-element collapse may have produced a scalar already;
            # applying a named key to a list means "apply to each element".
            gathered = [el.get(key, None) for el in cur if isinstance(el, dict)]
            cur = [g for g in gathered if g not in (None, [], {})]
        else:
            return default
        if want_list:
            cur = as_list(cur)
    return cur


def as_list(value: Any) -> list[Any]:
    """Normalise WoS's scalar-vs-list ambiguity into a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def attr(dct: Any, name: str, default: Any = None) -> Any:
    """Read an XML-style attribute from a parsed-JSON dict (``$`` or direct)."""
    if not isinstance(dct, dict):
        return default
    if "$" in dct and isinstance(dct["$"], dict) and name in dct["$"]:
        return dct["$"][name]
    return dct.get(name, default)


def text_of(node: Any) -> str | None:
    """Extract textual content from ``{"content": ...}``, ``{"p": ...}`` or str."""
    if node is None:
        return None
    if isinstance(node, str):
        return node.strip() or None
    if isinstance(node, dict):
        for k in ("content", "p", "full_name", "display_name"):
            if k in node:
                v = node[k]
                if isinstance(v, list):
                    v = " ".join(str(x) for x in v)
                s = str(v).strip()
                if s:
                    return s
    return None


# --------------------------------------------------------------------------- #
# Identifiers / UT
# --------------------------------------------------------------------------- #
_UT_RE = re.compile(r"^WOS:\s*(\d{4}-\s*\d{4}(?:-\d{3})?(?:-\d+)?)$", re.I)


def parse_ut(uid: str) -> str:
    """Normalise a WoS UID ``WOS:0003...`` to canonical accession ``WOS:...``.

    Returns the trimmed original when it doesn't match the documented pattern
    (other products' UIDs such as ``MEDLINE:`` can appear in multi-database
    searches; they must survive unchanged for dedup provenance).
    """
    s = (uid or "").strip()
    return s


def ut_digits(ut: str) -> str:
    """Numeric/alnum part after the ``WOS:`` prefix (for BibTeX keys)."""
    m = _UT_RE.match(ut)
    if m:
        return re.sub(r"\s+", "", m.group(1))
    return re.sub(r"\W+", "", ut.split(":")[-1])


_ORCID_RE = re.compile(r"\b(\d{4}-){3}\d{3}[\dXx]\b")


def normalize_orcid(raw: Any) -> str | None:
    """Return canonical uppercase dashed ORCID or ``None``."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    m = _ORCID_RE.search(s.upper())
    return m.group(0).upper() if m else None


_RID_RE = re.compile(r"\b([A-Z]-\d{4}-\d{4}|[A-Z]{2}-?\d{4}-\d{4}|R[A-Z]-\d{4}-\d{4}|[A-Z]-\d{6}-\d{4}|P-[A-Z\d]+-\d{4})\b")


def normalize_researcherid(raw: Any) -> str | None:
    """Extract a ResearcherID token (e.g. ``A-4571-2019``, ``RIZ-2584-2019``).

    The WoS record exposes the bare RID in ``r_id.content``; we accept any
    letter-prefixed hyphenated pattern used by current RID formats.
    """
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if not s:
        return None
    m = _RID_RE.search(s)
    return m.group(1) if m else (s if len(s) >= 6 else None)


_DOI_RE = re.compile(r"^https?://(?:dx\.)?doi\.org/", re.I)


def normalize_doi(raw: Any) -> str | None:
    """Lowercase, strip URL prefix and surrounding whitespace."""
    if raw is None:
        return None
    s = str(raw).strip()
    s = _DOI_RE.sub("", s)
    s = s.removeprefix("doi:").removeprefix("DOI:")
    s = s.strip("/ ").lower()
    return s or None


# --------------------------------------------------------------------------- #
# Titles / years / identifiers arrays
# --------------------------------------------------------------------------- #
def title_of(rec: dict[str, Any], paths: dict[str, Any], which: str = "item") -> str | None:
    """``which`` in {item, container, book}; falls back sensibly."""
    titles = as_list(dig(rec, paths["titles"]))
    wanted = {"item": ("item",), "container": ("source", "container", "journal"),
              "book": ("book", "bookseries")}[which]
    fallback: str | None = None
    for t in titles:
        ttype = (attr(t, "type") or "").lower()
        txt = text_of(t)
        if not txt:
            continue
        if ttype in wanted:
            return txt
        if fallback is None:
            fallback = txt
    if which == "container" and fallback is None:
        return None
    return fallback if which == "item" else fallback


def pub_years(rec: dict[str, Any], paths: dict[str, Any]) -> tuple[int | None, int | None]:
    """Return (final_publication_year, early_access_year)."""
    def _yr(v: Any) -> int | None:
        try:
            n = int(str(v).strip())
            return n if 1900 <= n <= 2100 else None
        except (TypeError, ValueError):
            return None

    return _yr(dig(rec, paths["pubyear"])), _yr(dig(rec, paths["early_access_year"]))


def times_cited(rec: dict[str, Any], paths: dict[str, Any]) -> dict[str, Any]:
    """Citation block with explicit source labelling (never merged silently)."""
    cc = dig(rec, paths["tc_dedup_total"])
    out: dict[str, Any] = {
        "tc_cc_dedup": int(cc) if cc is not None and str(cc).isdigit() else None,
        "tc_function": {},
        "tc_silo": {},
    }
    for f in as_list(dig(rec, paths["tc_function"])):
        cls = (attr(f, "func_class") or attr(f, "name") or "").upper()
        val = attr(f, "count") or text_of(f)
        if cls and str(val).isdigit():
            out["tc_function"][cls] = int(val)
    for s in as_list(dig(rec, paths["tc_silo"])):
        cid = attr(s, "coll_id") or text_of(s)
        cnt = attr(s, "local_count")
        if cid and str(cnt).isdigit():
            out["tc_silo"][str(cid)] = int(cnt)
    return out


def identifiers_map(rec: dict[str, Any], paths: dict[str, Any]) -> dict[str, str]:
    """``{DOI: ..., ISSN: ..., EISSN: ..., ISBN: ..., PubMed: ...}`` lower-cased keys."""
    out: dict[str, str] = {}
    for ident in as_list(dig(rec, paths["identifiers"])):
        if not isinstance(ident, dict):
            continue
        itype = (attr(ident, "type") or text_of(ident.get("$", {})) or "").strip()
        val = text_of(ident)
        if itype and val:
            out.setdefault(itype.upper(), val)
    # defensive secondary paths (issn_candidates etc. contain filtered paths)
    for key, cand_paths in (("ISSN", "issn_candidates"), ("EISSN", "eissn_candidates"),
                            ("ISBN", "isbn_candidates")):
        if key in out:
            continue
        for cp in as_list(paths.get(cand_paths)):
            if not isinstance(cp, str):
                continue
            base, _, filt = cp.partition("[type=")
            if filt:
                want, _, rest = filt.partition("]")
                vals = [
                    text_of(i)
                    for i in as_list(dig(rec, base.rstrip(".").rstrip("[]")))
                    if isinstance(i, dict) and (attr(i, "type") or "").upper() == want.upper()
                ]
                got = next((v for v in vals if v), None)
            else:
                got = text_of(dig(rec, cp))
            if got:
                out[key] = got
                break
    return out


# --------------------------------------------------------------------------- #
# Authors + addresses + affiliation confirmation
# --------------------------------------------------------------------------- #
@dataclass
class AuthorHit:
    seq: int | None
    display_name: str | None
    wos_standard: str | None
    last_name: str | None
    first_name: str | None
    researcher_id: str | None
    orcid: str | None
    daisng_id: str | None
    addr_nos: list[str] = field(default_factory=list)
    reprint: bool = False
    claim_status: str | None = None
    data_item_ids: list[str] = field(default_factory=list)


@dataclass
class AddressEntry:
    addr_no: str | None
    full_address: str
    organizations: list[str] = field(default_factory=list)
    suborganizations: list[str] = field(default_factory=list)
    city: str | None = None
    country: str | None = None


def _match_norm(text: str) -> str:
    """Case/diacritic-insensitive token normalisation (mirrors config helper)."""
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = stripped.lower()
    tokens = [t for t in ("".join(c if c.isalnum() else " " for c in lowered)).split() if t]
    return " ".join(tokens)


def extract_addresses(rec: dict[str, Any], paths: dict[str, Any]) -> dict[str, AddressEntry]:
    """Map ``addr_no -> AddressEntry`` from ``addresses.address_name[]``."""
    nf = paths["address_fields"]
    out: dict[str, AddressEntry] = {}
    for a in as_list(dig(rec, paths["addresses"])):
        if not isinstance(a, dict):
            continue
        nos = _scalar_list(dig(a, nf["addr_no"]))
        orgs = [text_of(o) for o in as_list(dig(a, nf["organizations"]))]
        subs = [text_of(s) for s in as_list(dig(a, nf["suborganizations"]))]
        entry = AddressEntry(
            addr_no=nos[0] if nos else None,
            full_address=text_of(dig(a, nf["full_address"])) or "",
            organizations=[o for o in orgs if o],
            suborganizations=[s for s in subs if s],
            city=text_of(dig(a, nf["city"])),
            country=text_of(dig(a, nf["country"])),
        )
        for no in nos or [None]:
            if no is not None:
                out[no] = entry
    return out


def extract_authors(rec: dict[str, Any], paths: dict[str, Any]) -> list[AuthorHit]:
    """All authors with identifiers and address links (scalar/list safe)."""
    nf = paths["name_fields"]
    authors: list[AuthorHit] = []
    for n in as_list(dig(rec, paths["names"])):
        if not isinstance(n, dict):
            continue
        authors.append(
            AuthorHit(
                seq=_to_int(attr(n, nf["seq_no"]) or dig(n, nf["seq_no"])),
                display_name=text_of(dig(n, nf["display_name"])) or text_of(n),
                wos_standard=text_of(dig(n, nf["wos_standard"])),
                last_name=text_of(dig(n, nf["last_name"])),
                first_name=text_of(dig(n, nf["first_name"])),
                researcher_id=normalize_researcherid(_content_of(dig(n, nf["researcher_id"]))),
                orcid=normalize_orcid(_content_of(dig(n, nf["orcid"]))),
                daisng_id=(lambda v: str(v).strip() or None if v is not None else None)(
                    _content_of(dig(n, nf["daisng_id"]))
                ),
                addr_nos=_scalar_list(dig(n, nf["addr_no"])),
                reprint=str(attr(n, "reprint") or dig(n, nf["reprint"]) or "").lower() == "true",
                claim_status=str(dig(n, "claim.status") or "") or None,
                data_item_ids=_scalar_list(dig(n, nf["data_item_ids"])),
            )
        )
    return authors


def _content_of(node: Any) -> Any:
    """r_id/orcid_id/daisng_id are dicts with content|identifier attr; unwrap."""
    if node is None:
        return None
    if isinstance(node, dict):
        return node.get("content") or attr(node, "identifier") or attr(node, "$") or None
    return node


def _scalar_list(node: Any) -> list[str]:
    """WoS scalar/list ambiguity for value nodes.

    ``addr_no`` may arrive as ``"1"``, ``["1","2"]`` or
    ``{"content": ["1","2"]}`` / ``{"content": "1"}``; flatten to strings and
    drop empty values.  Non-dict mapping objects are unwrapped via their
    ``content`` key first (never str()-ed whole).
    """
    if node is None:
        return []
    if isinstance(node, dict):
        inner = node.get("content", node.get("$"))
        if inner is None:
            return []
        return _scalar_list(inner)
    if isinstance(node, list):
        out: list[str] = []
        for el in node:
            out.extend(_scalar_list(el))
        return out
    s = str(node).strip()
    return [s] if s else []


def _to_int(v: Any) -> int | None:
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None


def affiliation_confirmed(
    author: AuthorHit,
    addresses: dict[str, AddressEntry],
    org_match_terms: Iterable[str],
) -> bool:
    """True iff one of the author's linked addresses mentions an org variant.

    Matching is case- and diacritics-insensitive over tokenised strings and
    checks both the free-text ``full_address`` and every listed organization /
    suborganization name.  This is a FLAG ONLY — callers must not filter on it.
    """
    terms = [t for t in org_match_terms if t]
    candidate_texts: list[str] = []
    for no in author.addr_nos:
        entry = addresses.get(no)
        if entry is None:
            continue
        candidate_texts.append(entry.full_address)
        candidate_texts.extend(entry.organizations)
        candidate_texts.extend(entry.suborganizations)
    norm_candidates = [_match_norm(t) for t in candidate_texts if t]
    for term in terms:
        for cand in norm_candidates:
            if term in cand:
                return True
    return False


def best_affiliation_string(
    authors: list[AuthorHit], addresses: dict[str, AddressEntry], org_match_terms: Iterable[str]
) -> str | None:
    """Affiliation string for BibTeX: first address that confirms affiliation,
    else the corresponding-author address, else the first address."""
    terms = [t for t in org_match_terms if t]
    for a in authors:
        if affiliation_confirmed(a, addresses, terms):
            for no in a.addr_nos:
                e = addresses.get(no)
                if e and e.full_address:
                    return e.full_address
    for a in authors:
        if a.reprint:
            for no in a.addr_nos:
                e = addresses.get(no)
                if e and e.full_address:
                    return e.full_address
    first = next(iter(addresses.values()), None)
    return first.full_address if first and first.full_address else None
