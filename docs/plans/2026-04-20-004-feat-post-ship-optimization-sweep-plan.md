---
title: "feat: Post-ship optimization sweep — parallel, cache, deadcode, UI polish, docs"
type: feat
status: completed
date: 2026-04-20
origin: conversation-driven
supersedes_none: true
completed_commits: 516f20c 3e04cc6 8ce5473 fc5b9be
---

# Post-Ship Optimization Sweep

## Overview

Plan 003 shipped SerpAPI integration. This plan lands 5 bounded optimizations on top: parallel fetch, response cache, dead-code sweep, UI polish, and docs/CSV. Each unit is independently shippable and revertible. No architectural changes.

## Requirements Trace

- **R-A** Suggest + SerpAPI run concurrently under full mode — observed wall-clock per Submit drops from ~5s to ~3s.
- **R-B** Repeated identical `(query, lang, country)` within TTL hits cache, zero SerpAPI credit spent.
- **R-C** `FailureCategory.BLOCKED_BY_CAPTCHA / BLOCKED_BY_CONSENT / BROWSER_CRASH` are removed from the enum + UI + export. These values are now unproducible after plan 003.
- **R-D** Language + country become `st.selectbox` dropdowns driven by `config.SUPPORTED_LOCALES`. SerpAPI quota remaining surfaces as a small caption on Full mode boot.
- **R-E** `.env.example` template exists, `README.md` documents quick start, CSV export available alongside MD.

## Scope Boundaries

- Cache is query-level only (not per-surface). Skipping cache when query differs by even one character.
- No cache-warm / prefetch. Manual cache reset via CLI script only.
- SerpAPI quota check is one-shot at app boot, not live-updating per query.
- CSV export is a single flat file; no zip/bundle.
- README is deliberately minimal (solo-op tool).

## Implementation Units

### Unit C: Dead-code sweep (smallest; ship first)

**Goal:** Remove `FailureCategory.BLOCKED_BY_CAPTCHA / BLOCKED_BY_CONSENT / BROWSER_CRASH` and all references.

**Files:**
- Modify: `seoserper/models.py` — drop 3 enum values
- Modify: `app.py` — drop 3 entries from `_FAILURE_MSG`
- Modify: `seoserper/export.py` — drop 3 entries from `FAILURE_DIAGNOSTIC_ZH`
- Modify: `seoserper/core/engine.py` — `_mark_running_surfaces_failed` uses `BROWSER_CRASH` for the safety net; swap to `NETWORK_ERROR`
- Test scenarios:
  - `FailureCategory` enum has exactly 3 values (`BLOCKED_RATE_LIMIT`, `SELECTOR_NOT_FOUND`, `NETWORK_ERROR`)
  - Unhandled engine exception test still terminates cleanly (use NETWORK_ERROR)

**Verification:** Full suite green; `grep BLOCKED_BY_CAPTCHA seoserper/ tests/ app.py` → 0 hits.

### Unit A: Parallel Suggest + SerpAPI

**Goal:** `_run_analysis` dispatches Suggest + SerpAPI concurrently instead of serially.

**Files:**
- Modify: `seoserper/core/engine.py` — use `concurrent.futures.ThreadPoolExecutor(max_workers=2)` inside `_run_analysis` when both `run_suggest` and `run_serp` are True; single-path when only one runs.
- Test: `tests/test_engine.py` — new scenario asserts wall-clock is ~ max(suggest, serp) not sum

**Approach:**
- Keep existing `_do_suggest` / `_do_serp` untouched. Submit both to a local executor; `futures.wait(return_when=ALL_COMPLETED)`.
- Progress events still emit from each method; ordering may interleave but test drains are tolerant.
- DB writes via `update_surface` already acquire a connection per call — no new concurrency concern on the SQLite side; the existing WAL-mode + BEGIN IMMEDIATE handles cross-thread surface writes.
- Retry path (`retry_failed_surfaces`) gets the same parallel treatment.

**Verification:**
- Synthetic test: inject slow suggest (sleep 0.5s) + slow serp (sleep 0.5s) → total wall-clock < 0.9s (not 1.0s)
- All pre-existing engine tests still pass

### Unit B: SerpAPI response cache

**Goal:** Cache OK responses keyed by `(query, lang, country)` with TTL=24h. Repeat queries hit cache, save free-tier credits.

**Files:**
- Modify: `seoserper/storage.py` — add `serp_cache` table to SCHEMA + idempotent migration. Columns: `cache_key TEXT PK`, `response_json TEXT`, `created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP`.
- Modify: `seoserper/storage.py` — CRUD: `cache_get(cache_key, ttl_seconds) -> dict|None`, `cache_put(cache_key, response_json)`, `cache_prune(ttl_seconds)`.
- Create: `seoserper/fetchers/serp_cache.py` — thin wrapper around `fetch_serp_data` that reads/writes via storage. Exposes `fetch_serp_data_cached(query, lang, country, *, api_key) -> dict[SurfaceName, ParseResult]`.
- Modify: `seoserper/config.py` — add `SERP_CACHE_TTL_SECONDS = 86400` (24h default).
- Modify: `app.py` — `_boot_engine` uses `fetch_serp_data_cached` instead of `fetch_serp_data`.
- Create: `scripts/reset_serp_cache.py` — opt-in utility to nuke cache.
- Test: `tests/test_serp_cache.py` — 8+ scenarios.

