"""Unit 6: render_analysis_to_md + build_filename + slugify."""

from __future__ import annotations

from pathlib import Path

import pytest

from seoserper.export import (
    build_filename,
    render_analysis_to_md,
    slugify,
)
from seoserper.models import (
    AnalysisJob,
    FailureCategory,
    JobStatus,
    PAAQuestion,
    RelatedSearch,
    Suggestion,
    SurfaceName,
    SurfaceResult,
    SurfaceStatus,
)

FIXTURES = Path(__file__).parent / "fixtures" / "export"
STAMP = "2026-04-20 14:30:00"


def _job(
    *,
    query: str = "best running shoes",
    lang: str = "en",
    country: str = "us",
    surfaces: dict[SurfaceName, SurfaceResult] | None = None,
    status: JobStatus = JobStatus.COMPLETED,
    stamp: str = STAMP,
) -> AnalysisJob:
    return AnalysisJob(
        id=1,
        query=query,
        language=lang,
        country=country,
        status=status,
        overall_status=status,
        started_at=stamp,
        source_suggest="Google Suggest API",
        source_serp="SerpAPI",
        surfaces=surfaces or {},
    )


def _ok_suggest() -> SurfaceResult:
    return SurfaceResult(
        surface=SurfaceName.SUGGEST,
        status=SurfaceStatus.OK,
        items=[
            Suggestion(text="best running shoes", rank=1),
            Suggestion(text="best running shoes for men", rank=2),
            Suggestion(text="best running shoes for women", rank=3),
        ],
        rank_count=3,
    )


def _ok_paa() -> SurfaceResult:
    return SurfaceResult(
        surface=SurfaceName.PAA,
        status=SurfaceStatus.OK,
        items=[
            PAAQuestion(
                question="What are the top-rated running shoes in 2026?", rank=1
            ),
            PAAQuestion(
                question="Are Nike running shoes good for flat feet?",
                rank=2,
                answer_preview="Generally yes for neutral runners.",
            ),
        ],
        rank_count=2,
    )


def _ok_related() -> SurfaceResult:
    return SurfaceResult(
        surface=SurfaceName.RELATED,
        status=SurfaceStatus.OK,
        items=[
            RelatedSearch(query="trail running shoes", rank=1),
            RelatedSearch(query="running shoes near me", rank=2),
        ],
        rank_count=2,
    )


# --- byte-equal golden comparisons -------------------------------------------


def test_all_ok_matches_golden():
    job = _job(
        surfaces={
            SurfaceName.SUGGEST: _ok_suggest(),
            SurfaceName.PAA: _ok_paa(),
            SurfaceName.RELATED: _ok_related(),
        }
    )
    expected = (FIXTURES / "expected_all_ok.md").read_text()
    assert render_analysis_to_md(job) == expected


def test_partial_matches_golden():
    job = _job(
        query="长尾中文查询示例",
        lang="zh",
        country="cn",
        surfaces={
            SurfaceName.SUGGEST: SurfaceResult(
                surface=SurfaceName.SUGGEST,
                status=SurfaceStatus.OK,
                items=[Suggestion(text="长尾中文查询示例", rank=1)],
                rank_count=1,
            ),
            SurfaceName.PAA: SurfaceResult(
                surface=SurfaceName.PAA,
                status=SurfaceStatus.FAILED,
                failure_category=FailureCategory.BLOCKED_RATE_LIMIT,
            ),
            SurfaceName.RELATED: SurfaceResult(
                surface=SurfaceName.RELATED, status=SurfaceStatus.EMPTY
            ),
        },
    )
    expected = (FIXTURES / "expected_partial.md").read_text()
    assert render_analysis_to_md(job) == expected


def test_all_failed_matches_golden():
    job = _job(
        query="xxyyzz",
        status=JobStatus.FAILED,
        surfaces={
            SurfaceName.SUGGEST: SurfaceResult(
                surface=SurfaceName.SUGGEST,
                status=SurfaceStatus.FAILED,
                failure_category=FailureCategory.NETWORK_ERROR,
            ),
            SurfaceName.PAA: SurfaceResult(
                surface=SurfaceName.PAA,
                status=SurfaceStatus.FAILED,
                failure_category=FailureCategory.BLOCKED_RATE_LIMIT,
            ),
            SurfaceName.RELATED: SurfaceResult(
                surface=SurfaceName.RELATED,
                status=SurfaceStatus.FAILED,
                failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            ),
        },
    )
    expected = (FIXTURES / "expected_all_failed.md").read_text()
    assert render_analysis_to_md(job) == expected


# --- frontmatter fields ------------------------------------------------------


