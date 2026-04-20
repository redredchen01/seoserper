"""Analysis engine — orchestrates Suggest HTTP + Playwright render + parse + storage.

The engine receives a keyword + locale from the UI and coordinates the three
surfaces. Each surface is persisted independently so the UI can reveal
partial results as they arrive. The parser is injected so Unit 5 can ship
and be exercised end-to-end before Unit 4's real implementation lands.

Progressive reveal:
    engine.progress_queue is a single ``queue.Queue`` the UI drains on each
    ``st.rerun`` tick. Events are one-way (engine → UI).

Retry:
    ``retry_failed_surfaces(job_id)`` reads the current job state and only
    re-runs the surfaces whose status != ok (plan R8). If Suggest is already
    ok we won't re-hit the Google JSON endpoint; if PAA is failed we do the
    full render pass again but only overwrite non-ok surfaces on return.
"""

from __future__ import annotations

import logging
import queue
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote_plus

from seoserper import config
from seoserper.core.render import (
    BlockedByCaptchaError,
    BlockedByConsentError,
    BlockedRateLimitError,
    BrowserCrashError,
    RenderError,
    SelectorNotFoundError,
)
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

ParseFn = Callable[[str, str], dict[SurfaceName, ParseResult]]
FetchFn = Callable[[str, str, str], SuggestResult]


@dataclass
class ProgressEvent:
    """Single message in the engine → UI queue."""

    job_id: int
    kind: str  # start / suggest / paa / related / complete / error
    status: str = ""  # enum value (ok/failed/empty/running/completed/...)
    message: str = ""


_RENDER_EXC_MAP: dict[type, FailureCategory] = {
    BlockedByCaptchaError: FailureCategory.BLOCKED_BY_CAPTCHA,
    BlockedByConsentError: FailureCategory.BLOCKED_BY_CONSENT,
    BlockedRateLimitError: FailureCategory.BLOCKED_RATE_LIMIT,
    BrowserCrashError: FailureCategory.BROWSER_CRASH,
    SelectorNotFoundError: FailureCategory.SELECTOR_NOT_FOUND,
}


def _render_exc_to_category(exc: BaseException) -> FailureCategory:
    for cls, cat in _RENDER_EXC_MAP.items():
        if isinstance(exc, cls):
            return cat
    if isinstance(exc, RenderError):
        return FailureCategory.SELECTOR_NOT_FOUND
    return FailureCategory.NETWORK_ERROR


def _build_serp_url(query: str, lang: str, country: str) -> str:
    return (
        "https://www.google.com/search?"
        f"q={quote_plus(query)}&hl={lang}&gl={country}&pws=0"
    )


