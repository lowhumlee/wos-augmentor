"""Configuration loading, validation and secret handling.

Three YAML files are combined into one typed :class:`AppConfig`:

* ``config/config.yaml``  – organisation, time span, toggles, resources
* ``config/api_map.yaml`  – every endpoint/parameter/header/JSON-path (see
  ``docs/API_ASSUMPTIONS.md`` for the provenance of each value)
* ``config/bibtex.yaml``   – entry-type map, field names, escaping rules

API keys are read **only** from environment variables (or a local ``.env`` file
via python-dotenv).  They are never stored inside the config objects' ``repr``
(they are ``SecretStr``) and never written to disk by this module.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .errors import ConfigError, CredentialsError

DEFAULT_CONFIG_FILES = ("config.yaml", "api_map.yaml", "bibtex.yaml")

ViewMode = Literal["auto", "sr", "fr"]


# --------------------------------------------------------------------------- #
# Sub-models
# --------------------------------------------------------------------------- #
class AddressRoute(BaseModel):
    """Optional AD=-field search route (disabled by default)."""

    model_config = ConfigDict(extra="allow")
    enabled: bool = False
    query: str | None = None
    variants: list[str] = Field(default_factory=list)


class Organization(BaseModel):
    """Target organisation and its searchable name variants."""

    model_config = ConfigDict(extra="allow")
    display_name: str
    country: str = ""
    name_variants: list[str] = Field(default_factory=list)
    address_route: AddressRoute = Field(default_factory=AddressRoute)

    @field_validator("name_variants")
    @classmethod
    def _nonempty_variants(cls, v: list[str]) -> list[str]:
        cleaned = [s.strip() for s in v if s and s.strip()]
        if not cleaned:
            raise ValueError("organization.name_variants must contain at least one variant")
        return cleaned


class TimeSpan(BaseModel):
    """Publication-year window; configurable via YAML *and* CLI overrides."""

    model_config = ConfigDict(extra="allow")
    start_year: int = 1900
    end_year: int = 2026
    min_incites_year: int = 1980

    @model_validator(mode="after")
    def _check(self) -> "TimeSpan":
        if self.start_year > self.end_year:
            raise ValueError(f"start_year {self.start_year} > end_year {self.end_year}")
        if not (1900 <= self.end_year <= 2100):
            raise ValueError("end_year out of plausible range")
        return self

    def years(self) -> list[int]:
        """Inclusive ascending list of publication years to iterate over."""
        return list(range(self.start_year, self.end_year + 1))


class Toggles(BaseModel):
    """Behaviour switches required by the specification."""

    model_config = ConfigDict(extra="allow")
    include_non_org_connected_records: bool = True
    harvest_view_mode: ViewMode = "auto"
    fetch_full_records_once: bool = True
    force_refresh: bool = False


class Resources(BaseModel):
    """Concurrency / retry / paging budget."""

    model_config = ConfigDict(extra="allow")
    concurrency: int = Field(default=4, ge=1, le=16)
    timeout_seconds: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=6, ge=0, le=20)
    backoff_base_seconds: float = Field(default=1.0, gt=0)
    backoff_max_seconds: float = Field(default=120.0, gt=0)
    page_size: int = Field(default=100, ge=1)
    politeness_delay_ms: int = Field(default=150, ge=0)


class Paths(BaseModel):
    """Working/output directories (relative paths resolve against cwd)."""

    model_config = ConfigDict(extra="allow")
    work_dir: Path = Path(".work")
    out_dir: Path = Path("out")


class QuotaCfg(BaseModel):
    model_config = ConfigDict(extra="allow")
    warn_when_estimated_exceeds_remaining: bool = True
    dry_run_uses_short_view: bool = True
    assumed_annual_records: int | None = None  # fallback when header missing


class StatisticsCfg(BaseModel):
    model_config = ConfigDict(extra="allow")
    h_index_formula: str = ""
    citation_source_label_wos: str = "WOS_CC_dedup_total_count"
    citation_source_label_incites: str = "InCites.TIMES_CITED"
    quartile_from_percentile: dict[str, Any] = Field(default_factory=dict)
    jif_edition_year_label: str = ""


class ExpansionCfg(BaseModel):
    model_config = ConfigDict(extra="allow")
    chunk_max_query_chars: int = 1800
    split_by: Literal["year", "none"] = "year"
    identifier_tags: dict[str, str] | None = None


# --------------------------------------------------------------------------- #
# Aggregate
# --------------------------------------------------------------------------- #
class AppConfig(BaseModel):
    """Everything the pipeline needs; constructed once by :func:`load_config`."""

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    schema_version: int = 1
    organization: Organization
    time_span: TimeSpan = Field(default_factory=TimeSpan)
    toggles: Toggles = Field(default_factory=Toggles)
    resources: Resources = Field(default_factory=Resources)
    paths: Paths = Field(default_factory=Paths)
    quota: QuotaCfg = Field(default_factory=QuotaCfg)
    statistics: StatisticsCfg = Field(default_factory=StatisticsCfg)
    expansion: ExpansionCfg = Field(default_factory=ExpansionCfg)

    #: Raw ``wos`` section of api_map.yaml (paths, params, response_paths…).
    wos_api: dict[str, Any] = Field(default_factory=dict)
    #: Raw ``incites`` section of api_map.yaml.
    incites_api: dict[str, Any] = Field(default_factory=dict)
    #: Raw bibtex.yaml.
    bibtex: dict[str, Any] = Field(default_factory=dict)

    wos_key: SecretStr | None = None
    incites_key: SecretStr | None = None

    config_dir: Path = Path("config")

    # ---- convenience accessors ------------------------------------------- #
    def wos_path(self, key: str) -> str:
        """Return a WoS response field path from api_map (raises early on typos)."""
        try:
            return self.wos_api["response_paths"][key]
        except KeyError as exc:  # pragma: no cover - config bug
            raise ConfigError(f"api_map.yaml: wos.response_paths.{key} missing") from exc

    def wos_param(self, key: str) -> str:
        try:
            return self.wos_api["params"][key]
        except KeyError as exc:
            raise ConfigError(f"api_map.yaml: wos.params.{key} missing") from exc

    def query_tag(self, name: str) -> str:
        tags = dict(self.wos_api.get("query_tags", {}))
        if self.expansion.identifier_tags:
            tags.update(self.expansion.identifier_tags)
        if name not in tags:
            raise ConfigError(f"api_map.yaml: wos.query_tags.{name} missing")
        return tags[name]

    def org_match_terms(self) -> list[str]:
        """Lower-cased, diacritic-stripped variants used for affiliation checks."""
        return [normalize_for_matching(v) for v in self.organization.name_variants]

    def effective_years(self) -> list[int]:
        return self.time_span.years()

    def describe_secrets(self) -> str:
        """Human-readable presence report — never contains key material."""
        return (
            f"WOS_API_KEY={'set' if self.wos_key else 'MISSING'}, "
            f"INCITES_API_KEY={'set' if self.incites_key else 'MISSING'}"
        )


def normalize_for_matching(text: str) -> str:
    """Case-fold, strip diacritics and collapse punctuation for org-name matching.

    ``Medical University - Varna`` / ``medical university of varna`` /
    ``MEDICAL UNIV VARNA`` all reduce to comparable forms; abbreviations that
    differ lexically (University vs Univ) are handled by the caller through the
    configured variant list, not by fuzzy logic here.
    """
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = stripped.lower()
    tokens = [t for t in ("".join(c if c.isalnum() else " " for c in lowered)).split() if t]
    return " ".join(tokens)


# --------------------------------------------------------------------------- #
# Loader
# --------------------------------------------------------------------------- #
def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at top level")
    return data


def _load_keys(env_file: Path | None) -> tuple[SecretStr | None, SecretStr | None]:
    """Read API keys from environment (with optional .env), never from YAML."""
    candidates = [env_file] if env_file else []
    candidates += [Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"]
    for cand in candidates:
        if cand and cand.is_file():
            load_dotenv(cand, override=False)
            break
    wos_raw = os.environ.get("WOS_API_KEY") or os.environ.get("WOS_APIKEY") or ""
    inc_raw = os.environ.get("INCITES_API_KEY") or os.environ.get("INCITES_APIKEY") or ""
    return (
        SecretStr(wos_raw.strip()) if wos_raw.strip() else None,
        SecretStr(inc_raw.strip()) if inc_raw.strip() else None,
    )


def load_config(
    config_dir: Path | str = Path("config"),
    *,
    env_file: Path | str | None = None,
    require_keys: bool = True,
    cli_overrides: dict[str, Any] | None = None,
) -> AppConfig:
    """Load and validate all configuration.

    Args:
        config_dir: Directory holding the three YAML files.
        env_file: Explicit ``.env`` path (optional).
        require_keys: When True raise :class:`CredentialsError` if WoS key absent.
        cli_overrides: Flat overrides from argparse, e.g.
            ``{"start_year": 2015, "end_year": 2020, "work_dir": ..., "force_refresh": True}``.

    Returns:
        A fully validated :class:`AppConfig`.
    """
    cfg_dir = Path(config_dir)
    main = _read_yaml(cfg_dir / DEFAULT_CONFIG_FILES[0])
    apimap = _read_yaml(cfg_dir / DEFAULT_CONFIG_FILES[1])
    bib = _read_yaml(cfg_dir / DEFAULT_CONFIG_FILES[2])

    payload: dict[str, Any] = dict(main)
    payload["wos_api"] = apimap.get("wos", {})
    payload["incites_api"] = apimap.get("incites", {})
    payload["bibtex"] = bib
    payload["config_dir"] = cfg_dir

    # Cross-check: page_size must respect the documented maximum count (100).
    res = payload.get("resources", {})
    max_count = payload["wos_api"].get("params", {}).get("max_count", 100)
    if int(res.get("page_size", 100)) > int(max_count):
        raise ConfigError(
            f"resources.page_size={res.get('page_size')} exceeds documented WoS maximum count={max_count}"
        )

    overrides = cli_overrides or {}
    if "start_year" in overrides or "end_year" in overrides:
        ts = dict(payload.get("time_span", {}))
        ts.setdefault("start_year", 1900)
        ts.setdefault("end_year", 2026)
        if overrides.get("start_year") is not None:
            ts["start_year"] = int(overrides["start_year"])
        if overrides.get("end_year") is not None:
            ts["end_year"] = int(overrides["end_year"])
        payload["time_span"] = ts
    if overrides.get("work_dir"):
        payload.setdefault("paths", {})["work_dir"] = Path(overrides["work_dir"])
    if overrides.get("out_dir"):
        payload.setdefault("paths", {})["out_dir"] = Path(overrides["out_dir"])
    if overrides.get("concurrency") is not None:
        payload.setdefault("resources", {})["concurrency"] = int(overrides["concurrency"])
    if overrides.get("force_refresh"):
        payload.setdefault("toggles", {})["force_refresh"] = True
    if overrides.get("include_non_org") is not None:
        payload.setdefault("toggles", {})["include_non_org_connected_records"] = bool(
            overrides["include_non_org"]
        )
    if overrides.get("address_route"):
        payload.setdefault("organization", {}).setdefault("address_route", {})["enabled"] = True

    wos_key, inc_key = _load_keys(Path(env_file) if env_file else None)
    payload["wos_key"] = wos_key
    payload["incites_key"] = inc_key

    try:
        cfg = AppConfig.model_validate(payload)
    except Exception as exc:  # pydantic ValidationError and friends
        raise ConfigError(f"configuration invalid: {exc}") from exc

    if require_keys and cfg.wos_key is None:
        raise CredentialsError(
            "WOS_API_KEY is not set. Export it in the environment or put it in a local .env "
            "file (never in config.yaml). See README 'Setup'."
        )
    return cfg


def validate_runtime(cfg: "AppConfig") -> list[str]:
    """Non-fatal sanity checks surfaced as warnings by the CLI.

    Returns a list of human-readable problems (empty == healthy). Missing
    INCITES_API_KEY is a warning, not an error, because JCR-dependent fields
    are simply left empty (never estimated).
    """
    problems: list[str] = []
    if cfg.time_span.start_year > cfg.time_span.end_year:
        problems.append(
            f"start_year {cfg.time_span.start_year} > end_year {cfg.time_span.end_year}")
    if cfg.time_span.end_year > 2026:
        problems.append(f"end_year {cfg.time_span.end_year} is in the future; WoS may return few records")
    if not cfg.organization.name_variants:
        problems.append("organization.name_variants is empty — OG queries will fail")
    if cfg.incites_key is None:
        problems.append("INCITES_API_KEY not set — impact factor / quartile columns will be EMPTY "
                        "(this is intentional; values are never estimated)")
    if cfg.wos_api.get("auth_header") != "X-ApiKey":
        problems.append("unexpected wos auth header name (expected X-ApiKey per api_map.yaml)")
    return problems
