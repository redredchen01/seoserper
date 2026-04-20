"""Model surface: enum values + dataclass defaults + round-trip via asdict/json."""

from __future__ import annotations

import json
from dataclasses import asdict

from seoserper.models import (
    AnalysisJob,
    FailureCategory,
    JobStatus,
    PAAQuestion,
    RelatedSearch,
    Suggestion,
    SurfaceName,
    SurfaceStatus,
    SurfaceResult,
)


def test_surface_name_enum_values():
    assert SurfaceName.SUGGEST.value == "suggest"
    assert SurfaceName.PAA.value == "paa"
    assert SurfaceName.RELATED.value == "related"
    assert {s.value for s in SurfaceName} == {"suggest", "paa", "related"}


def test_surface_status_enum_values():
    assert {s.value for s in SurfaceStatus} == {"running", "ok", "empty", "failed"}


def test_failure_category_six_values():
    assert {c.value for c in FailureCategory} == {
        "blocked_by_captcha",
        "blocked_by_consent",
        "blocked_rate_limit",
        "selector_not_found",
        "network_error",
        "browser_crash",
    }


def test_job_status_enum_values():
    assert {s.value for s in JobStatus} == {"running", "completed", "failed"}


def test_suggestion_round_trip():
    s = Suggestion(text="best running shoes", rank=1)
    restored = Suggestion(**json.loads(json.dumps(asdict(s))))
    assert restored == s


def test_paa_question_round_trip_with_preview():
    q = PAAQuestion(question="Are Nikes good?", rank=2, answer_preview="Yes, for neutral runners.")
    restored = PAAQuestion(**json.loads(json.dumps(asdict(q))))
    assert restored == q


def test_related_search_round_trip():
    r = RelatedSearch(query="trail running shoes", rank=3)
    restored = RelatedSearch(**json.loads(json.dumps(asdict(r))))
    assert restored == r


def test_analysis_job_defaults_are_empty():
    job = AnalysisJob()
    assert job.id is None
    assert job.status == JobStatus.RUNNING
    assert job.overall_status == JobStatus.RUNNING
    assert job.surfaces == {}


def test_surface_result_defaults():
    sr = SurfaceResult(surface=SurfaceName.SUGGEST)
    assert sr.status == SurfaceStatus.RUNNING
    assert sr.failure_category is None
    assert sr.items == []
    assert sr.rank_count == 0
