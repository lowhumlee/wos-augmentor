"""Versioned intermediate artifact store (JSON Lines, gzip-compressed).

Every phase writes through :class:`Store` so that:

* files carry a ``_meta`` first line with ``schema_version``, tool version and
  retrieval timestamp (citation counts are snapshots — always stamped);
* readers refuse artifacts produced by an incompatible schema version;
* resumability is achieved with *segment manifests*: each phase records the
  queries it completed in ``<phase>/_done.json``; on rerun, completed segments
  are skipped without touching the network (the raw-response cache is the
  second safety net).

Parquet was considered; JSONL.gz keeps zero-extra-dependency portability and
stays diff-friendly for auditing. The format is documented in the README.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import SCHEMA_VERSION, __version__
from .errors import PipelineStateError

log = logging.getLogger("muv.store")


def utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --------------------------------------------------------------------------- #
# JSONL read/write
# --------------------------------------------------------------------------- #
def _atomic_write_lines(path: Path, lines: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    os.close(fd)
    try:
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(tmp, "wt", encoding="utf-8") as fh:
            for ln in lines:
                fh.write(ln if ln.endswith("\n") else ln + "\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]], *, meta: dict[str, Any] | None = None) -> int:
    """Write ``rows`` as JSONL(.gz when path ends .gz); returns row count written."""
    count = 0
    lines: list[str] = []
    header = {
        "_meta": True,
        "schema_version": SCHEMA_VERSION,
        "tool_version": __version__,
        "written_at_utc": utc_now_iso(),
        **(meta or {}),
    }
    lines.append(json.dumps(header, ensure_ascii=False))
    for r in rows:
        lines.append(json.dumps(r, ensure_ascii=False))
        count += 1
    _atomic_write_lines(Path(path), lines)
    return count


def append_jsonl_segment(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    """Append rows to an existing JSONL(.gz) file (used incrementally per query)."""
    n = 0
    p = Path(path)
    opener = gzip.open if p.suffix == ".gz" else open
    p.parent.mkdir(parents=True, exist_ok=True)
    with opener(p, "at", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    return n


def read_jsonl(path: Path, *, expect_meta: bool = True) -> Iterator[dict[str, Any]]:
    """Yield rows; validates the schema-version header and skips the meta line."""
    p = Path(path)
    if not p.is_file():
        raise PipelineStateError(f"intermediate file missing: {p} (run the previous phase first)")
    opener = gzip.open if p.suffix == ".gz" else open
    with opener(p, "rt", encoding="utf-8") as fh:
        first = True
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PipelineStateError(f"{p}:{lineno}: corrupt JSONL ({exc})") from exc
            if first and expect_meta and isinstance(obj, dict) and obj.get("_meta"):
                sv = int(obj.get("schema_version", -1))
                if sv != SCHEMA_VERSION:
                    raise PipelineStateError(
                        f"{p}: schema_version {sv} incompatible with current {SCHEMA_VERSION}; "
                        "delete the work dir or migrate before continuing."
                    )
                first = False
                yield obj  # expose meta as first item under key _meta for callers wanting it
                continue
            first = False
            yield obj


def read_rows(path: Path) -> list[dict[str, Any]]:
    return [o for o in read_jsonl(path) if not o.get("_meta")]


# --------------------------------------------------------------------------- #
# Done-segment bookkeeping (resumability)
# --------------------------------------------------------------------------- #
@dataclass
class SegmentLedger:
    """Tracks completed 'segments' (e.g. one year-query) for one phase.

    Stored as plain JSON at ``<work>/<phase>/_done.json``::

        {"segments": {"harvest:2019": {"records": 137, "finished_at": "..."}}, ...}

    A segment key must be deterministic (query text hash or label). Marking is
    done only after the segment's rows were fully written, so an interrupted
    run re-does exactly the unfinished segment (and even then, cached HTTP
    responses make the redo quota-free).
    """

    path: Path
    segments: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, phase_dir: Path, name: str = "_done.json") -> "SegmentLedger":
        p = Path(phase_dir) / name
        data: dict[str, Any] = {}
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                log.warning("ledger %s unreadable; starting fresh", p)
        return cls(path=p, segments=dict(data.get("segments", {})))

    def is_done(self, key: str) -> bool:
        return key in self.segments

    def mark_done(self, key: str, **info: Any) -> None:
        self.segments[key] = {"finished_at": utc_now_iso(), **info}
        self._flush()

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"schema_version": SCHEMA_VERSION, "segments": self.segments},
                       ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def summary(self) -> dict[str, int]:
        return {"segments_total": len(self.segments)}


# --------------------------------------------------------------------------- #
# Store facade
# --------------------------------------------------------------------------- #
class Store:
    """Directory layout helper shared by all phases.

    Layout under ``cfg.paths.work_dir``::

        cache/               raw API responses (ResponseCache)
        harvest/records.jsonl.gz          raw FR/SR records (phase 1)
        harvest/identifiers.jsonl         author identifier hits
        harvest/_done.json                completed year/month segments
        expand/records.jsonl.gz           identifier-derived records (phase 2)
        expand/_done.json
        dedup/final.jsonl.gz              unique records w/ provenance (phase 3)
        dedup/dedup_log.csv
        stats/…                           csv/xlsx/json/md (phase 4)
        run_summary.json                  quota snapshot + counters per run
    """

    def __init__(self, work_dir: Path | str) -> None:
        self.root = Path(work_dir)
        self.root.mkdir(parents=True, exist_ok=True)

    # paths --------------------------------------------------------------- #
    def cache_dir(self) -> Path:
        p = self.root / "cache"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def phase_dir(self, phase: str) -> Path:
        p = self.root / phase
        p.mkdir(parents=True, exist_ok=True)
        return p

    def file(self, phase: str, name: str) -> Path:
        return self.phase_dir(phase) / name

    # ledgers ------------------------------------------------------------- #
    def ledger(self, phase: str) -> SegmentLedger:
        return SegmentLedger.load(self.phase_dir(phase))

    # run summary ---------------------------------------------------------- #
    def save_run_summary(self, payload: dict[str, Any]) -> Path:
        p = self.root / "run_summary.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                       encoding="utf-8")
        os.replace(tmp, p)
        return p

    def load_run_summary(self) -> dict[str, Any] | None:
        p = self.root / "run_summary.json"
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
        return None
