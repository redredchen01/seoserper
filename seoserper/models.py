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
    # Post plan 003: only these 3 values are produced. Captcha / consent /
    # browser_crash were Playwright-era and died with render.py.
    BLOCKED_RATE_LIMIT = "blocked_rate_limit"
    SELECTOR_NOT_FOUND = "selector_not_found"
    NETWORK_ERROR = "network_error"


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
class ParseResult:
    """Return type of Unit 4's parse_serp(). One per SERP surface (PAA / Related)."""

    status: SurfaceStatus
    items: list = field(default_factory=list)
    failure_category: FailureCategory | None = None


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
    # "full" = 3-surface (suggest + paa + related), "suggest-only" = 1-surface.
    # Stamped at create_job based on config.SERPAPI_KEY; insulates historical
    # jobs from live config mutation so a retry on a pre-pivot full-mode job
    # never mutates mid-flight.
    render_mode: str = "full"
    # "google" (default; pre-plan-005 rows auto-tag via migration) or "bing".
    # Orthogonal to render_mode. Bing jobs always seed 2 surfaces (PAA +
    # RELATED) since Bing has no public autocomplete endpoint.
    engine: str = "google"
    surfaces: dict[SurfaceName, SurfaceResult] = field(default_factory=dict)
