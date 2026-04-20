"""Analysis engine — orchestrates Suggest HTTP + SerpAPI PAA/Related + storage.

The engine receives a keyword + locale from the UI and coordinates the three
surfaces. Each surface is persisted independently so the UI can reveal
partial results as they arrive.

Full mode (``config.SERPAPI_KEY`` is set) runs Suggest (free endpoint) plus
SerpAPI (PAA + Related returned in a single ``engine=google`` call).
Suggest-only mode (``SERPAPI_KEY`` unset) skips the SerpAPI path entirely
and writes a single surface row.

Progressive reveal:
    engine.progress_queue is a single ``queue.Queue`` the UI drains on each
    ``st.rerun`` tick. Events are one-way (engine → UI).

Retry:
    ``retry_failed_surfaces(job_id)`` reads the current job state and only
    re-runs the surfaces whose status != ok (plan R8). If Suggest is already
    ok we won't re-hit the suggestqueries endpoint; if PAA is failed we
    re-run the full SerpAPI call but R8 preserves any ok surface on return.

ADV-1 guard:
    A historical full-mode job retried under ``SERPAPI_KEY=None`` coerces
    to suggest-only retry semantics. This prevents the engine from invoking
    ``.submit()`` on a ``None`` serp_fn and from calling a missing provider.
"""

from __future__ import annotations

import concurrent.futures
import logging
import queue
import threading
from dataclasses import dataclass
from typing import Callable

from seoserper import config
from seoserper.fetchers.suggest import SuggestResult
from seoserper.suggest import get_suggestions
from seoserper.models import (
    FailureCategory,
    JobStatus,
    ParseResult,
    SurfaceName,
    SurfaceStatus,
)
from seoserper.storage import (
    complete_job,
    create_job,
    get_connection,
    get_job,
    update_surface,
)

logger = logging.getLogger(__name__)

SerpFn = Callable[[str, str, str], dict[SurfaceName, ParseResult]]
FetchFn = Callable[[str, str, str], SuggestResult]


def _engine_suggest_fn(query: str, lang: str, country: str) -> SuggestResult:
    """Engine-context wrapper over ``seoserper.suggest.get_suggestions``.

    Pins ``retry=False`` so the library's internal transient retry is
    disabled here — ``AnalysisEngine.retry_failed_surfaces`` is the sole
    retry layer under engine context. Without this pin a single Submit +
    one operator retry would compound to up to 4 upstream hits per failing
    surface (plan 007 Key Decision).
    """
    return get_suggestions(query, lang, country, retry=False)


@dataclass
class ProgressEvent:
    """Single message in the engine → UI queue."""

    job_id: int
    kind: str  # start / suggest / paa / related / complete / error
    status: str = ""  # enum value (ok/failed/empty/running/completed/...)
    message: str = ""


