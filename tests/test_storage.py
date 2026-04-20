"""Storage behavior: init / CRUD / N+1 / reap / migration / concurrency."""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from seoserper import storage
from seoserper.models import (
    FailureCategory,
    JobStatus,
    PAAQuestion,
    RelatedSearch,
    Suggestion,
    SurfaceName,
    SurfaceStatus,
)
from seoserper.storage import (
    complete_job,
    create_job,
    get_job,
    init_db,
    list_recent_jobs,
    reap_orphaned,
    update_surface,
)


def test_init_db_is_idempotent(tmp_path: Path):
    path = str(tmp_path / "a.db")
    init_db(path)
    init_db(path)  # second call must not raise


def test_create_job_then_get_job(db_path: str):
    job_id = create_job("coffee", "en", "us", db_path=db_path)
    job = get_job(job_id, db_path=db_path)
    assert job is not None
    assert job.id == job_id
    assert job.query == "coffee"
    assert job.language == "en"
    assert job.country == "us"
    assert job.status == JobStatus.RUNNING
    # All 3 surfaces seeded as running
    assert set(job.surfaces.keys()) == {SurfaceName.SUGGEST, SurfaceName.PAA, SurfaceName.RELATED}
    for s in job.surfaces.values():
        assert s.status == SurfaceStatus.RUNNING
        assert s.items == []


def test_update_surface_persists_items(db_path: str):
    job_id = create_job("coffee", "en", "us", db_path=db_path)
    items = [Suggestion(text="coffee near me", rank=1), Suggestion(text="coffee shop", rank=2)]
    update_surface(job_id, SurfaceName.SUGGEST, SurfaceStatus.OK, items=items, db_path=db_path)

    job = get_job(job_id, db_path=db_path)
    surf = job.surfaces[SurfaceName.SUGGEST]
    assert surf.status == SurfaceStatus.OK
    assert surf.rank_count == 2
    assert surf.items == items
    assert surf.failure_category is None


def test_update_surface_records_failure_category(db_path: str):
    job_id = create_job("coffee", "en", "us", db_path=db_path)
    update_surface(
        job_id,
        SurfaceName.PAA,
        SurfaceStatus.FAILED,
        failure_category=FailureCategory.BLOCKED_RATE_LIMIT,
        db_path=db_path,
    )
    job = get_job(job_id, db_path=db_path)
    assert job.surfaces[SurfaceName.PAA].status == SurfaceStatus.FAILED
    assert job.surfaces[SurfaceName.PAA].failure_category == FailureCategory.BLOCKED_RATE_LIMIT
    assert job.surfaces[SurfaceName.PAA].rank_count == 0


def test_complete_job_completed_when_any_ok(db_path: str):
    job_id = create_job("coffee", "en", "us", db_path=db_path)
    update_surface(job_id, SurfaceName.SUGGEST, SurfaceStatus.OK, items=[], db_path=db_path)
    update_surface(
        job_id,
        SurfaceName.PAA,
        SurfaceStatus.FAILED,
        failure_category=FailureCategory.BLOCKED_RATE_LIMIT,
        db_path=db_path,
    )
    update_surface(
        job_id,
        SurfaceName.RELATED,
        SurfaceStatus.FAILED,
        failure_category=FailureCategory.BLOCKED_RATE_LIMIT,
        db_path=db_path,
    )
    final = complete_job(job_id, db_path=db_path)
    assert final == JobStatus.COMPLETED
    assert get_job(job_id, db_path=db_path).status == JobStatus.COMPLETED


def test_complete_job_failed_when_zero_ok(db_path: str):
    job_id = create_job("coffee", "en", "us", db_path=db_path)
    for name in SurfaceName:
        update_surface(
            job_id,
            name,
            SurfaceStatus.FAILED,
            failure_category=FailureCategory.NETWORK_ERROR,
            db_path=db_path,
        )
    final = complete_job(job_id, db_path=db_path)
    assert final == JobStatus.FAILED


def test_complete_job_empty_does_not_count_as_ok(db_path: str):
    """Three empty surfaces = failed (plan §Key Decisions)."""
    job_id = create_job("coffee", "en", "us", db_path=db_path)
    for name in SurfaceName:
        update_surface(job_id, name, SurfaceStatus.EMPTY, items=[], db_path=db_path)
    final = complete_job(job_id, db_path=db_path)
    assert final == JobStatus.FAILED