**Approach:**
- `cache_key = f"{query}|{lang}|{country}"` (no hashing — SQLite PK handles uniqueness; ASCII-debuggable).
- Cache only OK responses. Failures (rate-limit, network, quota-exhausted) are NOT cached — next call retries.
- "OK response" definition: both PAA and Related are `status ∈ {OK, EMPTY}`. If either is FAILED, don't cache.
- Prune on `cache_put` (remove rows older than TTL) — cheap, keeps table size bounded.
- Cache stores raw SerpAPI JSON response, NOT the `dict[SurfaceName, ParseResult]`. Rationale: if extract logic changes later, cached rows stay replayable. Downside: small CPU cost on each hit to re-extract — negligible (<1ms per call).

**Verification:**
- Test: first call → HTTP made + row inserted; second call within TTL → no HTTP + same result
- Test: TTL expired → row dropped, HTTP made
- Test: failed response → NOT cached
- Test: different lang on same query → separate cache rows
- Test: cache_prune clears old entries but keeps fresh

### Unit D: UI polish — locale dropdowns + quota display

**Goal:** Language + country become `selectbox` dropdowns. SerpAPI quota remaining shows as top caption in Full mode.

**Files:**
- Modify: `app.py` — input row replaces text_input with selectbox for lang/country; add `_fetch_quota_info()` helper that calls `https://serpapi.com/account?api_key=...` and parses `plan_searches_left`; display as caption below mode notice in Full mode.
- Modify: `seoserper/config.py` — export `SUPPORTED_LOCALES` as `[(lang, country, label), ...]` with human labels (e.g., "English (US)", "简体中文 (CN)", "日本語 (JP)").
- Create: `seoserper/serpapi_account.py` — lightweight helper to fetch account info; mirrors `fetchers/` pattern; never raises (returns None on failure).
- Modify: `tests/test_ui_smoke.py` — assertions update: `selectbox` present, quota caption appears when key set.
- Create: `tests/test_serpapi_account.py` — mock account-endpoint scenarios.

**Approach:**
- Dropdowns are strictly for the MVP-scope locales (en-us, zh-cn, ja-jp). User can still run other locales via direct DB manipulation but the UI is opinionated.
- Quota fetch is cached in session_state for the whole Streamlit session (no live per-query refresh; SerpAPI's dashboard is the source of truth for exact count). One call per boot.
- Quota fetch silently no-ops when SERPAPI_KEY unset or the account endpoint errors — UI never blocks on this.

**Verification:**
- Dropdowns render with 3 options each
- Quota caption shows "SerpAPI 剩余 N/100" when key works; absent when key unset; absent on HTTP error

### Unit E: `.env.example` + README + CSV export

**Goal:** New user onboarding docs + CSV export alongside MD.

**Files:**
- Create: `.env.example` — one line `SERPAPI_KEY=` (blank value)
- Create: `README.md` — minimal: what SEOSERPER does, env setup, 3-line run instructions, link to `seoserper/config.py` for details
- Modify: `seoserper/export.py` — add `render_analysis_to_csv(analysis) -> str` pure function; one flat CSV with columns `[surface, rank, text, answer_preview]`
- Modify: `app.py` — add second download button next to MD: "📊 导出 CSV"
- Create: `tests/fixtures/export/expected_all_ok.csv` — golden
- Modify: `tests/test_export.py` — add CSV scenarios (happy path, empty surfaces, suggest-only)

**Approach:**
- CSV uses Python's `csv.writer` with `\r\n` line endings (RFC 4180) for Excel compat.
- Column layout: `surface` (suggest/paa/related), `rank` (int), `text` (query or question), `answer_preview` (PAA only, empty for others).
- No frontmatter in CSV (unlike MD) — first row is header, subsequent rows are data.
- Golden fixture per-byte equality gates the format.

**Verification:**
- `.env.example` exists, readable, single line
- `README.md` exists, Chinese + English mix per repo style
- CSV export produces a well-formed CSV that opens cleanly in Excel / LibreOffice (sanity check via Python `csv.reader` roundtrip in test)

## Dependencies

```
C (deadcode)  →  independent, lands first
A (parallel)  →  depends on C (engine touches _mark_running_surfaces_failed)
B (cache)     →  depends on A (wraps fetcher, engine still uses fetch_fn signature)
D (ui polish) →  independent of B; depends on config changes
E (docs+csv)  →  independent, lands last
```

Execution order: C → A → B → D → E. Each commits atomically.

## Scope Boundaries (explicit non-goals)

- No per-surface cache (PAA-only cache without Related). Too granular for self-use.
- No cache TTL per-locale (ja-JP might drift faster than en-US). Single 24h TTL.
- No SerpAPI account endpoint 500-retry. Silently skip quota display on error.
- No migration of existing full-mode historical rows to populate cache table.

## Risks

| Risk | Mitigation |
|------|------------|
| Parallel engine races on progress_queue ordering breaks UI rerun logic | `_drain_progress` already tolerates reordering (drains until terminal event). Tests simulate interleaved events. |
| Cache stores stale data when SerpAPI response evolves (e.g., new fields) | Raw JSON stored; extraction re-runs on each hit. Schema drift surfaces as normal fetcher failure. |
| Quota endpoint silently fails, UI shows nothing | Acceptable; user can check serpapi.com dashboard. No hard dep. |
| CSV encoding for zh-CN / ja-JP breaks Excel | Write UTF-8 with BOM prefix (Excel Chinese compat). Test golden locks the prefix. |

## Sources

- Plan 003 (just shipped): `docs/plans/2026-04-20-003-feat-serpapi-integration-full-serp-plan.md`
- SerpAPI account endpoint: https://serpapi.com/account-api
- CSV UTF-8 BOM for Excel: de-facto standard, RFC 4180 doesn't mandate but Microsoft expects it