class AnalysisEngine:
    def __init__(
        self,
        *,
        render_thread=None,
        parse_fn: ParseFn | None = None,
        db_path: str | None = None,
        fetch_fn: FetchFn = fetch_suggestions,
        render_timeout: float = config.RENDER_TIMEOUT_SECONDS,
    ):
        # render_thread is Optional: under ENABLE_SERP_RENDER=False the engine
        # never enters _do_serp, so a real RenderThread is unnecessary. The
        # invariant "run_render → render_thread is not None" is asserted in
        # _do_serp to keep the contract machine-checked rather than convention.
        self._render_thread = render_thread
        # parse_fn is Optional for the same reason — only consumed in _do_serp.
        self._parse_fn = parse_fn
        self._db_path = db_path
        self._fetch_fn = fetch_fn
        self._render_timeout = render_timeout
        self.progress_queue: queue.Queue[ProgressEvent] = queue.Queue()

    # --- public API ---

    def submit(self, query: str, lang: str, country: str) -> int:
        """INSERT a new job and spawn a background worker. Returns job_id.

        Reads ``config.ENABLE_SERP_RENDER`` once at the top to decide whether
        to run in full mode (3 surfaces, render + parse) or suggest-only mode
        (1 surface, Suggest HTTP only). The flag is stamped onto the job row
        so historical jobs stay self-consistent if the live flag later flips.
        """
        render_mode = "full" if config.ENABLE_SERP_RENDER else "suggest-only"
        job_id = create_job(
            query, lang, country,
            db_path=self._db_path,
            render_mode=render_mode,
        )
        run_render = render_mode == "full"
        self._spawn_worker(
            job_id, query, lang, country,
            run_suggest=True, run_render=run_render,
        )
        return job_id

    def retry_failed_surfaces(self, job_id: int) -> None:
        """Re-run only the surfaces whose current status != ok.

        Retry semantics depend on the *stored* render_mode, not the live flag,
        so a historical suggest-only job stays suggest-only. ADV-1 crash-path
        guard: if the job was created in full mode but the live flag is now
        False (no real render_thread available), coerce retry to suggest-only
        semantics — retry Suggest only, leave PAA/Related as-is.
        """
        job = get_job(job_id, db_path=self._db_path)
        if job is None:
            return

        run_suggest = (
            SurfaceName.SUGGEST in job.surfaces
            and job.surfaces[SurfaceName.SUGGEST].status != SurfaceStatus.OK
        )
        if job.render_mode == "suggest-only":
            run_render = False
        elif not config.ENABLE_SERP_RENDER:
            # ADV-1 guard: historical full-mode job, live flag now off.
            # Skip render to avoid .submit() on a None render_thread.
            run_render = False
        else:
            run_render = any(
                name in job.surfaces
                and job.surfaces[name].status != SurfaceStatus.OK
                for name in (SurfaceName.PAA, SurfaceName.RELATED)
            )

        if not (run_suggest or run_render):
            return

        with get_connection(self._db_path) as conn:
            conn.execute(
                "UPDATE jobs SET status='running', overall_status='running', "
                "completed_at = NULL WHERE id = ?",
                (job_id,),
            )
        self._spawn_worker(
            job_id, job.query, job.language, job.country,
            run_suggest=run_suggest, run_render=run_render,
        )

    # --- internal ---

    def _spawn_worker(self, job_id: int, query: str, lang: str, country: str,
                      *, run_suggest: bool, run_render: bool) -> None:
        thread = threading.Thread(
            target=self._run_analysis,
            args=(job_id, query, lang, country, run_suggest, run_render),
            name=f"seoserper-engine-{job_id}",
            daemon=True,
        )
        thread.start()

    def _run_analysis(self, job_id: int, query: str, lang: str, country: str,
                      run_suggest: bool, run_render: bool) -> None:
        self._emit(job_id, "start")
        try:
            if run_suggest:
                self._do_suggest(job_id, query, lang, country)
            if run_render:
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
        # Invariant: run_render=True implies the caller has configured a real
        # render_thread + parse_fn. Assert here so misuse (e.g. a bug that
        # routes a suggest-only job through the render path) fails loudly
        # rather than NPE deep in _render_thread.submit.
        assert self._render_thread is not None, (
            "render_thread is None — suggest-only jobs must not reach _do_serp"
        )
        assert self._parse_fn is not None, "parse_fn is None — required under full render mode"
        url = _build_serp_url(query, lang, country)
        try:
            future = self._render_thread.submit(url)
            html = future.result(timeout=self._render_timeout)
        except FutureTimeoutError:
            self._write_render_failure(job_id, FailureCategory.NETWORK_ERROR)
            return
        except BaseException as exc:
            category = _render_exc_to_category(exc)
            self._write_render_failure(job_id, category)
            return

        locale = f"{lang}-{country}".lower()
        try:
            parsed = self._parse_fn(html, locale)
        except Exception:
            logger.exception("parser raised job=%d", job_id)
            self._write_render_failure(job_id, FailureCategory.SELECTOR_NOT_FOUND)
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

    def _write_render_failure(self, job_id: int, category: FailureCategory) -> None:
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
