"""SQLite storage: connection management, schema init, CRUD, orphan sweep.

Writer concurrency: SEOSERPER has at most one engine worker writing at a
time (single submit → 3 UPDATEs). WAL + `busy_timeout=5000` is sufficient;
no WriterThread is needed (plan §Key Decisions).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict
from typing import Iterable

from seoserper import config
from seoserper.models import (
    AnalysisJob,
    FailureCategory,
    JobStatus,
    PAAQuestion,
    RelatedSearch,
    SurfaceName,
    SurfaceResult,
    SurfaceStatus,
    Suggestion,
)

# Serialize bootstrap across threads — avoids racing WAL setup on a fresh DB.
_INIT_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    language TEXT NOT NULL,
    country TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    overall_status TEXT NOT NULL DEFAULT 'running',
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    source_suggest TEXT NOT NULL DEFAULT 'Google Suggest API',
    source_serp TEXT NOT NULL DEFAULT 'Google Search Playwright',
    render_mode TEXT NOT NULL DEFAULT 'full'
);

CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_query ON jobs(query, language, country);

CREATE TABLE IF NOT EXISTS surfaces (
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    surface TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    failure_category TEXT,
    data_json TEXT NOT NULL DEFAULT '[]',
    rank_count INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (job_id, surface)
);
"""

# Items in surfaces.data_json are serialized as plain dicts. Map surface → dataclass
# so we can rehydrate on read. `suggest` / `paa` / `related` are the only three
# permitted surfaces per `SurfaceName`.
_ITEM_CLASS: dict[SurfaceName, type] = {
    SurfaceName.SUGGEST: Suggestion,
    SurfaceName.PAA: PAAQuestion,
    SurfaceName.RELATED: RelatedSearch,
}


def init_db(db_path: str | None = None) -> str:
    """Create schema, apply migrations, return resolved path. Idempotent."""
    path = db_path or config.DB_PATH
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    with _INIT_LOCK:
        with get_connection(path) as conn:
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            conn.executescript(SCHEMA)
            # Idempotent additive migrations for legacy v0 databases that
            # predate source_suggest / source_serp / render_mode on jobs.
            _migrate_jobs_add_source_columns(conn)
            _migrate_jobs_add_render_mode(conn)
            if current < config.SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {config.SCHEMA_VERSION}")
    return path


def _migrate_jobs_add_source_columns(conn: sqlite3.Connection) -> None:
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "source_suggest" in cols and "source_serp" in cols:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        if "source_suggest" not in cols:
            conn.execute(
                "ALTER TABLE jobs ADD COLUMN source_suggest TEXT NOT NULL DEFAULT 'Google Suggest API'"
            )
        if "source_serp" not in cols:
            conn.execute(
                "ALTER TABLE jobs ADD COLUMN source_serp TEXT NOT NULL DEFAULT 'Google Search Playwright'"
            )
        conn.commit()
    except sqlite3.OperationalError as exc:
        conn.rollback()
        if "duplicate column" not in str(exc).lower():
            raise


def _migrate_jobs_add_render_mode(conn: sqlite3.Connection) -> None:
    """Add jobs.render_mode column if missing. Pre-existing rows default to 'full'."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "render_mode" in cols:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        if "render_mode" not in cols:
            conn.execute(
                "ALTER TABLE jobs ADD COLUMN render_mode TEXT NOT NULL DEFAULT 'full'"
            )
        conn.commit()
    except sqlite3.OperationalError as exc:
        conn.rollback()
        if "duplicate column" not in str(exc).lower():
            raise


@contextmanager
def get_connection(db_path: str | None = None):
    """Short-lived connection with WAL + busy_timeout + row_factory=Row."""
    path = db_path or config.DB_PATH
    conn = sqlite3.connect(path, timeout=5.0)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --- CRUD ---


def create_job(
    query: str,
    language: str,
    country: str,
    db_path: str | None = None,
    *,
    render_mode: str = "full",
) -> int:
    """INSERT a job + seed surface rows. `render_mode` controls the surface count.

    - "full"         → 3 rows (suggest / paa / related), all status=running
    - "suggest-only" → 1 row (suggest only), status=running

    `db_path` stays positional-or-keyword so existing callers keep working;
    `render_mode` is keyword-only to force explicit use.
    """
    if render_mode == "suggest-only":
        surfaces_to_seed = [SurfaceName.SUGGEST]
    else:
        surfaces_to_seed = list(SurfaceName)

    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO jobs "
            "(query, language, country, source_suggest, source_serp, render_mode) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                query,
                language,
                country,
                config.SOURCE_SUGGEST,
                config.SOURCE_SERP,
                render_mode,
            ),
        )
        job_id = cursor.lastrowid
        conn.executemany(
            "INSERT INTO surfaces (job_id, surface, status) VALUES (?, ?, 'running')",
            [(job_id, s.value) for s in surfaces_to_seed],
        )
    assert job_id is not None
    return job_id


def update_surface(
    job_id: int,
    surface: SurfaceName,
    status: SurfaceStatus,
    items: Iterable | None = None,
    failure_category: FailureCategory | None = None,
    db_path: str | None = None,
) -> None:
    """Update one surface row. `items` is list[Suggestion|PAAQuestion|RelatedSearch]."""
    items = list(items or [])
    data_json = json.dumps([asdict(i) for i in items], ensure_ascii=False)
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE surfaces "
            "SET status = ?, failure_category = ?, data_json = ?, rank_count = ?, "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE job_id = ? AND surface = ?",
            (
                status.value,
                failure_category.value if failure_category else None,
                data_json,
                len(items),
                job_id,
                surface.value,
            ),
        )


def complete_job(job_id: int, db_path: str | None = None) -> JobStatus:
    """Finalize job status. Rule: ok_count ≥ 1 → completed; else failed."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT status FROM surfaces WHERE job_id = ?", (job_id,)
        ).fetchall()
        ok_count = sum(1 for r in rows if r["status"] == SurfaceStatus.OK.value)
        final = JobStatus.COMPLETED if ok_count >= 1 else JobStatus.FAILED
        conn.execute(
            "UPDATE jobs SET status = ?, overall_status = ?, completed_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (final.value, final.value, job_id),
        )
    return final