def test_frontmatter_carries_source_and_status_triplet():
    job = _job(
        surfaces={
            SurfaceName.SUGGEST: _ok_suggest(),
            SurfaceName.PAA: SurfaceResult(
                surface=SurfaceName.PAA,
                status=SurfaceStatus.FAILED,
                failure_category=FailureCategory.BLOCKED_RATE_LIMIT,
            ),
            SurfaceName.RELATED: SurfaceResult(
                surface=SurfaceName.RELATED, status=SurfaceStatus.EMPTY
            ),
        }
    )
    md = render_analysis_to_md(job)
    assert "source_suggest: Google Suggest API" in md
    assert "source_serp: SerpAPI" in md
    assert "status_suggestions: ok" in md
    assert "status_paa: failed" in md
    assert "status_related: empty" in md


def test_timestamp_frontmatter_is_utc_iso8601():
    job = _job(stamp="2026-04-20 14:30:00")
    md = render_analysis_to_md(job)
    assert "timestamp: 2026-04-20T14:30:00Z" in md


def test_non_utc_timestamp_normalized_to_utc():
    """Stamps with tz offset get rewritten to Z-suffix UTC."""
    job = _job(stamp="2026-04-20T10:30:00+08:00")
    md = render_analysis_to_md(job)
    assert "timestamp: 2026-04-20T02:30:00Z" in md


def test_missing_timestamp_tolerated():
    job = _job(stamp="")
    md = render_analysis_to_md(job)
    # Frontmatter still renders with empty timestamp rather than crashing.
    assert "timestamp: " in md


# --- markdown escaping -------------------------------------------------------


def test_paa_question_with_metachars_is_escaped():
    job = _job(
        surfaces={
            SurfaceName.SUGGEST: SurfaceResult(surface=SurfaceName.SUGGEST, status=SurfaceStatus.EMPTY),
            SurfaceName.PAA: SurfaceResult(
                surface=SurfaceName.PAA,
                status=SurfaceStatus.OK,
                items=[
                    PAAQuestion(
                        question="What about *bold* and _italic_ and `code`?",
                        rank=1,
                    )
                ],
                rank_count=1,
            ),
            SurfaceName.RELATED: SurfaceResult(surface=SurfaceName.RELATED, status=SurfaceStatus.EMPTY),
        }
    )
    md = render_analysis_to_md(job)
    # Metachars are backslash-escaped so downstream renderers don't expand them.
    assert r"What about \*bold\* and \_italic\_ and \`code\`?" in md


def test_related_with_brackets_is_escaped():
    job = _job(
        surfaces={
            SurfaceName.SUGGEST: SurfaceResult(surface=SurfaceName.SUGGEST, status=SurfaceStatus.EMPTY),
            SurfaceName.PAA: SurfaceResult(surface=SurfaceName.PAA, status=SurfaceStatus.EMPTY),
            SurfaceName.RELATED: SurfaceResult(
                surface=SurfaceName.RELATED,
                status=SurfaceStatus.OK,
                items=[RelatedSearch(query="shoes [budget pick]", rank=1)],
                rank_count=1,
            ),
        }
    )
    md = render_analysis_to_md(job)
    assert r"shoes \[budget pick\]" in md


# --- filename / slugify ------------------------------------------------------


def test_filename_basic():
    job = _job(query="best running shoes")
    assert (
        build_filename(job)
        == "seoserper-best-running-shoes-en-us-20260420-1430.md"
    )


def test_filename_uses_non_ascii_base64_fallback():
    job = _job(query="最佳跑鞋", lang="zh", country="cn")
    name = build_filename(job)
    assert name.startswith("seoserper-q-")
    assert name.endswith("-zh-cn-20260420-1430.md")


def test_slugify_truncates_long_inputs():
    text = "x" * 100
    slug = slugify(text)
    assert len(slug) == 60
    assert slug == "x" * 60


def test_slugify_collapses_consecutive_specials():
    assert slugify("hello   world & co!") == "hello-world-co"


def test_slugify_fallback_on_pure_specials():
    slug = slugify("!!!")
    # Specials hash to base64 → non-empty slug
    assert slug.startswith("q-")


def test_slugify_empty_input_is_placeholder():
    assert slugify("") == "query-empty"


# --- running status ----------------------------------------------------------


def test_running_surface_renders_pending():
    """If a surface hasn't completed yet, section shows 'Pending.'"""
    job = _job(
        surfaces={
            SurfaceName.SUGGEST: SurfaceResult(surface=SurfaceName.SUGGEST, status=SurfaceStatus.RUNNING),
            SurfaceName.PAA: SurfaceResult(surface=SurfaceName.PAA, status=SurfaceStatus.RUNNING),
            SurfaceName.RELATED: SurfaceResult(surface=SurfaceName.RELATED, status=SurfaceStatus.RUNNING),
        },
        status=JobStatus.RUNNING,
    )
    md = render_analysis_to_md(job)
    assert md.count("_Pending._") == 3