def test_list_recent_jobs_excludes_running(db_path: str):
    done_id = create_job("a", "en", "us", db_path=db_path)
    update_surface(done_id, SurfaceName.SUGGEST, SurfaceStatus.OK, items=[], db_path=db_path)
    complete_job(done_id, db_path=db_path)

    _running_id = create_job("b", "en", "us", db_path=db_path)  # left running

    jobs = list_recent_jobs(db_path=db_path)
    ids = [j.id for j in jobs]
    assert done_id in ids
    assert _running_id not in ids


def test_list_recent_jobs_preserves_duplicates(db_path: str):
    """R15: same (query, lang, country) makes new rows; all visible in list."""
    for _ in range(3):
        jid = create_job("coffee", "en", "us", db_path=db_path)
        update_surface(jid, SurfaceName.SUGGEST, SurfaceStatus.OK, items=[], db_path=db_path)
        complete_job(jid, db_path=db_path)
    jobs = list_recent_jobs(db_path=db_path)
    assert len([j for j in jobs if j.query == "coffee"]) == 3


def test_list_recent_jobs_respects_limit(db_path: str):
    for i in range(5):
        jid = create_job(f"q{i}", "en", "us", db_path=db_path)
        update_surface(jid, SurfaceName.SUGGEST, SurfaceStatus.OK, items=[], db_path=db_path)
        complete_job(jid, db_path=db_path)
    assert len(list_recent_jobs(limit=3, db_path=db_path)) == 3


