"""Unit 5: AnalysisEngine orchestration, retry, progress events, error paths."""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from pathlib import Path

import pytest

from seoserper.core.engine import AnalysisEngine, ProgressEvent, _build_serp_url
from seoserper.core.render import (
    BlockedByCaptchaError,
    BlockedByConsentError,
    BlockedRateLimitError,
    BrowserCrashError,
)
from seoserper.fetchers.suggest import SuggestResult
from seoserper.models import (
    FailureCategory,
    JobStatus,
    PAAQuestion,
    ParseResult,
    RelatedSearch,
    Suggestion,
    SurfaceName,
    SurfaceStatus,
)
from seoserper.storage import get_job


# --- fakes -------------------------------------------------------------------


class FakeRenderThread:
    """Matches RenderThread.submit() surface. Configurable outcome per call."""

    def __init__(self, html: str | BaseException = "<html/>"):
        self.outcome = html
        self.calls: list[str] = []

    def submit(self, url: str) -> Future:
        self.calls.append(url)
        fut: Future = Future()
        if isinstance(self.outcome, BaseException):
            fut.set_exception(self.outcome)
        else:
            fut.set_result(self.outcome)
        return fut


def _ok_suggest(query="coffee") -> SuggestResult:
    return SuggestResult(
        status=SurfaceStatus.OK,
        items=[Suggestion(text=query, rank=1), Suggestion(text=f"{query} shop", rank=2)],
    )


def _failed_suggest(category=FailureCategory.NETWORK_ERROR) -> SuggestResult:
    return SuggestResult(status=SurfaceStatus.FAILED, failure_category=category)


def _ok_parsed() -> dict[SurfaceName, ParseResult]:
    return {
        SurfaceName.PAA: ParseResult(
            status=SurfaceStatus.OK,
            items=[PAAQuestion(question="Is coffee good?", rank=1)],
        ),
        SurfaceName.RELATED: ParseResult(
            status=SurfaceStatus.OK,
            items=[RelatedSearch(query="espresso", rank=1)],
        ),
    }


def _drain(engine: AnalysisEngine, expect_complete: bool = True, timeout: float = 2.0
           ) -> list[ProgressEvent]:
    """Block until a complete / error event arrives, returning all events."""
    deadline = time.monotonic() + timeout
    events: list[ProgressEvent] = []
    while time.monotonic() < deadline:
        try:
            ev = engine.progress_queue.get(timeout=0.05)
        except Exception:
            continue
        events.append(ev)
        if ev.kind in ("complete", "error"):
            if not expect_complete:
                continue
            return events
    raise AssertionError(f"no terminal event within {timeout}s; got {events}")


# --- happy path --------------------------------------------------------------


def test_submit_creates_job_and_returns_id(db_path):
    engine = AnalysisEngine(
        render_thread=FakeRenderThread(),
        parse_fn=lambda html, locale: _ok_parsed(),
        db_path=db_path,
        fetch_fn=lambda q, l, c: _ok_suggest(q),
    )
    job_id = engine.submit("coffee", "en", "us")
    assert isinstance(job_id, int) and job_id > 0

    events = _drain(engine)
    kinds = [e.kind for e in events]
    assert kinds == ["start", "suggest", "paa", "related", "complete"]

    job = get_job(job_id, db_path=db_path)
    assert job.status == JobStatus.COMPLETED
    for name in SurfaceName:
        assert job.surfaces[name].status == SurfaceStatus.OK


def test_partial_success_counts_as_completed(db_path):
    """ok_count >= 1 rule: Suggest ok + render captcha → still completed."""
    engine = AnalysisEngine(
        render_thread=FakeRenderThread(html=BlockedByCaptchaError("captcha")),
        parse_fn=lambda html, locale: _ok_parsed(),
        db_path=db_path,
        fetch_fn=lambda q, l, c: _ok_suggest(q),
    )
    job_id = engine.submit("coffee", "en", "us")
    events = _drain(engine)
    assert events[-1].kind == "complete"
    assert events[-1].status == "completed"

    job = get_job(job_id, db_path=db_path)
    assert job.surfaces[SurfaceName.SUGGEST].status == SurfaceStatus.OK
    assert job.surfaces[SurfaceName.PAA].status == SurfaceStatus.FAILED
    assert job.surfaces[SurfaceName.PAA].failure_category == FailureCategory.BLOCKED_BY_CAPTCHA
    assert job.surfaces[SurfaceName.RELATED].failure_category == FailureCategory.BLOCKED_BY_CAPTCHA
    assert job.status == JobStatus.COMPLETED


def test_all_failed_job_marks_failed(db_path):
    engine = AnalysisEngine(
        render_thread=FakeRenderThread(html=BlockedRateLimitError("rate")),
        parse_fn=lambda html, locale: _ok_parsed(),
        db_path=db_path,
        fetch_fn=lambda q, l, c: _failed_suggest(),
    )
    job_id = engine.submit("coffee", "en", "us")
    events = _drain(engine)
    assert events[-1].kind == "complete"
    assert events[-1].status == "failed"
    assert get_job(job_id, db_path=db_path).status == JobStatus.FAILED