class AnalysisEngine:
    def __init__(
        self,
        *,
        serp_fn: SerpFn | None = None,
        db_path: str | None = None,
        fetch_fn: FetchFn = _engine_suggest_fn,
    ):
        # serp_fn is Optional: under SERPAPI_KEY=None the engine never enters
        # _do_serp, so the fetcher is unnecessary. The invariant
        # "run_serp=True → serp_fn is not None" is asserted in _do_serp to
        # keep the contract machine-checked rather than convention.
        self._serp_fn = serp_fn
        self._db_path = db_path
        self._fetch_fn = fetch_fn
        self.progress_queue: queue.Queue[ProgressEvent] = queue.Queue()
        # Captures the library-layer metadata from the most recent _do_suggest
        # call (provider_used, from_cache, latency_ms). UI reads this to
        # render a "cache hit · 12ms" badge so the library's cache behavior
        # is visible to the operator. Keys:
        #   provider_used: "cache" | "google" | "static" | "none" | ""
        #   from_cache: bool
        #   latency_ms: int
        self.last_suggest_meta: dict = {}

    # --- public API ---

    def submit(self, query: str, lang: str, country: str, *, engine: str = "google") -> int:
        """INSERT a new job and spawn a background worker. Returns job_id.

        Reads ``config.SERPAPI_KEY`` once at the top to decide whether to run
        full SERP fetching. Engine (``google`` | ``bing``) is orthogonal:
        - engine=google + key set     → Suggest + PAA + Related (3 surfaces)
        - engine=google + key unset   → Suggest-only (1 surface)
        - engine=bing   + key set     → PAA + Related via Bing (2 surfaces)
        - engine=bing   + key unset   → invalid (no free Bing autocomplete);
          treated as suggest-only + render_mode stays 'full' + serp skipped.
          The resulting 2-row Bing job completes with both surfaces FAILED
          (NETWORK_ERROR) when the worker runs — consistent with other
          SERPAPI_KEY-missing failure modes.
        """
        if engine == "bing":
            run_suggest = False
            run_serp = config.SERPAPI_KEY is not None
            render_mode = "full"  # semantic: Bing jobs never collapse to suggest-only
        else:
            render_mode = "full" if config.SERPAPI_KEY else "suggest-only"
            run_suggest = True
            run_serp = render_mode == "full"

        job_id = create_job(
            query, lang, country,
            db_path=self._db_path,
            render_mode=render_mode,
            engine=engine,
        )
        self._spawn_worker(
            job_id, query, lang, country,
            run_suggest=run_suggest, run_serp=run_serp, engine=engine,
        )
        return job_id

    def retry_failed_surfaces(self, job_id: int) -> None:
        """Re-run only the surfaces whose current status != ok.

        Retry semantics depend on the *stored* render_mode, not the live key
        state, so a historical suggest-only job stays suggest-only. ADV-1
        crash-path guard: if the job was created in full mode but the live
        SERPAPI_KEY is now None (no serp_fn available), coerce retry to
        suggest-only semantics — retry Suggest only, leave PAA/Related as-is.
        """
        job = get_job(job_id, db_path=self._db_path)
        if job is None:
            return

        run_suggest = (
            SurfaceName.SUGGEST in job.surfaces
            and job.surfaces[SurfaceName.SUGGEST].status != SurfaceStatus.OK
        )
        if job.render_mode == "suggest-only":
            run_serp = False
        elif config.SERPAPI_KEY is None:
            # ADV-1 guard: historical full-mode job, SerpAPI unavailable now.
            # Skip SerpAPI to avoid calling a missing serp_fn.
            run_serp = False
        else:
            run_serp = any(
                name in job.surfaces
                and job.surfaces[name].status != SurfaceStatus.OK
                for name in (SurfaceName.PAA, SurfaceName.RELATED)
            )

        if not (run_suggest or run_serp):
            return

        with get_connection(self._db_path) as conn:
            conn.execute(
                "UPDATE jobs SET status='running', overall_status='running', "
                "completed_at = NULL WHERE id = ?",
                (job_id,),
            )
        self._spawn_worker(
            job_id, job.query, job.language, job.country,
            run_suggest=run_suggest, run_serp=run_serp, engine=job.engine,
        )

    # --- internal ---

    def _spawn_worker(self, job_id: int, query: str, lang: str, country: str,
                      *, run_suggest: bool, run_serp: bool, engine: str = "google") -> None:
        thread = threading.Thread(
            target=self._run_analysis,
            args=(job_id, query, lang, country, run_suggest, run_serp, engine),
            name=f"seoserper-engine-{job_id}",
            daemon=True,
        )
        thread.start()

    def _run_analysis(self, job_id: int, query: str, lang: str, country: str,
                      run_suggest: bool, run_serp: bool, engine: str = "google") -> None:
        self._emit(job_id, "start")
        try:
            if run_suggest and run_serp:
                # Parallel dispatch — both providers are independent HTTPs.
                # SQLite writes from both threads serialize on the page lock
                # (WAL mode), which is fine for our two-thread contention.
                # Progress events may interleave; _drain_progress on the UI
                # side is tolerant of the reordering.
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=2, thread_name_prefix=f"engine-{job_id}"
                ) as executor:
                    f_suggest = executor.submit(
                        self._do_suggest, job_id, query, lang, country
                    )
                    f_serp = executor.submit(
                        self._do_serp, job_id, query, lang, country, engine
                    )
                    for fut in concurrent.futures.as_completed((f_suggest, f_serp)):
                        fut.result()
            elif run_suggest:
                self._do_suggest(job_id, query, lang, country)
            elif run_serp:
                self._do_serp(job_id, query, lang, country, engine)
            final = complete_job(job_id, db_path=self._db_path)
            self._emit(job_id, "complete", status=final.value)
        except BaseException as exc:
            # Safety net — engine must not leave a job in `running`.
            logger.exception("engine unhandled exception job=%d", job_id)
            self._mark_running_surfaces_failed(job_id, FailureCategory.NETWORK_ERROR)
            try:
                complete_job(job_id, db_path=self._db_path)
            except Exception:
                logger.exception("complete_job recovery failed")
            self._emit(job_id, "error", message=str(exc))

    def _do_suggest(self, job_id: int, query: str, lang: str, country: str) -> None:
        result = self._fetch_fn(query, lang, country)
        # Capture library metadata for UI visibility badge. Attributes are
        # populated by seoserper.suggest.get_suggestions; raw fetchers
        # leave them at their dataclass defaults.
        self.last_suggest_meta = {
            "provider_used": getattr(result, "provider_used", ""),
            "from_cache": getattr(result, "from_cache", False),
            "latency_ms": getattr(result, "latency_ms", 0),
        }
        update_surface(
            job_id, SurfaceName.SUGGEST, result.status,
            items=result.items, failure_category=result.failure_category,
            db_path=self._db_path,
        )
        self._emit(job_id, "suggest", status=result.status.value)

    def _do_serp(self, job_id: int, query: str, lang: str, country: str, engine: str = "google") -> None:
        # Invariant: run_serp=True implies the caller wired a real serp_fn.
        # Assert here so misuse (e.g. a bug that routes a suggest-only job
        # through the SerpAPI path) fails loudly rather than AttributeError
        # deep in the callable.
        assert self._serp_fn is not None, (
            "serp_fn is None — suggest-only jobs must not reach _do_serp"
        )

        try:
            parsed = self._serp_fn(query, lang, country, engine=engine)
        except Exception:
            # Defensive only — Unit 2's fetcher converts all known error
            # classes to ParseResults already. This catches truly unexpected
            # bugs inside the fetcher.
            logger.exception("serp_fn raised job=%d", job_id)
            self._write_serp_failure(job_id, FailureCategory.NETWORK_ERROR)
            return

        for name in (SurfaceName.PAA, SurfaceName.RELATED):
            self._apply_parsed_surface(job_id, name, parsed.get(name))

    def _apply_parsed_surface(
        self, job_id: int, name: SurfaceName, result: ParseResult | None
    ) -> None:
        if result is None:
            update_surface(
                job_id, name, SurfaceStatus.FAILED,
                failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                db_path=self._db_path,
            )
            self._emit(job_id, name.value, status=SurfaceStatus.FAILED.value)
            return

        # R8: don't overwrite an already-ok surface when retrying.
        current = self._current_surface_status(job_id, name)
        if current == SurfaceStatus.OK:
            self._emit(job_id, name.value, status=SurfaceStatus.OK.value)
            return

        update_surface(
            job_id, name, result.status,
            items=result.items, failure_category=result.failure_category,
            db_path=self._db_path,
        )
        self._emit(job_id, name.value, status=result.status.value)

    def _current_surface_status(self, job_id: int, name: SurfaceName) -> SurfaceStatus:
        with get_connection(self._db_path) as conn:
            row = conn.execute(
                "SELECT status FROM surfaces WHERE job_id = ? AND surface = ?",
                (job_id, name.value),
            ).fetchone()
        return SurfaceStatus(row["status"]) if row else SurfaceStatus.RUNNING

    def _write_serp_failure(self, job_id: int, category: FailureCategory) -> None:
        for name in (SurfaceName.PAA, SurfaceName.RELATED):
            if self._current_surface_status(job_id, name) == SurfaceStatus.OK:
                # preserve ok-on-retry
                continue
            update_surface(
                job_id, name, SurfaceStatus.FAILED,
                failure_category=category, db_path=self._db_path,
            )
            self._emit(job_id, name.value, status=SurfaceStatus.FAILED.value)

    def _mark_running_surfaces_failed(
        self, job_id: int, category: FailureCategory
    ) -> None:
        for name in SurfaceName:
            if self._current_surface_status(job_id, name) == SurfaceStatus.RUNNING:
                update_surface(
                    job_id, name, SurfaceStatus.FAILED,
                    failure_category=category, db_path=self._db_path,
                )

    def _emit(self, job_id: int, kind: str, *, status: str = "", message: str = "") -> None:
        self.progress_queue.put(
            ProgressEvent(job_id=job_id, kind=kind, status=status, message=message)
        )
