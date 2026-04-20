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

import logging
import queue
import threading
from dataclasses import dataclass
from typing import Callable

from seoserper import config
from seoserper.fetchers.suggest import SuggestResult, fetch_suggestions
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
        fetch_fn: FetchFn = fetch_suggestions,
    ):
        # serp_fn is Optional: under SERPAPI_KEY=None the engine never enters
        # _do_serp, so the fetcher is unnecessary. The invariant
        # "run_serp=True → serp_fn is not None" is asserted in _do_serp to
        # keep the contract machine-checked rather than convention.
        self._serp_fn = serp_fn
        self._db_path = db_path
        self._fetch_fn = fetch_fn
        self.progress_queue: queue.Queue[ProgressEvent] = queue.Queue()

    # --- public API ---

    def submit(self, query: str, lang: str, country: str) -> int:
        """INSERT a new job and spawn a background worker. Returns job_id.

        Reads ``config.SERPAPI_KEY`` once at the top to decide whether to run
        in full mode (3 surfaces, Suggest + SerpAPI) or suggest-only mode
        (1 surface, Suggest only). The resolved render_mode is stamped on
        the job row so historical jobs stay self-consistent if the key is
        later unset / rotated.
        """
        render_mode = "full" if config.SERPAPI_KEY else "suggest-only"
        job_id = create_job(
            query, lang, country,
            db_path=self._db_path,
            render_mode=render_mode,
        )
        run_serp = render_mode == "full"
        self._spawn_worker(
            job_id, query, lang, country,
            run_suggest=True, run_serp=run_serp,
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
            run_suggest=run_suggest, run_serp=run_serp,
        )

    # --- internal ---

    def _spawn_worker(self, job_id: int, query: str, lang: str, country: str,
                      *, run_suggest: bool, run_serp: bool) -> None:
        thread = threading.Thread(
            target=self._run_analysis,
            args=(job_id, query, lang, country, run_suggest, run_serp),
            name=f"seoserper-engine-{job_id}",
            daemon=True,
        )
        thread.start()

    def _run_analysis(self, job_id: int, query: str, lang: str, country: str,
                      run_suggest: bool, run_serp: bool) -> None:
        self._emit(job_id, "start")
        try:
            if run_suggest:
                self._do_suggest(job_id, query, lang, country)
            if run_serp:
                self._do_serp(job_id, query, lang, country)
            final = complete_job(job_id, db_path=self._db_path)
            self._emit(job_id, "complete", status=final.value)
        except BaseException as exc:
            # Safety net — engine must not leave a job in `running`.
            logger.exception("engine unhandled exception job=%d", job_id)
            self._mark_running_surfaces_failed(job_id, FailureCategory.BROWSER_CRASH)
            try:
                complete_job(job_id, db_path=self._db_path)
            except Exception:
                logger.exception("complete_job recovery failed")
            self._emit(job_id, "error", message=str(exc))

    def _do_suggest(self, job_id: int, query: str, lang: str, country: str) -> None:
        result = self._fetch_fn(query, lang, country)
        update_surface(
            job_id, SurfaceName.SUGGEST, result.status,
            items=result.items, failure_category=result.failure_category,
            db_path=self._db_path,
        )
        self._emit(job_id, "suggest", status=result.status.value)

    def _do_serp(self, job_id: int, query: str, lang: str, country: str) -> None:
        # Invariant: run_serp=True implies the caller wired a real serp_fn.
        # Assert here so misuse (e.g. a bug that routes a suggest-only job
        # through the SerpAPI path) fails loudly rather than AttributeError
        # deep in the callable.
        assert self._serp_fn is not None, (
            "serp_fn is None — suggest-only jobs must not reach _do_serp"
        )

        try:
            parsed = self._serp_fn(query, lang, country)
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
