"""Structured logging: console (human) + rotating file (JSON lines).

API keys are scrubbed from *every* log line as a defence-in-depth measure: if a
secret ever leaks into an exception message or a URL repr, it is replaced by
``***REDACTED***`` before reaching stdout or the log file.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
import time
from pathlib import Path
from typing import Iterable

_CONSOLE_FMT = "%(asctime)s %(levelname)-7s %(name)-28s %(message)s"
_DATE_FMT = "%H:%M:%S"

# Matches anything that looks like a key=value pair where the key mentions an
# api key, plus bare 32-64 hex strings (Clarivate keys are long hex tokens).
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(x[-_ ]?api[-_ ]?key|apikey|wos_api_key|incites_api_key)(\s*[:=]\s*)(\S+)"),
    re.compile(r"\b[0-9a-f]{32,64}\b"),
)


def redact(text: str) -> str:
    """Return *text* with any secret-looking substring replaced.

    Args:
        text: Arbitrary log-ready string.

    Returns:
        The same string with secrets replaced by ``***REDACTED***``.
    """
    out = text
    out = _SECRET_PATTERNS[0].sub(lambda m: f"{m.group(1)}{m.group(2)}***REDACTED***", out)
    out = _SECRET_PATTERNS[1].sub("***REDACTED***", out)
    return out


class RedactingFilter(logging.Filter):
    """Scrub secrets from every record that passes through it."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D102
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        red = redact(msg)
        if red != msg:
            record.msg = red
            record.args = ()
        return True


class JsonLineFormatter(logging.Formatter):
    """One JSON object per line — machine-readable run log."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: D102
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update({k: redact(str(v)) for k, v in extra.items()})
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(
    log_dir: Path | str,
    *,
    level: int = logging.INFO,
    verbose: bool = False,
    log_file: Path | None = None,
) -> logging.Logger:
    """Configure root logging once and return the package logger.

    Args:
        log_dir: Directory for ``run.log`` (created if needed).
        level: Console level; ``DEBUG`` when *verbose*.
        verbose: Enable debug console output.
        log_file: Optional extra plain-text log file (in addition to the
            JSON-lines ``run.log`` written under *log_dir*).

    Returns:
        The ``muv_bibtex`` package logger.
    """
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if getattr(root, "_muv_configured", False):
        return logging.getLogger("muv_bibtex")

    root.setLevel(logging.DEBUG)
    cons = logging.StreamHandler(sys.stderr)
    cons.setLevel(logging.DEBUG if verbose else level)
    cons.setFormatter(logging.Formatter(_CONSOLE_FMT, _DATE_FMT))
    cons.addFilter(RedactingFilter())

    file_h = logging.handlers.RotatingFileHandler(
        log_path / "run.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_h.setLevel(logging.DEBUG)
    file_h.setFormatter(JsonLineFormatter())
    file_h.addFilter(RedactingFilter())

    root.addHandler(cons)
    root.addHandler(file_h)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        extra_h = logging.FileHandler(log_file, encoding="utf-8")
        extra_h.setLevel(logging.DEBUG)
        extra_h.setFormatter(logging.Formatter(_CONSOLE_FMT, _DATE_FMT))
        extra_h.addFilter(RedactingFilter())
        root.addHandler(extra_h)

    root._muv_configured = True  # type: ignore[attr-defined]

    # Third-party noise reduction.
    for noisy in ("urllib3", "requests", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    log = logging.getLogger("muv_bibtex")
    log.debug("logging initialised -> %s", log_path / "run.log")
    return log


def log_fields(logger: logging.Logger, level: int, message: str, **fields: object) -> None:
    """Log *message* with structured key/value fields attached."""
    logger.log(level, message, extra={"fields": fields})


def format_progress(done: int, total: int, *, label: str = "", width: int = 30) -> str:
    """Single-line progress bar for console display (no external dependency)."""
    total = max(total, 1)
    frac = min(max(done / total, 0.0), 1.0)
    filled = int(width * frac)
    bar = "#" * filled + "-" * (width - filled)
    return f"\r{label}[{bar}] {done}/{total} ({frac * 100:5.1f}%)"


def iter_with_progress(iterable: Iterable[object], *, label: str, every: int = 1) -> Iterable[object]:
    """Wrap *iterable* emitting a progress line to stderr every *every* items."""
    total = len(iterable) if hasattr(iterable, "__len__") else 0  # type: ignore[arg-type]
    done = 0
    for item in iterable:
        yield item
        done += 1
        if done % every == 0 and total:
            sys.stderr.write(format_progress(done, total, label=label))
            sys.stderr.flush()
    if total:
        sys.stderr.write(format_progress(total, total, label=label) + "\n")
        sys.stderr.flush()