# --- exception → category mapping --------------------------------------------


@pytest.mark.parametrize(
    "exc, expected",
    [
        (BlockedByCaptchaError("x"), FailureCategory.BLOCKED_BY_CAPTCHA),
        (BlockedByConsentError("x"), FailureCategory.BLOCKED_BY_CONSENT),
        (BlockedRateLimitError("x"), FailureCategory.BLOCKED_RATE_LIMIT),
        (BrowserCrashError("x"), FailureCategory.BROWSER_CRASH),
    ],
)
def test_render_exception_maps_to_failure_category(db_path, exc, expected):
    engine = AnalysisEngine(
        render_thread=FakeRenderThread(html=exc),
        parse_fn=lambda html, locale: _ok_parsed(),
        db_path=db_path,
        fetch_fn=lambda q, l, c: _ok_suggest(q),
    )
    job_id = engine.submit("coffee", "en", "us")
    _drain(engine)
    job = get_job(job_id, db_path=db_path)
    for name in (SurfaceName.PAA, SurfaceName.RELATED):
        assert job.surfaces[name].failure_category == expected


def test_render_timeout_maps_to_network_error(db_path):
    class HangingRender(FakeRenderThread):
        def submit(self, url):
            fut = Future()
            # never completes; engine times out
            return fut

    engine = AnalysisEngine(
        render_thread=HangingRender(),
        parse_fn=lambda html, locale: _ok_parsed(),
        db_path=db_path,
        fetch_fn=lambda q, l, c: _ok_suggest(q),
        render_timeout=0.1,
    )
    job_id = engine.submit("coffee", "en", "us")
    _drain(engine, timeout=3.0)
    job = get_job(job_id, db_path=db_path)
    assert job.surfaces[SurfaceName.PAA].failure_category == FailureCategory.NETWORK_ERROR
    assert job.surfaces[SurfaceName.RELATED].failure_category == FailureCategory.NETWORK_ERROR


def test_parser_exception_flagged_as_selector_not_found(db_path):
    def broken_parse(html, locale):
        raise RuntimeError("unexpected DOM shape")

    engine = AnalysisEngine(
        render_thread=FakeRenderThread(html="<html/>"),
        parse_fn=broken_parse,
        db_path=db_path,
        fetch_fn=lambda q, l, c: _ok_suggest(q),
    )
    job_id = engine.submit("coffee", "en", "us")
    _drain(engine)
    job = get_job(job_id, db_path=db_path)
    assert job.surfaces[SurfaceName.PAA].failure_category == FailureCategory.SELECTOR_NOT_FOUND
    assert job.surfaces[SurfaceName.RELATED].failure_category == FailureCategory.SELECTOR_NOT_FOUND


def test_parser_returns_none_for_surface_flagged_selector_not_found(db_path):
    """If parser dict is missing a surface key, engine treats it as failed."""

    def partial_parse(html, locale):
        return {SurfaceName.PAA: ParseResult(status=SurfaceStatus.OK, items=[])}

    engine = AnalysisEngine(
        render_thread=FakeRenderThread(html="<html/>"),
        parse_fn=partial_parse,
        db_path=db_path,
        fetch_fn=lambda q, l, c: _ok_suggest(q),
    )
    job_id = engine.submit("coffee", "en", "us")
    _drain(engine)
    job = get_job(job_id, db_path=db_path)
    assert job.surfaces[SurfaceName.RELATED].failure_category == FailureCategory.SELECTOR_NOT_FOUND


# --- retry -------------------------------------------------------------------


def test_retry_only_reruns_failed_surfaces(db_path):
    """After initial run (Suggest ok, render captcha), retry with render ok →
    PAA/Related become ok while Suggest stays ok (fetch_fn not called again)."""

    suggest_calls = []

    def fetch(q, l, c):
        suggest_calls.append((q, l, c))
        return _ok_suggest(q)

    captcha_render = FakeRenderThread(html=BlockedByCaptchaError("captcha"))
    engine = AnalysisEngine(
        render_thread=captcha_render,
        parse_fn=lambda html, locale: _ok_parsed(),
        db_path=db_path,
        fetch_fn=fetch,
    )
    job_id = engine.submit("coffee", "en", "us")
    _drain(engine)
    assert len(suggest_calls) == 1

    # Swap render to success and retry.
    engine._render_thread = FakeRenderThread(html="<html/>")
    engine.retry_failed_surfaces(job_id)
    _drain(engine)

    # Suggest was NOT re-fetched
    assert len(suggest_calls) == 1
    job = get_job(job_id, db_path=db_path)
    assert job.surfaces[SurfaceName.SUGGEST].status == SurfaceStatus.OK
    assert job.surfaces[SurfaceName.PAA].status == SurfaceStatus.OK
    assert job.surfaces[SurfaceName.RELATED].status == SurfaceStatus.OK


