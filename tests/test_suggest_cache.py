"""Plan 005 Unit 2: suggest_cache table + status-aware TTL helpers."""

from __future__ import annotations

import logging
import sqlite3
import time

import pytest

from seoserper.storage import get_connection, suggest_cache_get, suggest_cache_put


# --- happy path --------------------------------------------------------------


def test_ok_row_round_trip(db_path):
    key = "google|coffee|en|us"
    items = [{"text": "coffee shop", "rank": 1}, {"text": "coffee bean", "rank": 2}]
    suggest_cache_put(key, "ok", items, db_path=db_path)

    hit = suggest_cache_get(key, 43200, 300, db_path=db_path)
    assert hit == {"status": "ok", "items": items}


def test_empty_row_round_trip(db_path):
    key = "google|unknownquery|en|us"
    suggest_cache_put(key, "empty", [], db_path=db_path)

    hit = suggest_cache_get(key, 43200, 300, db_path=db_path)
    assert hit == {"status": "empty", "items": []}


def test_missing_key_returns_none(db_path):
    assert suggest_cache_get("absent", 43200, 300, db_path=db_path) is None


# --- TTL enforcement ---------------------------------------------------------


def test_ok_row_expired_returns_none(db_path):
    key = "google|coffee|en|us"
    suggest_cache_put(key, "ok", [{"text": "coffee", "rank": 1}], db_path=db_path)

    # Ask for 0s TTL so the fresh write immediately looks stale.
    time.sleep(1.1)
    assert suggest_cache_get(key, ttl_ok_seconds=0, ttl_empty_seconds=0, db_path=db_path) is None


def test_empty_ttl_stricter_than_ok_ttl(db_path):
    """An `empty` row aged between empty_ttl and ok_ttl must be treated as miss.

    This is the status-aware TTL invariant — we cannot just use the larger TTL
    for both row types.
    """
    key = "google|q|en|us"
    suggest_cache_put(key, "empty", [], db_path=db_path)
    time.sleep(1.1)

    # EMPTY rows older than 1s are stale here; OK rows would still be fresh
    # at 10s. The row must be invisible under status-aware TTL semantics.
    assert suggest_cache_get(key, ttl_ok_seconds=10, ttl_empty_seconds=0, db_path=db_path) is None


def test_empty_row_fresh_within_empty_ttl(db_path):
    key = "google|q|en|us"
    suggest_cache_put(key, "empty", [], db_path=db_path)
    hit = suggest_cache_get(key, ttl_ok_seconds=43200, ttl_empty_seconds=300, db_path=db_path)
    assert hit == {"status": "empty", "items": []}


# --- CHECK constraint --------------------------------------------------------


def test_status_check_constraint_rejects_invalid(db_path):
    with pytest.raises(sqlite3.IntegrityError):
        suggest_cache_put("k", "degraded", [], db_path=db_path)


def test_status_check_constraint_rejects_failed(db_path):
    with pytest.raises(sqlite3.IntegrityError):
        suggest_cache_put("k", "failed", [], db_path=db_path)


# --- corrupt row handling ----------------------------------------------------


def test_malformed_row_warns_and_deletes(db_path, caplog):
    key = "google|bad|en|us"
    # Bypass put() and write directly so we can simulate corruption.
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO suggest_cache (cache_key, response_json, status) "
            "VALUES (?, ?, ?)",
            (key, "{not-valid-json", "ok"),
        )

    with caplog.at_level(logging.WARNING, logger="seoserper.storage"):
        result = suggest_cache_get(key, 43200, 300, db_path=db_path)

    assert result is None
    assert any("malformed row" in r.message for r in caplog.records)

    # Row was deleted on detection; subsequent reads behave as plain miss.
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM suggest_cache WHERE cache_key = ?", (key,)
        ).fetchone()
    assert row is None


# --- opportunistic prune inside put ------------------------------------------


def test_put_with_ttl_prunes_expired(db_path):
    # Seed one fresh + one stale row.
    suggest_cache_put("fresh", "ok", [{"text": "a", "rank": 1}], db_path=db_path)
    # Manually insert a stale row to avoid sleeping 5 minutes in the test.
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO suggest_cache (cache_key, response_json, status, created_at) "
            "VALUES (?, ?, ?, datetime('now', '-7200 seconds'))",
            ("stale", '{"items": []}', "ok"),
        )

    # Put a third row with ttl_seconds=3600 → prunes anything older than 1h.
    suggest_cache_put("new", "ok", [], db_path=db_path, ttl_seconds=3600)

    with get_connection(db_path) as conn:
        keys = {row["cache_key"] for row in conn.execute("SELECT cache_key FROM suggest_cache")}

    assert "fresh" in keys
    assert "new" in keys
    assert "stale" not in keys


# --- init_db creates the table ----------------------------------------------


def test_init_db_creates_table(db_path):
    with get_connection(db_path) as conn:
        cols = {row["name"]: row for row in conn.execute("PRAGMA table_info(suggest_cache)")}

    assert {"cache_key", "response_json", "status", "created_at"} <= set(cols)
    assert cols["cache_key"]["pk"] == 1
    assert cols["status"]["notnull"] == 1