def test_missing_surface_from_dict_also_renders_pending():
    """Defensive: if a surface key is absent, still produces a legal section."""
    job = _job(surfaces={})
    md = render_analysis_to_md(job)
    assert "## Suggestions" in md
    assert "## People Also Ask" in md
    assert "## Related Searches" in md
    assert md.count("_Pending._") == 3


# --- purity ------------------------------------------------------------------


def test_render_is_deterministic():
    job = _job(surfaces={SurfaceName.SUGGEST: _ok_suggest(),
                          SurfaceName.PAA: _ok_paa(),
                          SurfaceName.RELATED: _ok_related()})
    assert render_analysis_to_md(job) == render_analysis_to_md(job)


def test_render_does_no_io(tmp_path, monkeypatch):
    """render_analysis_to_md must not touch the filesystem."""
    forbidden_calls = []

    real_open = open

    def spy_open(path, *args, **kwargs):
        forbidden_calls.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", spy_open)

    job = _job(surfaces={SurfaceName.SUGGEST: _ok_suggest(),
                          SurfaceName.PAA: _ok_paa(),
                          SurfaceName.RELATED: _ok_related()})
    render_analysis_to_md(job)
    assert forbidden_calls == []


# --- suggest-only mode (Unit 3 of pivot plan 2026-04-20-002) -----------------


def _suggest_only_job(
    query: str = "python decorators",
    items: list | None = None,
    status: SurfaceStatus = SurfaceStatus.OK,
    failure_category: FailureCategory | None = None,
) -> AnalysisJob:
    return AnalysisJob(
        id=4,
        query=query,
        language="en",
        country="us",
        status=JobStatus.COMPLETED,
        overall_status=JobStatus.COMPLETED,
        started_at=STAMP,
        source_suggest="Google Suggest API",
        source_serp="SerpAPI",
        render_mode="suggest-only",
        surfaces={
            SurfaceName.SUGGEST: SurfaceResult(
                surface=SurfaceName.SUGGEST,
                status=status,
                items=items or [],
                rank_count=len(items or []),
                failure_category=failure_category,
            ),
        },
    )


def test_suggest_only_matches_golden():
    job = _suggest_only_job(
        items=[
            Suggestion(text="python decorators", rank=1),
            Suggestion(text="python decorators example", rank=2),
            Suggestion(text="python decorators explained", rank=3),
        ]
    )
    expected = (FIXTURES / "expected_suggest_only.md").read_text()
    assert render_analysis_to_md(job) == expected


def test_suggest_only_frontmatter_omits_paa_and_related_status_keys():
    job = _suggest_only_job(items=[Suggestion(text="x", rank=1)])
    md = render_analysis_to_md(job)
    assert "render_mode: suggest-only" in md
    assert "status_suggestions: ok" in md
    assert "status_paa:" not in md
    assert "status_related:" not in md
    assert "source_serp:" not in md  # source_serp also omitted since no SERP run


def test_suggest_only_body_has_single_h2():
    job = _suggest_only_job(items=[Suggestion(text="x", rank=1)])
    md = render_analysis_to_md(job)
    # Exactly one "## " heading (the Suggestions section)
    assert md.count("\n## ") == 1
    assert "## Suggestions (1)" in md
    assert "## People Also Ask" not in md
    assert "## Related Searches" not in md


def test_suggest_only_empty_renders_empty_message():
    job = _suggest_only_job(items=[], status=SurfaceStatus.EMPTY)
    md = render_analysis_to_md(job)
    assert "Google Autocomplete returned no suggestions" in md
    assert md.count("\n## ") == 1


def test_suggest_only_failed_renders_diagnostic():
    job = _suggest_only_job(
        status=SurfaceStatus.FAILED,
        failure_category=FailureCategory.NETWORK_ERROR,
    )
    md = render_analysis_to_md(job)
    assert "Suggestions 获取失败: 网络错误" in md


def test_full_mode_frontmatter_still_has_render_mode_full():
    job = _job(
        surfaces={
            SurfaceName.SUGGEST: _ok_suggest(),
            SurfaceName.PAA: _ok_paa(),
            SurfaceName.RELATED: _ok_related(),
        }
    )
    md = render_analysis_to_md(job)
    assert "render_mode: full" in md
    # Full mode keeps all three status keys
    assert "status_suggestions: ok" in md
    assert "status_paa: ok" in md
    assert "status_related: ok" in md


# --- CSV export (plan 004 Unit E) --------------------------------------------


import csv as _csv_mod
import io as _io_mod

from seoserper.export import (
    build_csv_filename,
    render_analysis_to_csv,
    CSV_COLUMNS,
)


def _ok_job() -> AnalysisJob:
    return _job(
        surfaces={
            SurfaceName.SUGGEST: _ok_suggest(),
            SurfaceName.PAA: _ok_paa(),
            SurfaceName.RELATED: _ok_related(),
        }
    )