def test_retry_noop_when_all_ok(db_path):
    engine = AnalysisEngine(
        render_thread=FakeRenderThread(),
        parse_fn=lambda html, locale: _ok_parsed(),
        db_path=db_path,
        fetch_fn=lambda q, l, c: _ok_suggest(q),
    )
    job_id = engine.submit("coffee", "en", "us")
    _drain(engine)

    suggest_calls_before = [
        e for e in iter(lambda: engine.progress_queue.get_nowait() if not engine.progress_queue.empty() else None, None)
    ]
    # Retry on an all-ok job must NOT spawn a new worker
    engine.retry_failed_surfaces(job_id)
    time.sleep(0.1)
    assert engine.progress_queue.empty()


def test_retry_preserves_ok_on_retry_render(db_path):
    """Retry with render that returns ok parser but Suggest was previously ok:
    Suggest must not be clobbered even if some parse result arrives for it."""
    calls = {"fetch": 0}

    def fetch(q, l, c):
        calls["fetch"] += 1
        if calls["fetch"] == 1:
            return _ok_suggest(q)
        return _failed_suggest()  # on retry would fail — but must not be called

    engine = AnalysisEngine(
        render_thread=FakeRenderThread(html=BlockedByCaptchaError("x")),
        parse_fn=lambda html, locale: _ok_parsed(),
        db_path=db_path,
        fetch_fn=fetch,
    )
    job_id = engine.submit("coffee", "en", "us")
    _drain(engine)
    assert calls["fetch"] == 1

    engine._render_thread = FakeRenderThread(html="<html/>")
    engine.retry_failed_surfaces(job_id)
    _drain(engine)
    # fetch should not have been called again
    assert calls["fetch"] == 1


# --- progress ordering -------------------------------------------------------


def test_progress_events_emit_in_expected_order(db_path):
    engine = AnalysisEngine(
        render_thread=FakeRenderThread(),
        parse_fn=lambda html, locale: _ok_parsed(),
        db_path=db_path,
        fetch_fn=lambda q, l, c: _ok_suggest(q),
    )
    engine.submit("coffee", "en", "us")
    events = _drain(engine)
    kinds = [e.kind for e in events]
    assert kinds == ["start", "suggest", "paa", "related", "complete"]


# --- concurrency -------------------------------------------------------------


def test_two_concurrent_submits_dont_interfere(db_path):
    engine = AnalysisEngine(
        render_thread=FakeRenderThread(),
        parse_fn=lambda html, locale: _ok_parsed(),
        db_path=db_path,
        fetch_fn=lambda q, l, c: _ok_suggest(q),
    )
    id_a = engine.submit("coffee", "en", "us")
    id_b = engine.submit("tea", "en", "us")

    # Drain for up to 3s; we expect 2 complete events total
    deadline = time.monotonic() + 3.0
    completes = {}
    while time.monotonic() < deadline and len(completes) < 2:
        try:
            ev = engine.progress_queue.get(timeout=0.05)
        except Exception:
            continue
        if ev.kind == "complete":
            completes[ev.job_id] = ev
    assert id_a in completes and id_b in completes

    assert get_job(id_a, db_path=db_path).status == JobStatus.COMPLETED
    assert get_job(id_b, db_path=db_path).status == JobStatus.COMPLETED


# --- url builder -------------------------------------------------------------


def test_build_serp_url_quotes_spaces_and_specials():
    url = _build_serp_url("best running shoes", "en", "us")
    assert "q=best+running+shoes" in url
    assert "hl=en" in url
    assert "gl=us" in url


def test_build_serp_url_quotes_unicode():
    url = _build_serp_url("跑步鞋推荐", "zh-CN", "cn")
    # Must be fully percent-encoded
    assert "q=%E8%B7%91" in url
    assert "hl=zh-CN" in url


# --- unhandled exception safety ---------------------------------------------


def test_unhandled_exception_leaves_job_in_terminal_state(db_path):
    """If engine worker blows up, all running surfaces get flagged and job completes."""

    def bomb(q, l, c):
        raise RuntimeError("unexpected boom")

    engine = AnalysisEngine(
        render_thread=FakeRenderThread(),
        parse_fn=lambda html, locale: _ok_parsed(),
        db_path=db_path,
        fetch_fn=bomb,
    )
    job_id = engine.submit("coffee", "en", "us")
    events = _drain(engine)
    assert events[-1].kind == "error"
    job = get_job(job_id, db_path=db_path)
    assert job.status != JobStatus.RUNNING
    for name in SurfaceName:
        # all surfaces should be in a terminal state
        assert job.surfaces[name].status != SurfaceStatus.RUNNING