def test_list_recent_jobs_is_single_query(db_path: str, monkeypatch):
    """No N+1 — sidebar load issues exactly one SELECT."""
    # Seed 5 completed jobs with data in each surface.
    for i in range(5):
        jid = create_job(f"q{i}", "en", "us", db_path=db_path)
        update_surface(
            jid,
            SurfaceName.SUGGEST,
            SurfaceStatus.OK,
            items=[Suggestion(text="x", rank=1)],
            db_path=db_path,
        )
        update_surface(
            jid,
            SurfaceName.PAA,
            SurfaceStatus.EMPTY,
            items=[],
            db_path=db_path,
        )
        update_surface(
            jid,
            SurfaceName.RELATED,
            SurfaceStatus.FAILED,
            failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            db_path=db_path,
        )
        complete_job(jid, db_path=db_path)

    queries: list[str] = []
    original = storage.get_connection

    @contextmanager
    def traced(db_path=None):
        with original(db_path) as conn:
            conn.set_trace_callback(lambda s: queries.append(s))
            yield conn

    monkeypatch.setattr(storage, "get_connection", traced)
    queries.clear()

    jobs = list_recent_jobs(db_path=db_path)

    selects = [q for q in queries if q.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 1, f"expected 1 SELECT, saw {len(selects)}: {selects}"
    assert len(jobs) == 5
    # Surface badges hydrated from the blob
    for j in jobs:
        assert set(j.surfaces.keys()) == {SurfaceName.SUGGEST, SurfaceName.PAA, SurfaceName.RELATED}
        assert j.surfaces[SurfaceName.SUGGEST].status == SurfaceStatus.OK
        assert j.surfaces[SurfaceName.SUGGEST].rank_count == 1
        assert j.surfaces[SurfaceName.RELATED].failure_category == FailureCategory.SELECTOR_NOT_FOUND


def test_reap_orphaned_marks_old_running_failed(db_path: str):
    fresh_id = create_job("fresh", "en", "us", db_path=db_path)
    old_id = create_job("old", "en", "us", db_path=db_path)
    # Backdate "old" by 35 minutes
    with storage.get_connection(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET started_at = datetime('now', '-35 minutes') WHERE id = ?",
            (old_id,),
        )

    reaped = reap_orphaned(threshold_minutes=30, db_path=db_path)
    assert reaped == 1
    assert get_job(old_id, db_path=db_path).status == JobStatus.FAILED
    assert get_job(fresh_id, db_path=db_path).status == JobStatus.RUNNING
    for s in get_job(old_id, db_path=db_path).surfaces.values():
        assert s.status == SurfaceStatus.FAILED
        assert s.failure_category == FailureCategory.NETWORK_ERROR


def test_schema_v0_migration_adds_source_columns(tmp_path: Path):
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT NOT NULL,
            language TEXT NOT NULL,
            country TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            overall_status TEXT NOT NULL DEFAULT 'running',
            started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP
        );
        CREATE TABLE surfaces (
            job_id INTEGER,
            surface TEXT,
            status TEXT DEFAULT 'running',
            failure_category TEXT,
            data_json TEXT NOT NULL DEFAULT '[]',
            rank_count INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(job_id, surface)
        );
        """
    )
    # user_version stays at 0 (the default).
    conn.execute("INSERT INTO jobs (query, language, country) VALUES ('legacy', 'en', 'us')")
    conn.commit()
    conn.close()

    init_db(path)

    with storage.get_connection(path) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
        assert "source_suggest" in cols
        assert "source_serp" in cols
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 1
        legacy = conn.execute("SELECT source_suggest, source_serp FROM jobs WHERE query='legacy'").fetchone()
        assert legacy["source_suggest"] == "Google Suggest API"
        # Migration default updated in plan 003 when Playwright path retired.
        # Legacy v0 rows going through current init_db get the SerpAPI label
        # as backfill; rows written while the old default was in force keep
        # their stored value (no retroactive rewrite).
        assert legacy["source_serp"] == "SerpAPI"


def test_concurrent_update_surface_on_same_job(db_path: str):
    """Parallel UPDATEs on different surfaces of the same job should not raise SQLITE_BUSY."""
    job_id = create_job("coffee", "en", "us", db_path=db_path)
    errors: list[Exception] = []

    def worker(surface: SurfaceName) -> None:
        try:
            update_surface(
                job_id,
                surface,
                SurfaceStatus.OK,
                items=[Suggestion(text=f"x-{surface.value}", rank=1)]
                if surface == SurfaceName.SUGGEST
                else [],
                db_path=db_path,
            )
        except Exception as exc:  # pragma: no cover — failure is the assertion target
            errors.append(exc)

    t1 = threading.Thread(target=worker, args=(SurfaceName.SUGGEST,))
    t2 = threading.Thread(target=worker, args=(SurfaceName.PAA,))
    t3 = threading.Thread(target=worker, args=(SurfaceName.RELATED,))
    for t in (t1, t2, t3):
        t.start()
    for t in (t1, t2, t3):
        t.join()

    assert not errors, f"concurrent update raised: {errors}"
    job = get_job(job_id, db_path=db_path)
    for s in job.surfaces.values():
        assert s.status == SurfaceStatus.OK


def test_hydrate_all_three_item_types(db_path: str):
    job_id = create_job("coffee", "en", "us", db_path=db_path)
    update_surface(
        job_id,
        SurfaceName.SUGGEST,
        SurfaceStatus.OK,
        items=[Suggestion(text="coffee near me", rank=1)],
        db_path=db_path,
    )
    update_surface(
        job_id,
        SurfaceName.PAA,
        SurfaceStatus.OK,
        items=[PAAQuestion(question="Is coffee good?", rank=1, answer_preview="Depends.")],
        db_path=db_path,
    )
    update_surface(
        job_id,
        SurfaceName.RELATED,
        SurfaceStatus.OK,
        items=[RelatedSearch(query="espresso", rank=1)],
        db_path=db_path,
    )
    job = get_job(job_id, db_path=db_path)
    assert isinstance(job.surfaces[SurfaceName.SUGGEST].items[0], Suggestion)
    assert isinstance(job.surfaces[SurfaceName.PAA].items[0], PAAQuestion)
    assert isinstance(job.surfaces[SurfaceName.RELATED].items[0], RelatedSearch)


def test_get_job_returns_none_for_missing(db_path: str):
    assert get_job(9999, db_path=db_path) is None


# --- render_mode (suggest-only pivot, Unit 1) --------------------------------


def test_create_job_defaults_to_full_render_mode(db_path: str):
    jid = create_job("coffee", "en", "us", db_path=db_path)
    job = get_job(jid, db_path=db_path)
    assert job.render_mode == "full"
    assert set(job.surfaces.keys()) == {
        SurfaceName.SUGGEST,
        SurfaceName.PAA,
        SurfaceName.RELATED,
    }


def test_create_job_suggest_only_seeds_one_surface(db_path: str):
    jid = create_job("coffee", "en", "us", db_path=db_path, render_mode="suggest-only")
    job = get_job(jid, db_path=db_path)
    assert job.render_mode == "suggest-only"
    assert list(job.surfaces.keys()) == [SurfaceName.SUGGEST]
    assert job.surfaces[SurfaceName.SUGGEST].status == SurfaceStatus.RUNNING


def test_complete_job_on_suggest_only_uses_single_surface_rule(db_path: str):
    jid = create_job("coffee", "en", "us", db_path=db_path, render_mode="suggest-only")
    update_surface(
        jid,
        SurfaceName.SUGGEST,
        SurfaceStatus.OK,
        items=[Suggestion(text="coffee shop", rank=1)],
        db_path=db_path,
    )
    final = complete_job(jid, db_path=db_path)
    assert final == JobStatus.COMPLETED  # ok_count=1 >= 1 → completed


def test_complete_job_suggest_only_failed_marks_failed(db_path: str):
    jid = create_job("coffee", "en", "us", db_path=db_path, render_mode="suggest-only")
    update_surface(
        jid,
        SurfaceName.SUGGEST,
        SurfaceStatus.FAILED,
        failure_category=FailureCategory.NETWORK_ERROR,
        db_path=db_path,
    )
    final = complete_job(jid, db_path=db_path)
    assert final == JobStatus.FAILED  # ok_count=0 → failed


def test_list_recent_jobs_carries_render_mode(db_path: str):
    jid_full = create_job("a", "en", "us", db_path=db_path)
    update_surface(jid_full, SurfaceName.SUGGEST, SurfaceStatus.OK, items=[], db_path=db_path)
    complete_job(jid_full, db_path=db_path)

    jid_so = create_job("b", "en", "us", db_path=db_path, render_mode="suggest-only")
    update_surface(jid_so, SurfaceName.SUGGEST, SurfaceStatus.OK, items=[], db_path=db_path)
    complete_job(jid_so, db_path=db_path)

    jobs = {j.id: j for j in list_recent_jobs(db_path=db_path)}
    assert jobs[jid_full].render_mode == "full"
    assert jobs[jid_so].render_mode == "suggest-only"


def test_schema_v0_migration_adds_render_mode_column(tmp_path: Path):
    """Legacy DB without render_mode column → init_db adds it with default 'full'."""
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    # Simulate a v1-shaped DB (has source_suggest / source_serp) but NO render_mode
    conn.executescript(
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT NOT NULL,
            language TEXT NOT NULL,
            country TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            overall_status TEXT NOT NULL DEFAULT 'running',
            started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            source_suggest TEXT NOT NULL DEFAULT 'Google Suggest API',
            source_serp TEXT NOT NULL DEFAULT 'Google Search Playwright'
        );
        CREATE TABLE surfaces (
            job_id INTEGER,
            surface TEXT,
            status TEXT DEFAULT 'running',
            failure_category TEXT,
            data_json TEXT NOT NULL DEFAULT '[]',
            rank_count INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(job_id, surface)
        );
        """
    )
    conn.execute(
        "INSERT INTO jobs (query, language, country) VALUES ('legacy', 'en', 'us')"
    )
    conn.commit()
    conn.close()

    init_db(path)

    with storage.get_connection(path) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
        assert "render_mode" in cols
        row = conn.execute(
            "SELECT render_mode FROM jobs WHERE query='legacy'"
        ).fetchone()
        assert row["render_mode"] == "full"


