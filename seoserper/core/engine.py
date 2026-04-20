"""Analysis engine that orchestrates Suggest + Render + Parse."""

import json
import logging
import queue
import threading
import time
from dataclasses import asdict, dataclass
from typing import Optional

from seoserper.config import Config
from seoserper.core.render import RenderError, RenderThread
from seoserper.fetchers.suggest import fetch_suggestions
from seoserper.models import (
    AnalysisJob,
    FailureCategory,
    JobStatus,
    SurfaceName,
    SurfaceStatus,
)
from seoserper.parsers.serp import parse_serp
from seoserper.storage import get_connection, update_surface


logger = logging.getLogger(__name__)


@dataclass
class ProgressEvent:
    """Progress update from engine to UI."""

    job_id: int
    phase: str  # "start", "suggest", "paa", "related", "complete", "error"
    status: Optional[SurfaceStatus] = None
    failure_category: Optional[FailureCategory] = None
    timestamp: Optional[str] = None


class AnalysisEngine:
    """Orchestrates SERP analysis across three surfaces."""

    def __init__(self, render_thread: RenderThread, db_path: str = ":memory:"):
        self.render_thread = render_thread
        self.db_path = db_path
        self.progress_queue: queue.Queue[ProgressEvent] = queue.Queue()
        self._config = Config()

    def submit(self, query: str, language: str, country: str) -> int:
        """Submit an analysis job and return job ID.

        Returns immediately; processing happens in background worker thread.
        """
        with get_connection(self.db_path) as conn:
            job_id = self._create_job(conn, query, language, country)

        # Start background worker
        worker = threading.Thread(
            target=self._run_analysis,
            args=(job_id, query, language, country),
            daemon=True,
        )
        worker.start()
        return job_id

    def retry_failed_surfaces(self, job_id: int) -> None:
        """Rerun only failed surfaces of an existing job."""
        with get_connection(self.db_path) as conn:
            job = self._get_job_for_retry(conn, job_id)
            if not job:
                return

        # Restart background worker for this job
        worker = threading.Thread(
            target=self._run_analysis_retry,
            args=(job_id, job["query"], job["language"], job["country"]),
            daemon=True,
        )
        worker.start()

    def _create_job(
        self, conn, query: str, language: str, country: str
    ) -> int:
        """Insert job row and return ID."""
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO jobs
            (query, language, country, status, overall_status, started_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            """,
            (query, language, country, JobStatus.RUNNING.value, JobStatus.RUNNING.value),
        )
        conn.commit()

        # Initialize three surface rows
        job_id = cursor.lastrowid
        for surface in [SurfaceName.SUGGEST, SurfaceName.PAA, SurfaceName.RELATED]:
            cursor.execute(
                """
                INSERT INTO surfaces
                (job_id, surface, status, failure_category, updated_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                """,
                (job_id, surface.value, SurfaceStatus.RUNNING.value, None),
            )
        conn.commit()
        return job_id

    def _get_job_for_retry(self, conn, job_id: int) -> Optional[dict]:
        """Get job data including query/language/country."""
        cursor = conn.cursor()
        cursor.execute(
            "SELECT query, language, country FROM jobs WHERE id = ?", (job_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def _run_analysis(
        self, job_id: int, query: str, language: str, country: str
    ) -> None:
        """Background worker: fetch suggest → render → parse → update storage."""
        try:
            now = time.isoformat(time.gmtime())
            self.progress_queue.put(ProgressEvent(job_id, "start", timestamp=now))

            # 1. Fetch Suggestions (fast, no Playwright)
            suggest_result = fetch_suggestions(query, language, country)
            with get_connection(self.db_path) as conn:
                update_surface(
                    conn,
                    job_id,
                    SurfaceName.SUGGEST,
                    suggest_result.status,
                    suggest_result.failure_category,
                    suggest_result.items,
                )
            self.progress_queue.put(
                ProgressEvent(
                    job_id,
                    SurfaceName.SUGGEST.value,
                    suggest_result.status,
                    suggest_result.failure_category,
                )
            )

            # 2. Render and Parse (or mark both as failed if render fails)
            if suggest_result.status == SurfaceStatus.OK:
                self._render_and_parse(job_id, query, language, country)
            else:
                # Suggest failed, but still try to render for PAA+Related
                self._render_and_parse(job_id, query, language, country)

            # 3. Mark job complete (by ok_count >= 1 rule)
            with get_connection(self.db_path) as conn:
                self._complete_job(conn, job_id)

            self.progress_queue.put(ProgressEvent(job_id, "complete"))

        except Exception as e:
            logger.exception(f"Error in analysis job {job_id}: {e}")
            with get_connection(self.db_path) as conn:
                # Mark all surfaces as failed
                for surface in [SurfaceName.SUGGEST, SurfaceName.PAA, SurfaceName.RELATED]:
                    update_surface(
                        conn,
                        job_id,
                        surface,
                        SurfaceStatus.FAILED,
                        FailureCategory.BROWSER_CRASH,
                        [],
                    )
                self._complete_job(conn, job_id)
            self.progress_queue.put(
                ProgressEvent(
                    job_id,
                    "error",
                    SurfaceStatus.FAILED,
                    FailureCategory.BROWSER_CRASH,
                )
            )

    def _render_and_parse(
        self, job_id: int, query: str, language: str, country: str
    ) -> None:
        """Render SERP and parse PAA + Related."""
        try:
            # Submit render
            search_url = f"https://www.google.com/search?q={query}&hl={language}&gl={country}"
            render_future = self.render_thread.submit(search_url)
            html = render_future.result(timeout=30)

            # Parse both PAA and Related from same HTML
            parse_result = parse_serp(html, language)

            # Update PAA
            with get_connection(self.db_path) as conn:
                paa_result = parse_result[SurfaceName.PAA]
                update_surface(
                    conn,
                    job_id,
                    SurfaceName.PAA,
                    paa_result.status,
                    paa_result.failure_category,
                    paa_result.items,
                )
                self.progress_queue.put(
                    ProgressEvent(
                        job_id,
                        SurfaceName.PAA.value,
                        paa_result.status,
                        paa_result.failure_category,
                    )
                )

            # Update Related
            with get_connection(self.db_path) as conn:
                related_result = parse_result[SurfaceName.RELATED]
                update_surface(
                    conn,
                    job_id,
                    SurfaceName.RELATED,
                    related_result.status,
                    related_result.failure_category,
                    related_result.items,
                )
                self.progress_queue.put(
                    ProgressEvent(
                        job_id,
                        SurfaceName.RELATED.value,
                        related_result.status,
                        related_result.failure_category,
                    )
                )

        except RenderError as e:
            # Map RenderError to failure
            with get_connection(self.db_path) as conn:
                for surface in [SurfaceName.PAA, SurfaceName.RELATED]:
                    update_surface(
                        conn,
                        job_id,
                        surface,
                        SurfaceStatus.FAILED,
                        e.failure_category,
                        [],
                    )
                    self.progress_queue.put(
                        ProgressEvent(
                            job_id,
                            surface.value,
                            SurfaceStatus.FAILED,
                            e.failure_category,
                        )
                    )

    def _run_analysis_retry(
        self, job_id: int, query: str, language: str, country: str
    ) -> None:
        """Retry failed surfaces by re-rendering and re-parsing."""
        try:
            # Reset job status to RUNNING
            with get_connection(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE jobs SET status = ? WHERE id = ?",
                    (JobStatus.RUNNING.value, job_id),
                )
                conn.commit()

            # Re-render and parse (will update only the failed surfaces)
            self._render_and_parse(job_id, query, language, country)

            # Recompute job status
            with get_connection(self.db_path) as conn:
                self._complete_job(conn, job_id)

            self.progress_queue.put(ProgressEvent(job_id, "complete"))

        except Exception as e:
            logger.exception(f"Error in retry for job {job_id}: {e}")

    def _complete_job(self, conn, job_id: int) -> None:
        """Mark job as completed/failed based on ok_count >= 1 rule."""
        cursor = conn.cursor()

        # Count OK surfaces
        cursor.execute(
            """
            SELECT COUNT(*) as ok_count
            FROM surfaces
            WHERE job_id = ? AND status = ?
            """,
            (job_id, SurfaceStatus.OK.value),
        )
        ok_count = cursor.fetchone()["ok_count"]

        # Judge overall status
        overall_status = (
            JobStatus.COMPLETED if ok_count >= 1 else JobStatus.FAILED
        )

        cursor.execute(
            """
            UPDATE jobs
            SET status = ?, overall_status = ?, completed_at = datetime('now')
            WHERE id = ?
            """,
            (overall_status.value, overall_status.value, job_id),
        )
        conn.commit()
