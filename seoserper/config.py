"""Runtime configuration: DB path, timeouts, supported locales, SerpAPI key.

======================================================================
SERPAPI_KEY — enabling Full 3-surface mode (Suggest + PAA + Related)
======================================================================

SEOSERPER ships in Suggest-only mode when ``SERPAPI_KEY`` is unset (the
default). One Submit in that state yields a single Suggestions surface from
``suggestqueries.google.com`` (free, no auth, empirically validated at 30/30
ok on 2026-04-20). PAA + Related Searches require a SerpAPI key; Google has
no first-party API that returns those surfaces, and the 2026-04-20 Playwright
spike showed 5/5 ``/sorry/index`` redirects from the home IP, confirming that
direct HTML scraping of ``google.com/search`` is not viable from this network.

To enable Full mode (Suggest + PAA + Related):

  1. Sign up at https://serpapi.com — the free tier is 100 searches/month,
     no credit card required. One Full analysis costs exactly 1 SerpAPI
     search (PAA + Related come bundled in a single ``engine=google`` call).
  2. ``export SERPAPI_KEY=<your-key>`` in the shell before starting
     Streamlit. Empty-string and whitespace-only values are treated as
     unset; the key is stripped before use.
  3. **Restart Streamlit.** The env var is read at module import time, so
     hot-reload will not pick up the change. ``Ctrl-C`` then
     ``streamlit run app.py`` again.

Locale support (Full mode):

  SerpAPI ``google_domain`` is set per locale: (en, us) → google.com,
  (zh, cn) → google.com.hk (mainland google.cn has been redirected for
  years), (zh, tw) → google.com.tw, (ja, jp) → google.co.jp. Unknown
  locales fall back to google.com.

Quota-exhausted behavior:

  When the monthly SerpAPI quota runs out, the API returns an error payload
  that we map to ``FailureCategory.BLOCKED_RATE_LIMIT``. PAA + Related
  surfaces fail with that category; the Suggest surface (a different
  provider) is unaffected, so the overall job still completes with at
  least one ok surface. Wait until month rollover or upgrade to a paid
  tier at https://serpapi.com/pricing.

Secrets hygiene:

  ``SERPAPI_KEY`` is read **only** from the environment — never from a
  file, never from SQLite, never logged. The UI exposes only a boolean
  "configured / not configured" state derived from this module.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = os.environ.get("SEOSERPER_DB", str(ROOT / "seoserper.db"))


def _coerce_key(value: str | None) -> str | None:
    """SerpAPI key coerce: None / empty / whitespace-only → None; else stripped value."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


# When set, the engine activates Full mode (PAA + Related via SerpAPI). When
# None (default), the engine operates in Suggest-only mode. Read once at
# import; flipping the env var requires a Streamlit restart.
SERPAPI_KEY: str | None = _coerce_key(os.environ.get("SERPAPI_KEY"))
SERPAPI_URL: str = "https://serpapi.com/search.json"

# MVP-scope locales (plan §Key Decisions). Other locales work but quality is unwarranted.
SUPPORTED_LOCALES: tuple[tuple[str, str], ...] = (
    ("en", "us"),
    ("zh", "cn"),
    ("ja", "jp"),
)

# Source labels — surfaced in MD export frontmatter and UI metadata bar (R5).
# Unit 5 updates SOURCE_SERP to reflect the SerpAPI provider.
SOURCE_SUGGEST = "Google Suggest API"
SOURCE_SERP = "Google Search Playwright"

# Timeouts
SUGGEST_TIMEOUT_SECONDS = 5.0
RENDER_TIMEOUT_SECONDS = 30.0
# SerpAPI calls include Google-side scraping on the provider's servers, so
# end-to-end latency is typically 2-6s; 15s leaves headroom for slow months.
SERPAPI_TIMEOUT_SECONDS = 15.0

# Playwright RSS control — removed in Unit 6 alongside render.py deletion.
BROWSER_RESTART_AFTER_QUERIES = 50
BROWSER_RESTART_AFTER_SECONDS = 3600

# Sweep threshold for `running` jobs left behind by a process crash (R14 tail cleanup).
ORPHAN_RUNNING_MINUTES = 30

# Sidebar page size (R14).
HISTORY_SIDEBAR_LIMIT = 50

# Current SQLite schema version. Bump + add migration when schema changes.
SCHEMA_VERSION = 1
