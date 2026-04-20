"""Data models: enums and dataclasses.

Wire-level representations live here. Storage serializes `items` into
`surfaces.data_json`; UI / MD export consume the dataclasses directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SurfaceName(str, Enum):
    SUGGEST = "suggest"
    PAA = "paa"
    RELATED = "related"


class SurfaceStatus(str, Enum):
    RUNNING = "running"
    OK = "ok"
    EMPTY = "empty"
    FAILED = "failed"


class FailureCategory(str, Enum):
    BLOCKED_BY_CAPTCHA = "blocked_by_captcha"
    BLOCKED_BY_CONSENT = "blocked_by_consent"
    BLOCKED_RATE_LIMIT = "blocked_rate_limit"
    SELECTOR_NOT_FOUND = "selector_not_found"
    NETWORK_ERROR = "network_error"
    BROWSER_CRASH = "browser_crash"


class JobStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Suggestion:
    text: str
    rank: int


@dataclass
class PAAQuestion:
    question: str
    rank: int
    answer_preview: str = ""


@dataclass
class RelatedSearch:
    query: str
    rank: int



@dataclass
class SurfaceResult:
    """One row of the `surfaces` table, hydrated with deserialized items."""

    surface: SurfaceName
    status: SurfaceStatus = SurfaceStatus.RUNNING
    failure_category: FailureCategory | None = None
    items: list = field(default_factory=list)
    rank_count: int = 0
    updated_at: str = ""


@dataclass
class AnalysisJob:
    id: int | None = None
    query: str = ""
    language: str = ""
    country: str = ""
    status: JobStatus = JobStatus.RUNNING
    overall_status: JobStatus = JobStatus.RUNNING
    started_at: str = ""
    completed_at: str | None = None
    source_suggest: str = ""
    source_serp: str = ""
    surfaces: dict[SurfaceName, SurfaceResult] = field(default_factory=dict)
