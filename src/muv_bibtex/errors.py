"""Typed exceptions raised across the pipeline.

Every error message is written to be actionable for the operator and must never
contain an API key (keys are only ever present in one request header which this
module never sees).
"""
from __future__ import annotations


class MuvError(Exception):
    """Base class for all tool errors."""


class ConfigError(MuvError):
    """Configuration file missing, unreadable or semantically invalid."""


class CredentialsError(MuvError):
    """API key absent or empty.

    Raised before any network call so the operator gets a clear message instead
    of an HTTP 401.
    """


class ApiAuthenticationError(MuvError):
    """HTTP 401/403 — invalid key, key without product entitlement, or scope problem."""

    def __init__(self, message: str, *, status_code: int, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class ApiResponseError(MuvError):
    """Unexpected HTTP status or malformed payload after retries were exhausted."""

    def __init__(self, message: str, *, status_code: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class QuotaExceededWarning(MuvError):
    """Estimated record consumption exceeds the reported remaining annual quota."""


class CompletenessError(MuvError):
    """A query returned fewer records than its reported total and splitting failed."""


class PipelineStateError(MuvError):
    """A phase was asked to run without the intermediate output of a previous phase."""


class ValidationFailure(MuvError):
    """The produced BibTeX failed at least one validation assertion."""
