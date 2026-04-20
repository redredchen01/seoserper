"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from seoserper.storage import init_db


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """Fresh SQLite DB per test, isolated under pytest's tmp_path."""
    path = str(tmp_path / "seoserper.db")
    init_db(path)
    return path
