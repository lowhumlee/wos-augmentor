"""``discover`` command: API ground-truth verification before spending quota.

Actions (each optional via flags, all reported into ``docs/discovery_report.md``
plus ``.work/discovery/``):

1. Download the InCites and WoS OpenAPI/Swagger definitions from the URLs in
   ``api_map.yaml: incites.discover_specs`` (and the developer portal), save
   them, and list every endpoint with its parameters → ``endpoints.json``.
2. Runtime probes (no record retrieval):
   * WoS: one ``count=0`` search per configured query tag (OG/PY/DT/RI/OI/DA/AD)
     to verify each tag is accepted → marks NEEDS-CHECK items VERIFIED or FAILED;
   * InCites: ``InCitesLastUpdated`` reachability + one institution RecordCount
     probe for a recent year (confirms the key's institution profile works).
3. Print quota header snapshot from the probes.

Nothing here guesses endpoints: step 1 lists what the APIs *actually expose*,
step 2 verifies the handful of assumptions flagged NC in docs/API_ASSUMPTIONS.md.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .config import AppConfig
from .errors import ApiAuthenticationError, ApiResponseError
from .http import RequestCounter, ResponseCache
from .incites_client import IncitesClient
from .query_builder import org_query
from .store import Store, utc_now_iso
from .wos_client import WosClient

log = logging.getLogger("muv.discover")


def _download_spec(client: WosClient | IncitesClient, url: str, dest: Path) -> dict[str, Any] | None:
    """Fetch an absolute OpenAPI/Swagger URL (portal specs are unauthenticated).

    Uses a plain ``requests`` GET here — deliberately not the API client, which
    would prepend the product base_url and inject the API key header.  The key
    is never attached to third-party hosts.
    """
    import requests

    try:
        resp = requests.get(url, headers={"Accept": "application/json"},
                            timeout=client.http.timeout)
    except requests.RequestException as exc:
        log.warning("spec download failed for %s: %s", url, exc)
        return None
    if resp.status_code != 200:
        log.warning("spec download HTTP %d for %s", resp.status_code, url)
        return None
    try:
        data = resp.json()
    except ValueError:
        text = resp.text
        if "<html" in text[:200].lower():
            log.warning("spec URL %s returned an HTML page (portal login required?) — skipped", url)
            return None
        raise
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    log.info("saved OpenAPI definition: %s (%d bytes)", dest, len(resp.content))
    return data


def _list_endpoints(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalise OpenAPI paths into [{path, method, params:[{name, in, required}]}]."""
    out: list[dict[str, Any]] = []
    for ppath, ops in (spec.get("paths") or {}).items():
        if not isinstance(ops, dict):
            continue
        for method, op in ops.items():
            if method.lower() not in ("get", "post", "put", "delete") or not isinstance(op, dict):
                continue
            params = []
            for prm in op.get("parameters", []) or []:
                params.append({
                    "name": prm.get("name"),
                    "in": prm.get("in"),
                    "required": bool(prm.get("required", False)),
                    "type": ((prm.get("schema") or {}).get("type")),
                })
            out.append({"path": ppath, "method": method.upper(), "params": params,
                        "summary": op.get("summary", "")})
    return sorted(out, key=lambda e: (e["path"], e["method"]))