def test_migration_render_mode_is_idempotent(db_path: str):
    """Running init_db twice on a fresh DB must not break the render_mode column."""
    # db_path fixture already ran init_db once; run again
    storage.init_db(db_path)
    with storage.get_connection(db_path) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
        assert "render_mode" in cols


# --- delete_job (Unit L) -----------------------------------------------------


def test_delete_job_removes_row_and_surfaces(db_path: str):
    from seoserper.storage import delete_job, get_connection
    jid = create_job("coffee", "en", "us", db_path=db_path)
    update_surface(jid, SurfaceName.SUGGEST, SurfaceStatus.OK, items=[], db_path=db_path)

    removed = delete_job(jid, db_path=db_path)
    assert removed is True
    assert get_job(jid, db_path=db_path) is None
    with get_connection(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM surfaces WHERE job_id = ?", (jid,)).fetchone()[0]
        assert n == 0, "surface rows should cascade-delete"


def test_delete_job_missing_id_returns_false(db_path: str):
    from seoserper.storage import delete_job
    assert delete_job(999999, db_path=db_path) is False


def test_delete_job_leaves_siblings_untouched(db_path: str):
    from seoserper.storage import delete_job
    a = create_job("a", "en", "us", db_path=db_path)
    b = create_job("b", "en", "us", db_path=db_path)
    delete_job(a, db_path=db_path)
    assert get_job(a, db_path=db_path) is None
    assert get_job(b, db_path=db_path) is not None


# --- engine column (plan 005 Unit 1) -----------------------------------------


def test_create_job_default_engine_is_google(db_path: str):
    jid = create_job("coffee", "en", "us", db_path=db_path)
    job = get_job(jid, db_path=db_path)
    assert job.engine == "google"


def test_create_job_explicit_engine_bing_seeds_two_surfaces(db_path: str):
    jid = create_job("coffee", "en", "us", db_path=db_path, engine="bing")
    job = get_job(jid, db_path=db_path)
    assert job.engine == "bing"
    # Bing: no SUGGEST row, only PAA + RELATED.
    assert set(job.surfaces.keys()) == {SurfaceName.PAA, SurfaceName.RELATED}
    assert SurfaceName.SUGGEST not in job.surfaces
    for s in job.surfaces.values():
        assert s.status == SurfaceStatus.RUNNING


def test_create_job_google_suggest_only_still_works(db_path: str):
    """Regression: Bing branch must not steal the suggest-only path."""
    jid = create_job("coffee", "en", "us", db_path=db_path, render_mode="suggest-only")
    job = get_job(jid, db_path=db_path)
    assert job.engine == "google"
    assert list(job.surfaces.keys()) == [SurfaceName.SUGGEST]


def test_list_recent_jobs_hydrates_engine(db_path: str):
    jid_g = create_job("coffee", "en", "us", db_path=db_path)
    jid_b = create_job("tea", "en", "us", db_path=db_path, engine="bing")
    from seoserper.storage import update_surface
    update_surface(jid_g, SurfaceName.SUGGEST, SurfaceStatus.OK, items=[], db_path=db_path)
    update_surface(jid_b, SurfaceName.PAA, SurfaceStatus.OK, items=[], db_path=db_path)
    complete_job(jid_g, db_path=db_path)
    complete_job(jid_b, db_path=db_path)
    engines = {j.id: j.engine for j in list_recent_jobs(db_path=db_path)}
    assert engines.get(jid_g) == "google"
    assert engines.get(jid_b) == "bing"


def test_schema_v0_migration_adds_engine_column(tmp_path: Path):
    """Pre-plan-005 DB (no engine column) gets the column + 'google' default."""
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as conn:
        # Mimic a schema before plan 005 — includes render_mode but no engine.
        conn.executescript(
            """
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL,
                language TEXT NOT NULL,
                country TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                overall_status TEXT NOT NULL DEFAULT 'running',
                started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                source_suggest TEXT NOT NULL DEFAULT 'Google Suggest API',
                source_serp TEXT NOT NULL DEFAULT 'SerpAPI',
                render_mode TEXT NOT NULL DEFAULT 'full'
            );
            CREATE TABLE surfaces (
                job_id INTEGER NOT NULL,
                surface TEXT NOT NULL,
                status TEXT NOT NULL,
                failure_category TEXT,
                data_json TEXT NOT NULL DEFAULT '[]',
                rank_count INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (job_id, surface)
            );
            INSERT INTO jobs (query, language, country) VALUES ('pre-p5', 'en', 'us');
            """
        )

    init_db(path)

    with storage.get_connection(path) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
        assert "engine" in cols
        row = conn.execute(
            "SELECT engine FROM jobs WHERE query = 'pre-p5'"
        ).fetchone()
        assert row["engine"] == "google"
