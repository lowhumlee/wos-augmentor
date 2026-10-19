"""BibTeX generation from the deduplicated record set.

Design decisions (documented in README):

* **Entry types**: journal-article ``@article`` when source title + volume present,
  ``@review``/``@editorial``... mapped via DT heuristics → else ``@misc`` with
  ``note={WoS UT ...}`` so nothing is silently dropped; book chapters become
  ``@incollection`` when a book title exists.
* **Keys**: ``<first-author-surname><year><ut-digits-last-6>`` — globally unique
  because the WoS accession number is unique; collisions (same UT retrieved
  under two spellings) get an ``a/b`` suffix and are logged.
* **Author names**: WoS "Lastname, Firstnames" is already BibTeX-compatible;
  join with `` and ``; non-ASCII preserved (UTF-8 file).
* **JIF / quartile columns** (requirement: every entry carries them): emitted
  as custom fields ``journalimpactfactor`` and ``jcrquartile``.  Values come
  ONLY from InCites (``IMPACT_FACTOR``, best-percentile band); missing values
  stay empty — never estimated.  A header comment states this explicitly.
* **Encoding safety**: braces balanced, LaTeX escapes applied to a curated set
  of special characters only (medical-science text rarely contains TeX chars).
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
from .store import Store, read_rows, utc_now_iso

log = logging.getLogger("muv.bibtex")

MONTHS = {1: "jan", 2: "feb", 3: "mar", 4: "apr", 5: "may", 6: "jun",
          7: "jul", 8: "aug", 9: "sep", 10: "oct", 11: "nov", 12: "dec"}


# --------------------------------------------------------------------------- #
# Escaping
# --------------------------------------------------------------------------- #
_TEX_SPECIAL = {
    "&": r"\&",
    "%": r"\%",
    "#": r"\#",
}
_ACCENT_MAP = {
    "á": "{\\'a}", "à": "{\\`a}", "ä": '{\\"a}', "ö": '{\\"o}', "ü": '{\\"u}',
    "é": "{\\'e}", "è": "{\\`e}", "ë": '{\\"e}', "í": "{\\'i}", "ï": '{\\"i}',
    "ñ": "{\\~n}", "ç": "{\\c{c}}", "ş": "{\\c{s}}",
    "ă": "{\\u{a}}", "â": "{\\^a}", "î": "{\\^i}", "ș": "{\\s{\"s}}", "ț": "{\\s{t}}",
}
_UPPER = {k.upper(): v for k, v in _ACCENT_MAP.items()}


def latex_escape(text: str | None, *, mode: str) -> str:
    if text is None:
        return ""
    if mode == "raw":
        return text
    out = []
    for ch in text:
        if ch in _TEX_SPECIAL:
            out.append(_TEX_SPECIAL[ch])
        elif mode == "ascii" and (ch in _ACCENT_MAP or ch in _UPPER):
            out.append(_ACCENT_MAP.get(ch) or _UPPER[ch])
        else:
            out.append(ch)
    s = "".join(out)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def strip_braces_keep_balance(s: str) -> str:
    """Ensure balanced braces by dropping unmatched closers / appending openers."""
    depth, out = 0, []
    for ch in s:
        if ch == "{":
            depth += 1
            out.append(ch)
        elif ch == "}":
            if depth > 0:
                depth -= 1
                out.append(ch)
        else:
            out.append(ch)
    return "".join(out) + "}" * depth


# --------------------------------------------------------------------------- #
# Entry construction
# --------------------------------------------------------------------------- #
def surname_of(author: dict[str, Any]) -> str:
    name = author.get("wos_standard") or author.get("display_name") or ""
    if "," in name:
        return name.split(",")[0].strip()
    parts = [p for p in name.split() if p]
    return parts[-1] if parts else "Anonymous"


def bibtex_author_field(authors: list[dict[str, Any]]) -> str:
    names = []
    for a in authors:
        disp = a.get("display_name") or a.get("wos_standard") or ""
        if not disp:
            continue
        if "," in disp:
            last, first = disp.split(",", 1)
            names.append(f"{last.strip()}, {first.strip()}")
        else:
            parts = disp.rsplit(" ", 1)
            names.append(f"{parts[-1]}, {' '.join(parts[:-1])}" if len(parts) > 1 else disp)
    return " and ".join(names)


def month_from_date(sortdate: str | None, coverdate: str | None) -> str | None:
    for d in (sortdate, coverdate):
        if d:
            m = re.match(r"(\d{4})-(\d{2})", d)
            if m:
                mo = int(m.group(2))
                if 1 <= mo <= 12:
                    return MONTHS[mo]
    return None


DT_ENTRYTYPE = {
    "article": "article", "review": "article", "letter": "article",
    "editorial material": "article", "proceedings paper": "inproceedings",
    "book chapter": "incollection", "book": "book", "data paper": "article",
    "early access": "article", "abstract": "misc", "meeting abstract": "inproceedings",
}


def choose_entrytype(row: dict[str, Any], cfg: AppConfig) -> tuple[str, bool]:
    """Return (entrytype, needs_note). Falls back to @misc for odd types."""
    doctypes = [d.lower() for d in row.get("doctypes") or []]
    primary = doctypes[0] if doctypes else (row.get("pubtype") or "").lower()
    et = DT_ENTRYTYPE.get(primary, None)
    if et == "article" and not (row.get("journal") and row.get("volume")):
        return ("misc", True)
    if et == "inproceedings" and not row.get("book_title"):
        et = "conference" if row.get("series_title") else "inproceedings"
    if et is None:
        return ("misc", True)
    return (et, False)


def build_fields(row: dict[str, Any], metrics: dict[str, Any] | None, cfg: AppConfig) -> dict[str, str]:
    esc = lambda v: latex_escape(v, mode=cfg.output.encoding_mode)  # noqa: E731
    f: dict[str, str] = {}
    authors = row.get("authors") or []
    if authors:
        f["author"] = esc(bibtex_author_field(authors))
    if row.get("title"):
        f["title"] = "{" + esc(row["title"]) + "}"  # protect sentence case
    year = row.get("publication_year_final") or row.get("early_access_year")
    if year:
        f["year"] = str(year)
    if row.get("journal"):
        f["journal"] = esc(row["journal"])
    if row.get("book_title"):
        f["booktitle"] = esc(row["book_title"])
    if row.get("volume"):
        f["volume"] = esc(row["volume"])
    if row.get("issue"):
        f["number"] = esc(row["issue"])
    pages = None
    if row.get("page_begin"):
        pages = str(row["page_begin"])
        if row.get("page_end") and row["page_end"] != row["page_begin"]:
            pages += f"--{row['page_end']}"
    if pages:
        f["pages"] = pages
    elif row.get("page_count"):
        f["numpages"] = esc(str(row["page_count"]))
    if row.get("doi"):
        f["doi"] = row["doi"]  # DOIs are ASCII-safe by construction
    if row.get("issn"):
        f["issn"] = row["issn"]
    if row.get("publisher"):
        f["publisher"] = esc(row["publisher"])
    if row.get("bibtex_affiliation"):
        f["affiliation"] = esc(row["bibtex_affiliation"])
    m = month_from_date(row.get("sortdate"), row.get("coverdate"))
    if m:
        f["month"] = m
    if cfg.output.include_abstract and row.get("abstract"):
        f["abstract"] = esc(row["abstract"])
    kw = (row.get("keywords_author") or [])[: cfg.output.max_keywords]
    if kw:
        f["keywords"] = esc("; ".join(kw))
    ut = row.get("ut") or ""
    f["url"] = f"https://www.webofscience.com/wos/woscc/full-record/{ut}" if ut else ""
    if row.get("times_cited_cc_dedup") is not None:
        f["citedby-wos"] = str(row["times_cited_cc_dedup"])
    # --- required custom metric fields (empty when unavailable) ------------ #
    inc = metrics or {}
    jif = inc.get("IMPACT_FACTOR")
    f["journalimpactfactor"] = str(jif) if jif not in (None, "", "N/A") else ""
    band = inc.get("_derived_band")
    f["jcrquartile"] = str(band) if band else ""
    if inc.get("TIMES_CITED_INBITES") is not None:
        f["timescited-incites"] = str(inc["TIMES_CITED_INBITES"])
    if not f["url"]:
        del f["url"]
    return {k: v for k, v in f.items() if v != ""}


def make_key(row: dict[str, Any], used: set[str]) -> str:
    authors = row.get("authors") or [{}]
    sur = surname_of(authors[0])
    sur = unicodedata.normalize("NFKD", sur)
    sur = "".join(c for c in sur if not unicodedata.combining(c))
    sur = re.sub(r"[^A-Za-z]", "", sur) or "Anon"
    year = row.get("publication_year_final") or row.get("early_access_year") or "nd"
    tail = (row.get("ut_digits") or "")[-6:] or "000000"
    base = f"{sur}{year}{tail}"
    key, suffix = base, 0
    while key in used:
        suffix += 1
        key = base + chr(ord("a") + suffix - 1)
        if suffix > 25:
            key = f"{base}-{suffix}"
    used.add(key)
    return key


def render_entry(entrytype: str, key: str, fields: dict[str, str], note: str | None) -> str:
    lines = [f"@{entrytype}{{{key},"]
    order = ["author", "title", "journal", "booktitle", "year", "month", "volume",
             "number", "pages", "numpages", "note", "issn", "publisher", "affiliation",
             "doi", "journalimpactfactor", "jcrquartile", "citedby-wos",
             "timescited-incites", "url", "abstract", "keywords"]
    names = order + [k for k in fields if k not in order]
    body = dict(fields)
    if note:
        body["note"] = note
    for name in names:
        if name in body:
            val = strip_braces_keep_balance(str(body[name]))
            lines.append(f"  {name:<20}= {{{val}}},")
    lines[-1] = lines[-1].rstrip(",")
    lines.append("}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
@dataclass
class BibtexResult:
    entries: int = 0
    misc_fallbacks: int = 0
    keys_renamed: int = 0
    missing_jif: int = 0
    missing_quartile: int = 0
    warnings: list[str] = field(default_factory=list)


def run_bibtex(cfg: AppConfig, store: Store, *, metrics_by_ut: dict[str, dict[str, Any]] | None = None) -> BibtexResult:
    res = BibtexResult()
    final_path = store.file("dedup", "final.jsonl.gz")
    if not final_path.exists():
        raise FileNotFoundError(f"{final_path} missing — run 'dedup' first")
    rows = read_rows(final_path)
    if not cfg.toggles.include_non_org_connected_records:
        before = len(rows)
        rows = [r for r in rows if r.get("org_affiliated_any")]
        log.info("include_non_org_connected_records=false: filtered %d → %d records",
                 before, len(rows))
    rows.sort(key=lambda r: (r.get("publication_year_final") or 0, surname_of((r.get("authors") or [{}])[0])))

    used: set[str] = set()
    chunks: list[str] = []
    csv_rows: list[dict[str, Any]] = []
    metrics_by_ut = metrics_by_ut or {}
    for row in rows:
        ut_key_norm = (row.get("ut_digits") or "").upper()
        metrics = metrics_by_ut.get(ut_key_norm)
        entrytype, needs_note = choose_entrytype(row, cfg)
        note = None
        if needs_note:
            res.misc_fallbacks += 1
            dt = ", ".join(row.get("doctypes") or [row.get("pubtype") or "unknown"])
            note = f"Web of Science {row.get('ut', '')}; document type: {dt}"
        fields = build_fields(row, metrics, cfg)
        key = make_key(row, used)
        if key != make_key(row, set()):
            res.keys_renamed += 1
        chunks.append(render_entry(entrytype, key, fields, note))
        jif, band = fields.get("journalimpactfactor", ""), fields.get("jcrquartile", "")
        if not jif:
            res.missing_jif += 1
        if not band:
            res.missing_quartile += 1
        csv_rows.append({
            "bibkey": key, "ut": row.get("ut"), "doi": row.get("doi") or "",
            "year": row.get("publication_year_final") or "", "journal": row.get("journal") or "",
            "jif": jif, "quartile_or_band": band,
            "tc_wos": row.get("times_cited_cc_dedup") or "",
            "org_affiliated": row.get("org_affiliated_any"),
        })

    header = [
        "% ============================================================",
        f"% Generated by muv-bibtex on {utc_now_iso()}",
        f"% Organization : {cfg.organization.display_name} ({cfg.organization.country})",
        f"% Time span    : {cfg.time_span.start_year}--{cfg.time_span.end_year}",
        f"% Records      : {len(rows)} (deduplicated)",
        "% journalimpactfactor / jcrquartile come from InCites Document Level",
        "% Metrics (jcrquartile is DERIVED from InCites best-percentile bands,",
        "%   not official JCR labels). Empty value = not available in your",
        "%   InCites subscription at retrieval time — never estimated.",
        f"% Citation counts are snapshots taken at retrieval (see citedby-wos).",
        "% ============================================================",
        "",
    ]
    out = Path(cfg.output.filename)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(header) + "\n\n".join(chunks) + "\n", encoding="utf-8")
    log.info("wrote %d entries to %s", len(chunks), out)

    sidecar = out.with_suffix(".metrics_audit.csv")
    with open(sidecar, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(csv_rows[0].keys()) if csv_rows else
                           ["bibkey", "ut", "doi", "year", "journal", "jif",
                            "quartile_or_band", "tc_wos", "org_affiliated"])
        w.writeheader()
        w.writerows(csv_rows)

    res.entries = len(chunks)
    res.warnings.append(f"sidecar audit CSV: {sidecar}")
    return res
