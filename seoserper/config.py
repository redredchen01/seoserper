"""Runtime configuration: DB path, timeouts, supported locales, restart thresholds.

======================================================================
ENABLE_SERP_RENDER — re-enabling the full Playwright pipeline
======================================================================

SEOSERPER ships in a Suggest-only mode by default (``ENABLE_SERP_RENDER=False``).
The Google ``/search`` endpoint rate-limits aggressively from the home IP that
was used during the 2026-04-20 spike (100% /sorry redirect rate, see
``scripts/spike_results.jsonl``), so PAA + Related surfaces are disabled at
the engine boundary. The Suggest endpoint (``suggestqueries.google.com``) was
validated empirically on the same IP at 30/30 ok (see
``scripts/suggest_baseline.jsonl``) and remains the active data source.

To re-enable the Playwright pipeline:

  1. ``export SEOSERPER_ENABLE_SERP_RENDER=1`` in the shell before starting
     Streamlit (accepted truthy values: ``1``, ``true``, ``yes``, ``on``,
     case-insensitive). Or edit the module-level ``ENABLE_SERP_RENDER``
     constant below directly.
  2. **Restart Streamlit.** The env var is read at module import time, which
     means the Streamlit process must be fully restarted (Ctrl-C, then
     ``streamlit run app.py`` again). Hot-reload will not pick up the change.

Reactivation prerequisites (flipping the flag alone is NOT sufficient):

  - A network where ``https://www.google.com/search`` is not redirected to
     ``/sorry/index``. Re-run ``python scripts/spike.py run --limit 5`` to
     confirm before relying on the feature.
  - Unit 4 parser implementation (``seoserper/parsers/serp.py``) must exist;
     it is currently unshipped. Without it, PAA + Related surfaces will fall
     through to the ``selector_not_found`` stub and render as failed.

Kill criterion (Suggest SPOF):

  If ``jobs.overall_status='failed'`` exceeds 20% across any rolling 20-query
  window, assume the IP's Suggest budget is also flagged and stop using the
  tool. SEOSERPER has no engineered fallback short of network switching.

Sunset 2026-07-19:

  ``tests/test_sunset.py`` fails on 2026-07-19 to force an explicit extend-
  or-delete decision on the dormant Unit 3 / Unit 5 code. If the flag has
  stayed False the whole time, delete; if the user actively uses full mode,
  extend by bumping the date.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = os.environ.get("SEOSERPER_DB", str(ROOT / "seoserper.db"))


def _coerce_flag(value: str | None) -> bool:
    """Env-var truthy coercion. Accepts 1/true/yes/on (case-insensitive); rest is False."""
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Gate for the Playwright render pipeline. See module docstring for the full
# recovery checklist, kill criterion, and sunset. Env var is read once at
# import; attribute-level writes from tests / monkeypatch propagate to the
# next engine.submit call.
ENABLE_SERP_RENDER: bool = _coerce_flag(os.environ.get("SEOSERPER_ENABLE_SERP_RENDER"))

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
