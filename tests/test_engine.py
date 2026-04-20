"""Unit 3: AnalysisEngine orchestration, retry, progress events, error paths.

After plan 003 the engine pairs Suggest (suggestqueries.google.com) with
SerpAPI (PAA + Related in one ``engine=google`` call). Fake callables inject
the provider behavior; the real RenderThread-based tests were replaced when
the Playwright path was retired.
"""

from __future__ import annotations

import time

import pytest

from seoserper.core.engine import AnalysisEngine, ProgressEvent
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


@pytest.fixture(autouse=True)
def _full_mode(monkeypatch):
    """Default to full mode (SERPAPI_KEY set) for top-level tests. The
    SuggestOnlyMode / Adv1 nested classes monkeypatch this back to None.
    """
    from seoserper import config
    monkeypatch.setattr(config, "SERPAPI_KEY", "fake-key")


# --- fakes -------------------------------------------------------------------


def _ok_suggest(query: str = "coffee") -> SuggestResult:
    return SuggestResult(
        status=SurfaceStatus.OK,
        items=[
            Suggestion(text=query, rank=1),
            Suggestion(text=f"{query} shop", rank=2),
        ],
    )


def _failed_suggest(category: FailureCategory = FailureCategory.NETWORK_ERROR) -> SuggestResult:
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


def _failed_parsed(category: FailureCategory) -> dict[SurfaceName, ParseResult]:
    return {
        SurfaceName.PAA: ParseResult(status=SurfaceStatus.FAILED, failure_category=category),
        SurfaceName.RELATED: ParseResult(status=SurfaceStatus.FAILED, failure_category=category),
    }


def _drain(engine: AnalysisEngine, expect_complete: bool = True, timeout: float = 2.0
           ) -> list[ProgressEvent]:
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
        serp_fn=lambda q, l, c, *, engine="google": _ok_parsed(),
        db_path=db_path,
        fetch_fn=lambda q, l, c: _ok_suggest(q),
    )
    job_id = engine.submit("coffee", "en", "us")
    assert isinstance(job_id, int) and job_id > 0

    events = _drain(engine)
    kinds = [e.kind for e in events]
    # Post Unit A (parallel dispatch): start + complete bookend, but suggest
    # / paa / related may arrive in any order between them.
    assert kinds[0] == "start"
    assert kinds[-1] == "complete"
    assert set(kinds[1:-1]) == {"suggest", "paa", "related"}

    job = get_job(job_id, db_path=db_path)
    assert job.status == JobStatus.COMPLETED
    assert job.render_mode == "full"
    for name in SurfaceName:
        assert job.surfaces[name].status == SurfaceStatus.OK


