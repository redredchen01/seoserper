"""Runtime configuration: DB path, timeouts, supported locales, restart thresholds."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = os.environ.get("SEOSERPER_DB", str(ROOT / "seoserper.db"))

# MVP-scope locales (plan §Key Decisions). Other locales work but quality is unwarranted.
SUPPORTED_LOCALES: tuple[tuple[str, str], ...] = (
    ("en", "us"),
    ("zh", "cn"),
    ("ja", "jp"),
)

# Source labels — surfaced in MD export frontmatter and UI metadata bar (R5).
SOURCE_SUGGEST = "Google Suggest API"
SOURCE_SERP = "Google Search Playwright"

# Timeouts
SUGGEST_TIMEOUT_SECONDS = 5.0
RENDER_TIMEOUT_SECONDS = 30.0

# Playwright RSS control (plan §Key Decisions "restart policy").
BROWSER_RESTART_AFTER_QUERIES = 50
BROWSER_RESTART_AFTER_SECONDS = 3600

# Sweep threshold for `running` jobs left behind by a process crash (R14 tail cleanup).
ORPHAN_RUNNING_MINUTES = 30

# Sidebar page size (R14).
HISTORY_SIDEBAR_LIMIT = 50

# Current SQLite schema version. Bump + add migration when schema changes.
SCHEMA_VERSION = 1
