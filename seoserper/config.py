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

  1. Sign up at https://serpapi.com — free tier allocates somewhere around
     100-250 searches/month depending on plan / trial state (confirmed
     2026-04-20: one account saw 250/month). Check your actual ceiling at
     https://serpapi.com/manage-api-key. No credit card required. One Full
     analysis costs exactly 1 SerpAPI search (PAA + Related come bundled
     in a single ``engine=google`` call).
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

Engine selection (plan 005):

  The UI exposes a Google / Bing radio; both go through the same
  ``SERPAPI_KEY`` and share the same monthly quota pool (1 credit per
  Submit regardless of engine). Bing returns PAA + Related but has no
  public autocomplete endpoint, so Bing jobs skip the Suggest surface
  entirely. Bing PAA is opportunistic (~20-40% of queries); when Google
  returns a non-empty PAA for the same query, Bing may be EMPTY — this
  is upstream behavior, not a tool issue.

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

# MVP-scope locales (plan §Key Decisions). Each tuple is (lang, country, label).
# The UI renders label text; engine/storage consume the (lang, country) pair.
# Users CAN still analyze other locales via direct DB manipulation; the UI
# is opinionated for the quality-validated set only.
SUPPORTED_LOCALES: tuple[tuple[str, str, str], ...] = (
    # 简体中文 is the primary user's default (session 2026-04-20). Order
    # matters: Streamlit's selectbox defaults to options[0] so the first
    # entry is what the user sees without any interaction.
    ("zh-CN", "cn", "简体中文 (CN)"),
    ("en", "us", "English (US)"),
    ("zh-TW", "tw", "繁體中文 (TW)"),
    ("ja", "jp", "日本語 (JP)"),
)

# Source labels — surfaced in MD export frontmatter and UI metadata bar (R5).
SOURCE_SUGGEST = "Google Suggest API"
SOURCE_SERP = "SerpAPI"

# Suggest library (seoserper.suggest.get_suggestions) constants.
# Q_MAX_LENGTH caps the normalized query length to prevent log/cache blow-up.
# CACHE_TTL / EMPTY_TTL are enforced in SQL on the suggest_cache `status`
# column — OK rows are served for 12h, EMPTY rows only 5min so a recovery is
# instantly visible. RETRY_DELAY gates the library's single transient retry.
# STATIC_FALLBACK ships OFF; flipping it on activates _static_fallback (stub).
SUGGEST_Q_MAX_LENGTH = 128
SUGGEST_CACHE_TTL_SECONDS = 43200  # 12h
SUGGEST_EMPTY_TTL_SECONDS = 300  # 5min
SUGGEST_RETRY_DELAY_SECONDS = 0.2
SUGGEST_STATIC_FALLBACK: bool = False

# Timeouts
SUGGEST_TIMEOUT_SECONDS = 5.0
# SerpAPI calls include Google-side scraping on the provider's servers, so
# end-to-end latency is typically 2-6s; 15s leaves headroom for slow months.
SERPAPI_TIMEOUT_SECONDS = 15.0

# SerpAPI response cache TTL. Plan 004 Unit B: repeated (query, lang, country)
# within this window returns the cached payload and spends zero quota.
SERP_CACHE_TTL_SECONDS = 86400  # 24h

# Sweep threshold for `running` jobs left behind by a process crash (R14 tail cleanup).
ORPHAN_RUNNING_MINUTES = 30

# Sidebar page size (R14).
HISTORY_SIDEBAR_LIMIT = 50

# Current SQLite schema version. Bump + add migration when schema changes.
SCHEMA_VERSION = 1