def test_partial_success_counts_as_completed(db_path):
    """ok_count >= 1 rule: Suggest ok + SerpAPI rate-limited → still completed."""
    engine = AnalysisEngine(
        serp_fn=lambda q, l, c, *, engine="google": _failed_parsed(FailureCategory.BLOCKED_RATE_LIMIT),
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
    assert job.surfaces[SurfaceName.PAA].failure_category == FailureCategory.BLOCKED_RATE_LIMIT
    assert job.surfaces[SurfaceName.RELATED].failure_category == FailureCategory.BLOCKED_RATE_LIMIT
    assert job.status == JobStatus.COMPLETED


def test_all_failed_job_marks_failed(db_path):
    engine = AnalysisEngine(
        serp_fn=lambda q, l, c, *, engine="google": _failed_parsed(FailureCategory.NETWORK_ERROR),
        db_path=db_path,
        fetch_fn=lambda q, l, c: _failed_suggest(),
    )
    job_id = engine.submit("coffee", "en", "us")
    events = _drain(engine)
    assert events[-1].kind == "complete"
    assert events[-1].status == "failed"
    assert get_job(job_id, db_path=db_path).status == JobStatus.FAILED


# --- SerpAPI failure-category pass-through -----------------------------------


@pytest.mark.parametrize(
    "category",
    [
        FailureCategory.BLOCKED_RATE_LIMIT,
        FailureCategory.NETWORK_ERROR,
        FailureCategory.SELECTOR_NOT_FOUND,
    ],
)
def test_serp_failure_categories_round_trip(db_path, category):
    engine = AnalysisEngine(
        serp_fn=lambda q, l, c, *, engine="google": _failed_parsed(category),
        db_path=db_path,
        fetch_fn=lambda q, l, c: _ok_suggest(q),
    )
    job_id = engine.submit("coffee", "en", "us")
    _drain(engine)
    job = get_job(job_id, db_path=db_path)
    for name in (SurfaceName.PAA, SurfaceName.RELATED):
        assert job.surfaces[name].failure_category == category


def test_serp_fn_exception_flags_network_error(db_path):
    """Defensive path: an unexpected bug inside the fetcher must not leak."""

    def bomb(q, l, c, *, engine="google"):
        raise RuntimeError("unexpected fetcher bug")

    engine = AnalysisEngine(
        serp_fn=bomb,
        db_path=db_path,
        fetch_fn=lambda q, l, c: _ok_suggest(q),
    )
    job_id = engine.submit("coffee", "en", "us")
    events = _drain(engine)
    assert events[-1].kind == "complete"
    job = get_job(job_id, db_path=db_path)
    assert job.surfaces[SurfaceName.PAA].failure_category == FailureCategory.NETWORK_ERROR
    assert job.surfaces[SurfaceName.RELATED].failure_category == FailureCategory.NETWORK_ERROR


def test_serp_fn_missing_surface_key_flags_selector_not_found(db_path):
    """If serp_fn dict is missing a surface key, engine treats it as SELECTOR_NOT_FOUND."""

    def partial(q, l, c, *, engine="google"):
        return {SurfaceName.PAA: ParseResult(status=SurfaceStatus.OK, items=[])}

    engine = AnalysisEngine(
        serp_fn=partial,
        db_path=db_path,
        fetch_fn=lambda q, l, c: _ok_suggest(q),
    )
    job_id = engine.submit("coffee", "en", "us")
    _drain(engine)
    job = get_job(job_id, db_path=db_path)
    assert job.surfaces[SurfaceName.PAA].status == SurfaceStatus.OK
    assert job.surfaces[SurfaceName.RELATED].failure_category == FailureCategory.SELECTOR_NOT_FOUND


# --- retry -------------------------------------------------------------------


def test_retry_only_reruns_failed_surfaces(db_path):
    """After initial run (Suggest ok, SerpAPI fails), retry with serp ok →
    PAA/Related become ok while Suggest stays ok (fetch_fn not called again)."""

    suggest_calls: list[tuple[str, str, str]] = []

    def fetch(q, l, c):
        suggest_calls.append((q, l, c))
        return _ok_suggest(q)

    serp_state = {"fn": lambda q, l, c, *, engine="google": _failed_parsed(FailureCategory.BLOCKED_RATE_LIMIT)}

    def serp(q, l, c, *, engine="google"):
        return serp_state["fn"](q, l, c)

    engine = AnalysisEngine(
        serp_fn=serp,
        db_path=db_path,
        fetch_fn=fetch,
    )
    job_id = engine.submit("coffee", "en", "us")
    _drain(engine)
    assert len(suggest_calls) == 1

    # Swap serp to success and retry.
    serp_state["fn"] = lambda q, l, c, *, engine="google": _ok_parsed()
    engine.retry_failed_surfaces(job_id)
    _drain(engine)

    assert len(suggest_calls) == 1  # Suggest not re-fetched
    job = get_job(job_id, db_path=db_path)
    assert job.surfaces[SurfaceName.SUGGEST].status == SurfaceStatus.OK
    assert job.surfaces[SurfaceName.PAA].status == SurfaceStatus.OK
    assert job.surfaces[SurfaceName.RELATED].status == SurfaceStatus.OK


def test_retry_noop_when_all_ok(db_path):
    engine = AnalysisEngine(
        serp_fn=lambda q, l, c, *, engine="google": _ok_parsed(),
        db_path=db_path,
        fetch_fn=lambda q, l, c: _ok_suggest(q),
    )
    job_id = engine.submit("coffee", "en", "us")
    _drain(engine)

    # Retry on an all-ok job must NOT spawn a new worker.
    engine.retry_failed_surfaces(job_id)
    time.sleep(0.1)
    assert engine.progress_queue.empty()


def test_retry_preserves_ok_on_retry_serp(db_path):
    """Suggest ok + SerpAPI fail initially; retry with serp ok and a fetch_fn
    that would fail if called again — Suggest must not be re-called."""
    calls = {"fetch": 0}

    def fetch(q, l, c):
        calls["fetch"] += 1
        if calls["fetch"] == 1:
            return _ok_suggest(q)
        return _failed_suggest()

    serp_state = {"fn": lambda q, l, c, *, engine="google": _failed_parsed(FailureCategory.BLOCKED_RATE_LIMIT)}

    def serp(q, l, c, *, engine="google"):
        return serp_state["fn"](q, l, c)

    engine = AnalysisEngine(
        serp_fn=serp,
        db_path=db_path,
        fetch_fn=fetch,
    )
    job_id = engine.submit("coffee", "en", "us")
    _drain(engine)
    assert calls["fetch"] == 1

    serp_state["fn"] = lambda q, l, c, *, engine="google": _ok_parsed()
    engine.retry_failed_surfaces(job_id)
    _drain(engine)
    assert calls["fetch"] == 1  # fetch_fn NOT re-called


# --- progress ordering -------------------------------------------------------


def test_progress_events_cover_all_surfaces(db_path):
    """Parallel dispatch: start + complete bookend; suggest/paa/related
    emit in any order between them."""
    engine = AnalysisEngine(
        serp_fn=lambda q, l, c, *, engine="google": _ok_parsed(),
        db_path=db_path,
        fetch_fn=lambda q, l, c: _ok_suggest(q),
    )
    engine.submit("coffee", "en", "us")
    events = _drain(engine)
    kinds = [e.kind for e in events]
    assert kinds[0] == "start"
    assert kinds[-1] == "complete"
    middle = set(kinds[1:-1])
    assert middle == {"suggest", "paa", "related"}


def test_parallel_dispatch_wall_clock_is_not_sum(db_path):
    """Suggest sleeps 0.3s, SerpAPI sleeps 0.3s — parallel total < 0.55s
    (would be ~0.6s+ if serial)."""
    import time as _time

    def slow_suggest(q, l, c):
        _time.sleep(0.3)
        return _ok_suggest(q)

    def slow_serp(q, l, c):
        _time.sleep(0.3)
        return _ok_parsed()

    engine = AnalysisEngine(
        serp_fn=slow_serp,
        db_path=db_path,
        fetch_fn=slow_suggest,
    )
    start = _time.monotonic()
    engine.submit("coffee", "en", "us")
    _drain(engine, timeout=3.0)
    elapsed = _time.monotonic() - start
    assert elapsed < 0.55, (
        f"wall-clock {elapsed:.2f}s suggests serial execution "
        f"(expected < 0.55s for two 0.3s sleeps in parallel)"
    )


# --- concurrency -------------------------------------------------------------


def test_two_concurrent_submits_dont_interfere(db_path):
    engine = AnalysisEngine(
        serp_fn=lambda q, l, c, *, engine="google": _ok_parsed(),
        db_path=db_path,
        fetch_fn=lambda q, l, c: _ok_suggest(q),
    )
    id_a = engine.submit("coffee", "en", "us")
    id_b = engine.submit("tea", "en", "us")

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


# --- unhandled exception safety ---------------------------------------------


def test_unhandled_exception_leaves_job_in_terminal_state(db_path):
    def bomb(q, l, c, *, engine="google"):
        raise RuntimeError("unexpected boom")

    engine = AnalysisEngine(
        serp_fn=lambda q, l, c, *, engine="google": _ok_parsed(),
        db_path=db_path,
        fetch_fn=bomb,
    )
    job_id = engine.submit("coffee", "en", "us")
    events = _drain(engine)
    assert events[-1].kind == "error"
    job = get_job(job_id, db_path=db_path)
    assert job.status != JobStatus.RUNNING
    for name in SurfaceName:
        assert job.surfaces[name].status != SurfaceStatus.RUNNING


# --- Suggest-only mode (SERPAPI_KEY unset) -----------------------------------


class TestSuggestOnlyMode:
    """SERPAPI_KEY=None: engine skips SerpAPI, writes only Suggest row."""

    @pytest.fixture(autouse=True)
    def _no_key(self, monkeypatch):
        from seoserper import config
        monkeypatch.setattr(config, "SERPAPI_KEY", None)

    def test_submit_creates_suggest_only_job(self, db_path):
        engine = AnalysisEngine(
            serp_fn=None,
            db_path=db_path,
            fetch_fn=lambda q, l, c: _ok_suggest(q),
        )
        job_id = engine.submit("coffee", "en", "us")
        events = _drain(engine)

        job = get_job(job_id, db_path=db_path)
        assert job.render_mode == "suggest-only"
        assert list(job.surfaces.keys()) == [SurfaceName.SUGGEST]
        assert job.surfaces[SurfaceName.SUGGEST].status == SurfaceStatus.OK
        assert job.status == JobStatus.COMPLETED

        kinds = [e.kind for e in events]
        assert kinds == ["start", "suggest", "complete"]

    def test_submit_does_not_call_serp_fn(self, db_path):
        calls: list[tuple[str, str, str]] = []

        def tracking_serp(q, l, c):
            calls.append((q, l, c))
            raise AssertionError("serp_fn must not be called in suggest-only mode")

        engine = AnalysisEngine(
            serp_fn=tracking_serp,
            db_path=db_path,
            fetch_fn=lambda q, l, c: _ok_suggest(q),
        )
        engine.submit("coffee", "en", "us")
        _drain(engine)
        assert calls == []

    def test_suggest_failed_yields_job_failed(self, db_path):
        engine = AnalysisEngine(
            serp_fn=None,
            db_path=db_path,
            fetch_fn=lambda q, l, c: _failed_suggest(FailureCategory.NETWORK_ERROR),
        )
        job_id = engine.submit("coffee", "en", "us")
        events = _drain(engine)
        job = get_job(job_id, db_path=db_path)
        assert job.status == JobStatus.FAILED
        assert events[-1].status == "failed"

    def test_retry_on_suggest_only_does_not_invoke_serp(self, db_path):
        call_count = {"fetch": 0}

        def flaky_fetch(q, l, c):
            call_count["fetch"] += 1
            if call_count["fetch"] == 1:
                return _failed_suggest(FailureCategory.NETWORK_ERROR)
            return _ok_suggest(q)

        engine = AnalysisEngine(
            serp_fn=None,
            db_path=db_path,
            fetch_fn=flaky_fetch,
        )
        jid = engine.submit("coffee", "en", "us")
        _drain(engine)
        assert get_job(jid, db_path=db_path).status == JobStatus.FAILED

        engine.retry_failed_surfaces(jid)
        _drain(engine)

        job = get_job(jid, db_path=db_path)
        assert job.status == JobStatus.COMPLETED
        assert call_count["fetch"] == 2


class TestAdv1HistoricalRetryGuard:
    """Full-mode job retried under SERPAPI_KEY=None must not invoke serp_fn."""

    def test_coerces_to_suggest_only_retry(self, db_path, monkeypatch):
        from seoserper import config

        # Step 1: create a full-mode job with SERPAPI_KEY set, Suggest ok +
        # PAA/Related failed (mimics a pre-key-rotation failed attempt).
        monkeypatch.setattr(config, "SERPAPI_KEY", "fake-key")
        engine_full = AnalysisEngine(
            serp_fn=lambda q, l, c, *, engine="google": _failed_parsed(FailureCategory.BLOCKED_RATE_LIMIT),
            db_path=db_path,
            fetch_fn=lambda q, l, c: _ok_suggest(q),
        )
        jid = engine_full.submit("coffee", "en", "us")
        _drain(engine_full)
        job = get_job(jid, db_path=db_path)
        assert job.render_mode == "full"
        assert job.surfaces[SurfaceName.PAA].status == SurfaceStatus.FAILED
        assert job.surfaces[SurfaceName.RELATED].status == SurfaceStatus.FAILED
        assert job.surfaces[SurfaceName.SUGGEST].status == SurfaceStatus.OK

        # Step 2: unset SERPAPI_KEY and instantiate a new engine with
        # serp_fn=None. Retry must NOT attempt SerpAPI on the historical job.
        monkeypatch.setattr(config, "SERPAPI_KEY", None)
        engine_nokey = AnalysisEngine(
            serp_fn=None,
            db_path=db_path,
            fetch_fn=lambda q, l, c: _ok_suggest(q),
        )

        engine_nokey.retry_failed_surfaces(jid)
        time.sleep(0.1)
        assert engine_nokey.progress_queue.empty()

        # No AttributeError / crash from calling None serp_fn.
        job_after = get_job(jid, db_path=db_path)
        assert job_after.render_mode == "full"  # stored value unchanged
        assert job_after.surfaces[SurfaceName.PAA].status == SurfaceStatus.FAILED
        assert job_after.surfaces[SurfaceName.RELATED].status == SurfaceStatus.FAILED

    def test_retries_suggest_when_suggest_also_failed(self, db_path, monkeypatch):
        from seoserper import config

        monkeypatch.setattr(config, "SERPAPI_KEY", "fake-key")
        engine_full = AnalysisEngine(
            serp_fn=lambda q, l, c, *, engine="google": _failed_parsed(FailureCategory.BLOCKED_RATE_LIMIT),
            db_path=db_path,
            fetch_fn=lambda q, l, c: _failed_suggest(FailureCategory.NETWORK_ERROR),
        )
        jid = engine_full.submit("coffee", "en", "us")
        _drain(engine_full)

        monkeypatch.setattr(config, "SERPAPI_KEY", None)
        engine_nokey = AnalysisEngine(
            serp_fn=None,
            db_path=db_path,
            fetch_fn=lambda q, l, c: _ok_suggest(q),
        )
        engine_nokey.retry_failed_surfaces(jid)
        _drain(engine_nokey)

        job = get_job(jid, db_path=db_path)
        assert job.surfaces[SurfaceName.SUGGEST].status == SurfaceStatus.OK
        # PAA/Related untouched
        assert job.surfaces[SurfaceName.PAA].status == SurfaceStatus.FAILED
        assert job.surfaces[SurfaceName.RELATED].status == SurfaceStatus.FAILED


class TestEngineOptionalSerpFn:
    """serp_fn is Optional; engine must accept None when only Suggest runs."""

    def test_accepts_none_serp_fn(self, db_path, monkeypatch):
        from seoserper import config
        monkeypatch.setattr(config, "SERPAPI_KEY", None)
        engine = AnalysisEngine(
            serp_fn=None,
            db_path=db_path,
            fetch_fn=lambda q, l, c: _ok_suggest(q),
        )
        assert engine._serp_fn is None
        engine.submit("coffee", "en", "us")
        _drain(engine)


# --- Bing engine (plan 005 Unit 4) -------------------------------------------


class TestBingEngine:
    """engine='bing' creates 2-surface jobs (PAA + RELATED, no SUGGEST)."""

    def test_submit_bing_creates_two_surface_job(self, db_path):
        calls = []
        def serp(q, l, c, *, engine):
            calls.append(engine)
            return _ok_parsed()

        engine = AnalysisEngine(
            serp_fn=serp,
            db_path=db_path,
            fetch_fn=lambda q, l, c: _ok_suggest(q),
        )
        jid = engine.submit("coffee", "en", "us", engine="bing")
        _drain(engine)

        job = get_job(jid, db_path=db_path)
        assert job.engine == "bing"
        assert set(job.surfaces.keys()) == {SurfaceName.PAA, SurfaceName.RELATED}
        assert job.surfaces[SurfaceName.PAA].status == SurfaceStatus.OK
        assert job.surfaces[SurfaceName.RELATED].status == SurfaceStatus.OK
        # serp_fn received engine=bing
        assert calls == ["bing"]

    def test_submit_bing_does_not_invoke_suggest_fetcher(self, db_path):
        suggest_calls = []
        def fetch(q, l, c):
            suggest_calls.append(q)
            return _ok_suggest(q)

        engine = AnalysisEngine(
            serp_fn=lambda q, l, c, *, engine="google": _ok_parsed(),
            db_path=db_path,
            fetch_fn=fetch,
        )
        engine.submit("coffee", "en", "us", engine="bing")
        _drain(engine)
        assert suggest_calls == []  # Bing skips Suggest entirely

    def test_bing_retry_preserves_engine(self, db_path):
        """Bing job with a failed Related → retry should re-call serp_fn
        with engine='bing' (stored engine, not live arg)."""
        # First call fails, second call (retry) succeeds.
        state = {"calls": [], "fail_count": 1}
        def flaky_serp(q, l, c, *, engine):
            state["calls"].append(engine)
            if state["fail_count"] > 0:
                state["fail_count"] -= 1
                return _failed_parsed(FailureCategory.BLOCKED_RATE_LIMIT)
            return _ok_parsed()

        engine = AnalysisEngine(
            serp_fn=flaky_serp,
            db_path=db_path,
            fetch_fn=lambda q, l, c: _ok_suggest(q),
        )
        jid = engine.submit("coffee", "en", "us", engine="bing")
        _drain(engine)
        assert state["calls"] == ["bing"]
        assert get_job(jid, db_path=db_path).surfaces[SurfaceName.PAA].status == SurfaceStatus.FAILED

        # Retry — should pass engine='bing' again (from stored row, not args)
        engine.retry_failed_surfaces(jid)
        _drain(engine)
        assert state["calls"] == ["bing", "bing"]
        assert get_job(jid, db_path=db_path).surfaces[SurfaceName.PAA].status == SurfaceStatus.OK

    def test_google_and_bing_concurrent_dont_cross_contaminate(self, db_path):
        """Submit a Google job + a Bing job back to back; each job's engine
        is correctly passed to its own serp_fn invocation."""
        calls_by_query = {}
        def serp(q, l, c, *, engine):
            calls_by_query.setdefault(q, []).append(engine)
            return _ok_parsed()

        engine = AnalysisEngine(
            serp_fn=serp,
            db_path=db_path,
            fetch_fn=lambda q, l, c: _ok_suggest(q),
        )
        id_g = engine.submit("coffee", "en", "us")  # google default
        id_b = engine.submit("tea", "en", "us", engine="bing")

        import time as _time
        deadline = _time.monotonic() + 3.0
        completes = set()
        while _time.monotonic() < deadline and len(completes) < 2:
            try:
                ev = engine.progress_queue.get(timeout=0.05)
            except Exception:
                continue
            if ev.kind == "complete":
                completes.add(ev.job_id)
        assert {id_g, id_b}.issubset(completes)

        assert calls_by_query["coffee"] == ["google"]
        assert calls_by_query["tea"] == ["bing"]


class TestDefaultFetchFn:
    """Plan 007 Unit 5: engine's default fetch_fn wraps get_suggestions with retry=False.

    Load-bearing architectural decision: without the pin, retry_failed_surfaces
    compounds on top of the library's 1-retry, inflating upstream hits per
    failing surface from <=2 to <=4 across Submit + operator retry.
    """

    def test_default_fetch_fn_is_engine_wrapper(self, db_path):
        from seoserper.core.engine import AnalysisEngine, _engine_suggest_fn
        eng = AnalysisEngine(db_path=db_path, serp_fn=lambda q, l, c: {})
        assert eng._fetch_fn is _engine_suggest_fn

    def test_engine_wrapper_pins_retry_false(self, db_path, monkeypatch):
        """Verify _engine_suggest_fn calls get_suggestions with retry=False.

        Monkeypatches the library target to capture kwargs — proves the pin
        is wired, not just documented.
        """
        captured: dict = {}

        def fake_get_suggestions(q, hl, gl, **kwargs):
            captured.update(kwargs)
            return SuggestResult(status=SurfaceStatus.OK, items=[])

        monkeypatch.setattr(
            "seoserper.core.engine.get_suggestions", fake_get_suggestions
        )

        from seoserper.core.engine import _engine_suggest_fn
        _engine_suggest_fn("coffee", "en", "us")
        assert captured.get("retry") is False, (
            "engine must pin retry=False to prevent compounded retries; "
            f"got kwargs={captured}"
        )