def run_discover(
    cfg: AppConfig,
    *,
    verify_tags: bool = True,
    download_specs: bool = True,
    report_dir: Path | None = None,
) -> dict[str, Any]:
    """Execute discovery; returns the report dict (also written to disk)."""
    store = Store(cfg.paths.work_dir)
    out_dir = Path(report_dir) if report_dir else store.phase_dir("discovery")
    counter = RequestCounter()
    cache = ResponseCache(store.cache_dir(), force_refresh=False)
    report: dict[str, Any] = {"generated_at_utc": utc_now_iso(), "checks": []}

    wos = WosClient(cfg, cache, counter)
    inc = IncitesClient(cfg, cache, counter) if cfg.incites_key else None

    # ---- 1. specs ---------------------------------------------------------- #
    if download_specs:
        for name, urls in (
            ("incites", [str(u) for u in (cfg.incites_api.get("discover_specs") or [])]),
            ("wos", ["https://developer.clarivate.com/apis/wos/swagger",
                     "https://api.clarivate.com/api/wos/swagger.json"]),
        ):
            saved = None
            for url in urls:
                spec = _download_spec(wos if name == "wos" else (inc or wos), url,
                                      out_dir / f"{name}_openapi_downloaded.json")
                if spec:
                    saved = {"source_url": url, "endpoints": _list_endpoints(spec)}
                    break
            if saved is None:
                saved = {"source_url": None,
                         "endpoints": [],
                         "note": "no public spec URL reachable; using committed snapshot "
                                 "under docs/ (see docs/api_provenance.md)"}
                log.warning("%s: could not download live spec — relying on committed snapshot", name)
            report[f"{name}_spec"] = saved

    # ---- 2a. WoS query-tag verification (count=0 probes) ------------------- #
    if verify_tags:
        tags = dict(cfg.wos_api.get("query_tags", {}))
        variants = cfg.organization.name_variants
        results: dict[str, str] = {}
        for tname, tval in tags.items():
            q = org_query(variants, tval, year=None) if tname == "organization_enhanced" \
                else f'{tval}=("test")'
            # For non-org tags combine with the org clause so the probe stays tiny.
            if tname != "organization_enhanced":
                q = org_query(variants, tags["organization_enhanced"], year=None) + f" AND {q}"
            try:
                meta = wos.count_records(q)
                results[tname] = f"OK (records_found={meta.records_found})"
            except ApiAuthenticationError as exc:
                results[tname] = f"AUTH-ERROR ({exc.status_code})"
                log.error("authentication problem during tag probe: %s", exc)
                break
            except ApiResponseError as exc:
                results[tname] = f"FAILED ({str(exc)[:160]})"
            report["checks"].append({"kind": "wos_query_tag", "tag": tname, "value": tval,
                                     "result": results[tname]})
        report["wos_tag_verification"] = results

    # ---- 2b. InCites runtime verification ---------------------------------- #
    if inc and inc.available:
        try:
            lu = inc.last_updated()
            report["incites_last_updated"] = lu
            report["checks"].append({"kind": "incites_reachability", "result": "OK", "detail": lu})
        except ApiAuthenticationError as exc:
            report["checks"].append({"kind": "incites_reachability",
                                     "result": f"AUTH-ERROR {exc.status_code}: {exc}"})
        except ApiResponseError as exc:
            report["checks"].append({"kind": "incites_reachability", "result": f"FAILED: {exc}"})
        probe_year = min(cfg.time_span.end_year, 2025)
        try:
            n = inc.institution_record_count(probe_year)
            report["incites_institution_probe"] = {"year": probe_year, "record_count": n}
            log.info("InCites institution probe (key-bound profile): %d records in %d", n, probe_year)
        except ApiAuthenticationError as exc:
            report["incites_institution_probe"] = {"error": f"AUTH {exc.status_code}"}
        except ApiResponseError as exc:
            report["incites_institution_probe"] = {"error": str(exc)[:200]}
    else:
        report["incites_institution_probe"] = {"skipped": "INCITES_API_KEY not set"}

    # ---- 3. quota snapshot -------------------------------------------------- #
    report["quota_headers_wos"] = wos.http.quota.as_dict()
    if inc:
        report["quota_headers_incites"] = inc.http.quota.as_dict()
    report["requests"] = counter.snapshot()

    # ---- outputs ------------------------------------------------------------ #
    (out_dir / "endpoints.json").write_text(json.dumps({
        k: v for k, v in report.items() if k.endswith("_spec")}, indent=1), encoding="utf-8")
    md = ["# Discovery report", "", f"Generated: {report['generated_at_utc']}", ""]
    for section in ("wos_spec", "incites_spec"):
        s = report.get(section)
        if not s:
            continue
        md.append(f"## {section}")
        md.append(f"- source: `{s.get('source_url') or 'unreachable — using committed snapshot'}`")
        for ep in s.get("endpoints", [])[:200]:
            ps = ", ".join(f"{p['name']}({p['in']}{',required' if p['required'] else ''})"
                           for p in ep["params"])
            md.append(f"- **{ep['method']}** `{ep['path']}` — {ps}")
        md.append("")
    md.append("## WoS query-tag verification (NEEDS-CHECK items)")
    for k, v in (report.get("wos_tag_verification") or {}).items():
        md.append(f"- `{k}` → {v}")
    md.append("\n## InCites verification")
    md.append(f"- last updated: {json.dumps(report.get('incites_last_updated'))}")
    md.append(f"- institution probe: {json.dumps(report.get('incites_institution_probe'))}")
    md.append("\n## Quota headers observed")
    md.append(f"- WoS: {json.dumps(report.get('quota_headers_wos'))}")
    (out_dir / "discovery_report.md").write_text("\n".join(md), encoding="utf-8")
    (out_dir / "discovery_report.json").write_text(json.dumps(report, indent=1, default=str),
                                                   encoding="utf-8")
    wos.close()
    if inc:
        inc.close()
    log.info("discovery complete — see %s", out_dir / "discovery_report.md")
    return report