def test_csv_golden_all_ok():
    # Read bytes to avoid Python's universal-newline conversion of \r\n → \n;
    # CSV golden must match exactly down to line endings (RFC 4180 + Excel).
    expected_bytes = (FIXTURES / "expected_all_ok.csv").read_bytes()
    expected = expected_bytes.decode("utf-8")
    assert render_analysis_to_csv(_ok_job()) == expected


def test_csv_header_row_always_present():
    empty_job = _job(surfaces={})
    csv_str = render_analysis_to_csv(empty_job)
    # BOM + header + no data rows
    assert csv_str.startswith("\ufeff")
    assert "surface,rank,text,answer_preview" in csv_str


def test_csv_column_order_stable():
    csv_str = render_analysis_to_csv(_ok_job()).lstrip("\ufeff")
    reader = _csv_mod.reader(_io_mod.StringIO(csv_str))
    header = next(reader)
    assert tuple(header) == CSV_COLUMNS


def test_csv_suggest_rows_have_empty_answer_preview():
    csv_str = render_analysis_to_csv(_ok_job()).lstrip("\ufeff")
    reader = _csv_mod.DictReader(_io_mod.StringIO(csv_str))
    suggest_rows = [r for r in reader if r["surface"] == "suggest"]
    assert len(suggest_rows) == 3
    assert all(r["answer_preview"] == "" for r in suggest_rows)


def test_csv_paa_row_carries_answer_preview():
    csv_str = render_analysis_to_csv(_ok_job()).lstrip("\ufeff")
    reader = _csv_mod.DictReader(_io_mod.StringIO(csv_str))
    paa_rows = [r for r in reader if r["surface"] == "paa"]
    assert len(paa_rows) == 2
    # Second PAA item in _ok_paa() carries the answer preview.
    assert paa_rows[1]["answer_preview"] == "Generally yes for neutral runners."


def test_csv_only_ok_surfaces_contribute_rows():
    job = _job(
        surfaces={
            SurfaceName.SUGGEST: _ok_suggest(),
            SurfaceName.PAA: SurfaceResult(
                surface=SurfaceName.PAA,
                status=SurfaceStatus.FAILED,
                failure_category=FailureCategory.BLOCKED_RATE_LIMIT,
            ),
            SurfaceName.RELATED: SurfaceResult(
                surface=SurfaceName.RELATED, status=SurfaceStatus.EMPTY
            ),
        }
    )
    csv_str = render_analysis_to_csv(job).lstrip("\ufeff")
    reader = _csv_mod.DictReader(_io_mod.StringIO(csv_str))
    rows = list(reader)
    assert {r["surface"] for r in rows} == {"suggest"}  # PAA/Related suppressed


def test_csv_suggest_only_job_renders_one_surface():
    job = _job(
        surfaces={SurfaceName.SUGGEST: _ok_suggest()},
        status=JobStatus.COMPLETED,
    )
    csv_str = render_analysis_to_csv(job).lstrip("\ufeff")
    reader = _csv_mod.DictReader(_io_mod.StringIO(csv_str))
    rows = list(reader)
    assert {r["surface"] for r in rows} == {"suggest"}


def test_csv_unicode_chinese_japanese_preserved():
    job = _job(
        query="跑步鞋推荐",
        lang="zh",
        country="cn",
        surfaces={
            SurfaceName.SUGGEST: SurfaceResult(
                surface=SurfaceName.SUGGEST,
                status=SurfaceStatus.OK,
                items=[Suggestion(text="跑步鞋推荐", rank=1)],
                rank_count=1,
            ),
            SurfaceName.PAA: SurfaceResult(
                surface=SurfaceName.PAA,
                status=SurfaceStatus.OK,
                items=[PAAQuestion(question="跑步鞋如何选择？", rank=1, answer_preview="内翻型脚选支撑")],
                rank_count=1,
            ),
            SurfaceName.RELATED: SurfaceResult(surface=SurfaceName.RELATED, status=SurfaceStatus.EMPTY),
        },
    )
    csv_str = render_analysis_to_csv(job)
    assert csv_str.startswith("\ufeff")
    assert "跑步鞋推荐" in csv_str
    assert "跑步鞋如何选择？" in csv_str


def test_csv_crlf_line_endings():
    csv_str = render_analysis_to_csv(_ok_job())
    # Every data row ends \r\n (RFC 4180 compat with Excel)
    rows = csv_str.split("\r\n")
    # Empty trailing split is expected from trailing \r\n.
    assert rows[-1] == ""
    # At least one real row
    assert len(rows) > 2


def test_build_csv_filename_shape():
    job = _job(query="coffee")
    name = build_csv_filename(job)
    assert name.startswith("seoserper-coffee-en-us-")
    assert name.endswith(".csv")