def get_job(job_id: int, db_path: str | None = None) -> AnalysisJob | None:
    """Return full AnalysisJob with 3 hydrated surface results, or None."""
    with get_connection(db_path) as conn:
        job_row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job_row is None:
            return None
        surface_rows = conn.execute(
            "SELECT * FROM surfaces WHERE job_id = ?", (job_id,)
        ).fetchall()
    return _hydrate_job(job_row, surface_rows)


def list_recent_jobs(
    limit: int = config.HISTORY_SIDEBAR_LIMIT, db_path: str | None = None
) -> list[AnalysisJob]:
    """Sidebar history feed. Single SQL query (LEFT JOIN + GROUP_CONCAT)."""
    # GROUP_CONCAT builds a pipe-delimited surfaces blob so we avoid N+1 reads.
    # Delimiter pair: ';;' between surfaces, '|' between fields. Values that
    # could contain these are status / surface / rank_count / failure_category,
    # all drawn from closed enumerations → safe.
    sql = """
        SELECT
            j.id, j.query, j.language, j.country,
            j.status, j.overall_status, j.started_at, j.completed_at,
            j.source_suggest, j.source_serp, j.render_mode,
            GROUP_CONCAT(
                s.surface || '|' || s.status || '|' || s.rank_count || '|' ||
                COALESCE(s.failure_category, ''),
                ';;'
            ) AS surfaces_blob
        FROM jobs j
        LEFT JOIN surfaces s ON s.job_id = j.id
        WHERE j.status != 'running'
        GROUP BY j.id
        ORDER BY j.started_at DESC
        LIMIT ?
    """
    with get_connection(db_path) as conn:
        rows = conn.execute(sql, (limit,)).fetchall()
    return [_hydrate_job_from_blob(row) for row in rows]


def reap_orphaned(
    threshold_minutes: int = config.ORPHAN_RUNNING_MINUTES, db_path: str | None = None
) -> int:
    """Mark long-running jobs as failed (browser_crash). Return count reaped."""
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "UPDATE jobs SET status = 'failed', overall_status = 'failed', "
            "completed_at = CURRENT_TIMESTAMP "
            "WHERE status = 'running' AND started_at < datetime('now', ?)",
            (f"-{threshold_minutes} minutes",),
        )
        reaped = cursor.rowcount
        if reaped > 0:
            conn.execute(
                "UPDATE surfaces SET status = 'failed', failure_category = 'browser_crash', "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE status = 'running' AND job_id IN ("
                "  SELECT id FROM jobs WHERE status = 'failed' "
                "  AND started_at < datetime('now', ?)"
                ")",
                (f"-{threshold_minutes} minutes",),
            )
    return reaped


# --- row → dataclass hydration ---


def _hydrate_job(job_row: sqlite3.Row, surface_rows: list[sqlite3.Row]) -> AnalysisJob:
    surfaces: dict[SurfaceName, SurfaceResult] = {}
    for s in surface_rows:
        name = SurfaceName(s["surface"])
        items = _deserialize_items(name, s["data_json"])
        surfaces[name] = SurfaceResult(
            surface=name,
            status=SurfaceStatus(s["status"]),
            failure_category=(
                FailureCategory(s["failure_category"]) if s["failure_category"] else None
            ),
            items=items,
            rank_count=s["rank_count"],
            updated_at=s["updated_at"],
        )
    return AnalysisJob(
        id=job_row["id"],
        query=job_row["query"],
        language=job_row["language"],
        country=job_row["country"],
        status=JobStatus(job_row["status"]),
        overall_status=JobStatus(job_row["overall_status"]),
        started_at=job_row["started_at"],
        completed_at=job_row["completed_at"],
        source_suggest=job_row["source_suggest"],
        source_serp=job_row["source_serp"],
        render_mode=job_row["render_mode"],
        surfaces=surfaces,
    )


def _hydrate_job_from_blob(row: sqlite3.Row) -> AnalysisJob:
    """Sidebar path: surface items are omitted (rank_count + status are enough for badges)."""
    surfaces: dict[SurfaceName, SurfaceResult] = {}
    blob = row["surfaces_blob"] or ""
    for chunk in blob.split(";;") if blob else []:
        parts = chunk.split("|")
        if len(parts) != 4:
            continue
        name_raw, status_raw, rank_raw, fc_raw = parts
        name = SurfaceName(name_raw)
        surfaces[name] = SurfaceResult(
            surface=name,
            status=SurfaceStatus(status_raw),
            failure_category=FailureCategory(fc_raw) if fc_raw else None,
            items=[],
            rank_count=int(rank_raw or 0),
            updated_at="",
        )
    return AnalysisJob(
        id=row["id"],
        query=row["query"],
        language=row["language"],
        country=row["country"],
        status=JobStatus(row["status"]),
        overall_status=JobStatus(row["overall_status"]),
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        source_suggest=row["source_suggest"],
        source_serp=row["source_serp"],
        render_mode=row["render_mode"],
        surfaces=surfaces,
    )


def _deserialize_items(surface: SurfaceName, data_json: str) -> list:
    cls = _ITEM_CLASS[surface]
    try:
        raw = json.loads(data_json) if data_json else []
    except json.JSONDecodeError:
        return []
    return [cls(**item) for item in raw]
