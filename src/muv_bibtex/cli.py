"""Typer CLI: muv-bibtex <discover|count|harvest|expand|dedup|stats|bibtex|all>.

Exit codes (documented in README):
  0 success · 1 unexpected error · 2 usage/config error · 3 missing credentials
  4 authentication/entitlement failure · 5 incomplete retrieval (rerun!)
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import load_config, validate_runtime
from .errors import (
    ApiAuthenticationError,
    ApiResponseError,
    CompletenessError,
    CredentialsError,
    MuVError,
    PipelineStateError,
)
from .logging_setup import setup_logging

app = typer.Typer(
    name="muv-bibtex",
    help="Bibliometric harvesting & analysis for Medical University of Varna "
         "(WoS Expanded API + InCites Document Level Metrics).",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

EXIT_OK, EXIT_UNEXPECTED, EXIT_USAGE, EXIT_CREDENTIALS, EXIT_AUTH, EXIT_INCOMPLETE = 0, 1, 2, 3, 4, 5

CONFIG_DIR_OPT = typer.Option("config", "--config-dir", "-c",
                              help="Directory with organization.yaml / api_map.yaml / settings.yaml")
LOG_LEVEL_OPT = typer.Option(None, "--log-level", help="DEBUG|INFO|WARNING|ERROR")
LOG_FILE_OPT = typer.Option(None, "--log-file", help="Additional rotating log file")
WORKDIR_OPT = typer.Option(None, "--work-dir", help="Override intermediate work directory")


def _cfg(config_dir: str, work_dir: Optional[str], log_level: Optional[str], log_file: Optional[str]):
    try:
        cfg = load_config(Path(config_dir))
    except CredentialsError as exc:
        print(f"credentials: {exc}", file=sys.stderr)
        raise typer.Exit(code=EXIT_CREDENTIALS)
    except ValueError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        raise typer.Exit(code=EXIT_USAGE)
    if work_dir:
        cfg.paths.work_dir = Path(work_dir)
    setup_logging(level=log_level or cfg.logging.level,
                  quiet=cfg.logging.quiet_console,
                  to_console=cfg.logging.to_console,
                  log_file=Path(log_file) if log_file else cfg.logging.file_path,
                  max_bytes=cfg.logging.max_bytes, backups=cfg.logging.backup_count)
    return cfg


def _fail(exc: Exception, code: int) -> None:
    log = logging.getLogger("muv.cli")
    log.error("%s", exc)
    console.print(f"[red]✗ {type(exc).__name__}:[/red] {exc}")
    raise typer.Exit(code=code)


def _common_guard(cfg) -> None:
    problems = validate_runtime(cfg)
    if problems:
        for p in problems:
            console.print(f"[yellow]⚠ {p}[/yellow]")


# --------------------------------------------------------------------------- #
@app.command()
def discover(
    config_dir: str = CONFIG_DIR_OPT,
    log_level: Optional[str] = LOG_LEVEL_OPT,
    log_file: Optional[str] = LOG_FILE_OPT,
    work_dir: Optional[str] = WORKDIR_OPT,
    verify_tags: bool = typer.Option(True, help="Probe WoS query tags with count=0 searches"),
    download_specs: bool = typer.Option(True, help="Download live OpenAPI definitions"),
) -> None:
    """Verify endpoints/tags/quota against the real APIs before spending quota."""
    cfg = _cfg(config_dir, work_dir, log_level, log_file)
    _common_guard(cfg)
    from .discover import run_discover

    try:
        report = run_discover(cfg, verify_tags=verify_tags, download_specs=download_specs,
                              report_dir=None)
    except CredentialsError as exc:
        _fail(exc, EXIT_CREDENTIALS)
    except ApiAuthenticationError as exc:
        _fail(exc, EXIT_AUTH)
    except MuVError as exc:
        _fail(exc, EXIT_UNEXPECTED)
    tbl = Table(title="Discovery results")
    tbl.add_column("check"); tbl.add_column("result")
    for c in report.get("checks", [])[:20]:
        tbl.add_row(str(c.get("kind")) + (f" [{c.get('tag')}]" if c.get("tag") else ""),
                    str(c.get("result"))[:90])
    console.print(tbl)
    console.print(f"Report: [green]{cfg.paths.discovery_report}[/green] "
                  f"(JSON + endpoints.json alongside)")


@app.command()
def count(
    config_dir: str = CONFIG_DIR_OPT,
    log_level: Optional[str] = LOG_LEVEL_OPT,
    log_file: Optional[str] = LOG_FILE_OPT,
    work_dir: Optional[str] = WORKDIR_OPT,
    start_year: Optional[int] = typer.Option(None, "--start-year"),
    end_year: Optional[int] = typer.Option(None, "--end-year"),
) -> None:
    """Dry-run match counts + quota estimate. Retrieves ZERO records."""
    cfg = _cfg(config_dir, work_dir, log_level, log_file)
    _common_guard(cfg)
    if start_year:
        cfg.time_span.start_year = start_year
    if end_year:
        cfg.time_span.end_year = end_year
    from .harvest import run_harvest
    from .http import RequestCounter, ResponseCache
    from .store import Store
    from .wos_client import WosClient

    store = Store(cfg.paths.work_dir)
    counter = RequestCounter()
    cache = ResponseCache(store.cache_dir())
    try:
        with WosClient(cfg, cache, counter) as wos:
            stats = run_harvest(cfg, wos, store, dry_run=True)
            summary = {
                "phase": "count(dry-run)", "requests": counter.snapshot(),
                "quota_headers": wos.http.quota.as_dict(),
                "years": stats.years_total,
            }
            store.save_run_summary(summary)
    except CredentialsError as exc:
        _fail(exc, EXIT_CREDENTIALS)
    except ApiAuthenticationError as exc:
        _fail(exc, EXIT_AUTH)
    except ApiResponseError as exc:
        _fail(exc, EXIT_UNEXPECTED)


@app.command()
def harvest(
    config_dir: str = CONFIG_DIR_OPT,
    log_level: Optional[str] = LOG_LEVEL_OPT,
    log_file: Optional[str] = LOG_FILE_OPT,
    work_dir: Optional[str] = WORKDIR_OPT,
    start_year: Optional[int] = typer.Option(None, "--start-year"),
    end_year: Optional[int] = typer.Option(None, "--end-year"),
    force_refresh: bool = typer.Option(False, help="Ignore resume ledgers and re-retrieve"),
) -> None:
    """Phase 1: organization-enhanced full-record retrieval (resumable)."""
    cfg = _cfg(config_dir, work_dir, log_level, log_file)
    _common_guard(cfg)
    if start_year:
        cfg.time_span.start_year = start_year
    if end_year:
        cfg.time_span.end_year = end_year
    if force_refresh:
        cfg.toggles.force_refresh = True
    from .harvest import run_harvest
    from .http import RequestCounter, ResponseCache
    from .store import Store
    from .wos_client import WosClient

    store = Store(cfg.paths.work_dir)
    counter = RequestCounter()
    cache = ResponseCache(store.cache_dir(), force_refresh=force_refresh)
    try:
        with WosClient(cfg, cache, counter) as wos:
            stats = run_harvest(cfg, wos, store)
            store.save_run_summary({
                "phase": "harvest", "requests": counter.snapshot(),
                "quota_headers": wos.http.quota.as_dict(),
                "remaining_annual_records": wos.remaining_annual_records(),
                "stats": stats.as_dict(),
            })
        if stats.incomplete_years:
            console.print(f"[red]INCOMPLETE years: {stats.incomplete_years} — rerun 'harvest' "
                          f"to retry them (ledger keeps completed years).[/red]")
            raise typer.Exit(code=EXIT_INCOMPLETE)
        console.print(f"[green]✓ harvest:[/green] {stats.records_raw} raw rows, "
                      f"{stats.unique_uids} unique UTs, identifiers={stats.identifiers}")
    except CredentialsError as exc:
        _fail(exc, EXIT_CREDENTIALS)
    except ApiAuthenticationError as exc:
        _fail(exc, EXIT_AUTH)
    except CompletenessError as exc:
        _fail(exc, EXIT_INCOMPLETE)


@app.command()
def expand(
    config_dir: str = CONFIG_DIR_OPT,
    log_level: Optional[str] = LOG_LEVEL_OPT,
    log_file: Optional[str] = LOG_FILE_OPT,
    work_dir: Optional[str] = WORKDIR_OPT,
    limit_identifiers: Optional[int] = typer.Option(None, help="Cap identifier set (testing)"),
    force_refresh: bool = typer.Option(False),
) -> None:
    """Phase 2: retrieve all publications of harvested author identifiers."""
    cfg = _cfg(config_dir, work_dir, log_level, log_file)
    _common_guard(cfg)
    if force_refresh:
        cfg.toggles.force_refresh = True
    from .harvest import run_expand
    from .http import RequestCounter, ResponseCache
    from .store import Store
    from .wos_client import WosClient

    store = Store(cfg.paths.work_dir)
    counter = RequestCounter()
    cache = ResponseCache(store.cache_dir())
    try:
        with WosClient(cfg, cache, counter) as wos:
            summary = run_expand(cfg, wos, store, limit_identifiers=limit_identifiers)
            prev = store.load_run_summary() or {}
            prev["expand"] = {"requests": counter.snapshot(),
                              "quota_headers": wos.http.quota.as_dict(),
                              **summary}
            store.save_run_summary(prev)
        console.print(f"[green]✓ expand:[/green] {summary}")
    except CredentialsError as exc:
        _fail(exc, EXIT_CREDENTIALS)
    except ApiAuthenticationError as exc:
        _fail(exc, EXIT_AUTH)
    except CompletenessError as exc:
        _fail(exc, EXIT_INCOMPLETE)


@app.command()
def dedup(
    config_dir: str = CONFIG_DIR_OPT,
    log_level: Optional[str] = LOG_LEVEL_OPT,
    log_file: Optional[str] = LOG_FILE_OPT,
    work_dir: Optional[str] = WORKDIR_OPT,
) -> None:
    """Phase 3: merge harvest+expand with provenance; fuzzy dupes flagged only."""
    cfg = _cfg(config_dir, work_dir, log_level, log_file)
    from .dedup import run_dedup
    from .store import Store

    store = Store(cfg.paths.work_dir)
    try:
        res = run_dedup(cfg, store)
    except FileNotFoundError as exc:
        _fail(exc, EXIT_USAGE)
    console.print(f"[green]✓ dedup:[/green] {res.total_input_rows} → {res.unique_records} unique "
                  f"(UT merges {res.ut_collisions_merged}, DOI links {res.doi_links_merged}, "
                  f"fuzzy flagged {res.fuzzy_candidates_flagged})")
    if res.fuzzy_candidates_flagged:
        console.print(f"  Review candidates in {store.file('dedup','dedup_log.csv')}")


@app.command()
def stats(
    config_dir: str = CONFIG_DIR_OPT,
    log_level: Optional[str] = LOG_LEVEL_OPT,
    log_file: Optional[str] = LOG_FILE_OPT,
    work_dir: Optional[str] = WORKDIR_OPT,
    skip_incites: bool = typer.Option(False, help="Do not call InCites even if key is set"),
) -> None:
    """Phase 4: bibliometric indicators (+InCites document metrics when keyed)."""
    cfg = _cfg(config_dir, work_dir, log_level, log_file)
    from .http import RequestCounter, ResponseCache
    from .incites_client import IncitesClient
    from .statistics import run_stats
    from .store import Store, read_rows

    store = Store(cfg.paths.work_dir)
    incites_by_ut = None
    if cfg.incites_key and not skip_incites:
        counter = RequestCounter()
        cache = ResponseCache(store.cache_dir())
        try:
            with IncitesClient(cfg, cache, counter) as inc:
                rows = read_rows(store.file("dedup", "final.jsonl.gz")) \
                    if store.file("dedup", "final.jsonl.gz").exists() else []
                uts = [r.get("ut_digits") for r in rows if r.get("ut_digits")]
                mets = inc.document_metrics_by_ut(uts)
                # enrich each metrics row with derived percentile band
                for ut, row in mets.items():
                    band = inc.percentile_band(row)
                    if band:
                        row["_derived_band"] = band.get("derived_band")
                        row["_catpercentile"] = band.get("catpercentile")
                incites_by_ut = mets
                prev = store.load_run_summary() or {}
                prev["incites"] = {"requests": counter.snapshot(),
                                   "quota_headers": inc.http.quota.as_dict()}
                store.save_run_summary(prev)
        except ApiAuthenticationError as exc:
            console.print(f"[yellow]⚠ InCites authentication failed ({exc.status_code}); "
                          f"continuing WITHOUT metrics — fields stay empty.[/yellow]")
            logging.getLogger("muv.cli").warning("InCites auth: %s", exc)
        except ApiResponseError as exc:
            console.print(f"[yellow]⚠ InCites call failed: {exc} — fields stay empty.[/yellow]")
        except PipelineStateError as exc:
            _fail(exc, EXIT_USAGE)
    try:
        rep = run_stats(cfg, store, incites_by_ut=incites_by_ut)
    except FileNotFoundError as exc:
        _fail(exc, EXIT_USAGE)
    s = rep["scopes"]["all_included"]
    console.print(f"[green]✓ stats:[/green] pubs={s['total_publications']} "
                  f"citations={s['times_cited']['total']} h-index={s['h_index']} "
                  f"highly-cited={s['highly_cited']['count']}")
    console.print(f"  Outputs: {store.phase_dir('stats')} (report.md, summary.json, CSVs)")


@app.command()
def bibtex(
    config_dir: str = CONFIG_DIR_OPT,
    log_level: Optional[str] = LOG_LEVEL_OPT,
    log_file: Optional[str] = LOG_FILE_OPT,
    work_dir: Optional[str] = WORKDIR_OPT,
    output: Optional[str] = typer.Option(None, "--output", "-o"),
) -> None:
    """Generate the .bib file (with JIF + quartile columns from cached InCites)."""
    cfg = _cfg(config_dir, work_dir, log_level, log_file)
    if output:
        cfg.output.filename = Path(output)
    from .bibtex_export import run_bibtex
    from .http import RequestCounter, ResponseCache
    from .incites_client import IncitesClient
    from .store import Store, read_rows

    store = Store(cfg.paths.work_dir)
    metrics = None
    if cfg.incites_key:
        cache = ResponseCache(store.cache_dir())
        try:
            with IncitesClient(cfg, cache, RequestCounter()) as inc:
                mets = inc.document_metrics_by_ut(
                    [r.get("ut_digits") for r in read_rows(store.file("dedup", "final.jsonl.gz"))
                     if r.get("ut_digits")]
                ) if store.file("dedup", "final.jsonl.gz").exists() else {}
                for ut, row in mets.items():
                    band = inc.percentile_band(row)
                    if band:
                        row["_derived_band"] = band.get("derived_band")
                metrics = mets
        except (ApiAuthenticationError, ApiResponseError) as exc:
            console.print(f"[yellow]⚠ InCites unavailable ({exc}); JIF/quartile left empty.[/yellow]")
    try:
        res = run_bibtex(cfg, store, metrics_by_ut=metrics)
    except FileNotFoundError as exc:
        _fail(exc, EXIT_USAGE)
    console.print(f"[green]✓ wrote {res.entries} entries → {cfg.output.filename}[/green]")
    console.print(f"  @misc fallbacks: {res.misc_fallbacks}; missing JIF: {res.missing_jif}; "
                  f"missing quartile/band: {res.missing_quartile}")
    console.print("  NOTE: jcrquartile values are DERIVED from InCites percentile bands "
                  "(not official JCR labels); empties were never estimated.")


@app.command()
def all(
    config_dir: str = CONFIG_DIR_OPT,
    log_level: Optional[str] = LOG_LEVEL_OPT,
    log_file: Optional[str] = LOG_FILE_OPT,
    work_dir: Optional[str] = WORKDIR_OPT,
    start_year: Optional[int] = typer.Option(None, "--start-year"),
    end_year: Optional[int] = typer.Option(None, "--end-year"),
    skip_expand: bool = typer.Option(False, help="Skip identifier expansion phase"),
    yes: bool = typer.Option(False, "--yes", "-y", help="No interactive confirmation"),
) -> None:
    """Full pipeline: harvest → expand → dedup → stats → bibtex."""
    cfg = _cfg(config_dir, work_dir, log_level, log_file)
    _common_guard(cfg)
    if start_year:
        cfg.time_span.start_year = start_year
    if end_year:
        cfg.time_span.end_year = end_year
    if not cfg.wos_key:
        _fail(CredentialsError("WOS_API_KEY is not set"), EXIT_CREDENTIALS)
    if not yes:
        est_note = ("Run 'muv-bibtex count' first to see totals; this will now retrieve "
                    "FULL RECORDS and consume annual record quota.")
        console.print(f"[yellow]{est_note}[/yellow]")
        cont = typer.confirm("Continue with full retrieval?", default=False)
        if not cont:
            console.print("Aborted by operator. Use 'count' for a zero-quota dry run.")
            raise typer.Exit(code=EXIT_OK)
    from .dedup import run_dedup
    from .harvest import run_expand, run_harvest
    from .http import RequestCounter, ResponseCache
    from .wos_client import WosClient

    store = Store(cfg.paths.work_dir)
    counter = RequestCounter()
    cache = ResponseCache(store.cache_dir())
    with WosClient(cfg, cache, counter) as wos:
        hstats = run_harvest(cfg, wos, store)
        if hstats.incomplete_years:
            console.print(f"[red]INCOMPLETE years {hstats.incomplete_years}; "
                          f"continuing with what was retrieved (rerun 'harvest' to retry).[/red]")
        if not skip_expand:
            run_expand(cfg, wos, store)
    store.save_run_summary({"phase": "all:wos", "requests": counter.snapshot(),
                            "quota_headers": wos.http.quota.as_dict(),
                            "remaining_annual_records": wos.remaining_annual_records()})
    dres = run_dedup(cfg, store)
    console.print(f"[green]✓ dedup:[/green] {dres.unique_records} unique records")
    stats(config_dir=config_dir, log_level=log_level, log_file=log_file,
          work_dir=str(cfg.paths.work_dir), skip_incites=False)
    bibtex(config_dir=config_dir, log_level=log_level, log_file=log_file,
           work_dir=str(cfg.paths.work_dir), output=None)
    console.print("[bold green]Pipeline complete.[/bold green] Outputs: "
                  f"{cfg.output.filename}, {Path(cfg.paths.work_dir)}/stats/report.md, "
                  f"{Path(cfg.paths.work_dir)}/run_summary.json")


def main() -> None:  # entry point
    try:
        app()
    except typer.Exit:
        raise
    except MuVError as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        sys.exit(EXIT_UNEXPECTED)


if __name__ == "__main__":
    main()
