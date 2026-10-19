"""muv_bibtex — Web of Science / InCites harvesting, bibliometrics and BibTeX export.

A resumable, five-phase CLI pipeline for a configurable organization
(default: Medical University of Varna, Bulgaria):

1. ``harvest``  — organization record discovery + author identifier harvesting
2. ``expand``   — expansion to all publications connected to those identifiers
3. ``dedupe``   — UT/DOI/title-based deduplication with a full merge log
4. ``stats``    — organization / year / researcher bibliometrics (InCites-aware)
5. ``export``   — BibTeX per year + master file + manifest + sidecar metrics

All HTTP access lives in :mod:`muv_bibtex.http` plus the two thin clients
(:mod:`muv_bibtex.wos_client`, :mod:`muv_bibtex.incites_client`).  Every endpoint
path, parameter name, header name and JSON field path is read from
``config/api_map.yaml`` — never hard-coded in the modules that use them.

See ``docs/API_ASSUMPTIONS.md`` for the VERIFIED-FROM-DOCS / NEEDS-CHECK list.
"""

__version__ = "0.1.0"

#: Version stamped into every intermediate artifact so readers can refuse
#: files produced by an incompatible pipeline version.
SCHEMA_VERSION = 3
