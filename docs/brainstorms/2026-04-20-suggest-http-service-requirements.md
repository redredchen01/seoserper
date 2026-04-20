---
date: 2026-04-20
topic: suggest-http-service
parent: 2026-04-20-suggest-only-pivot-requirements.md
trigger: need-agent-programmatic-suggest-access
---

# Suggest HTTP Service (SEOSERPER sub-feature)

## Problem Frame

SEOSERPER today is a single-user Streamlit tool that calls `seoserper.fetchers.suggest` in-process. Agents and internal tools in the YD 2026 workspace have no way to consume suggest keywords programmatically without bypassing SEOSERPER and hitting `suggestqueries.google.com` directly — each caller would reinvent timeouts, fallbacks, caching, rate limits, and parse-shape defenses of its own.

This brainstorm scopes a sub-feature inside the existing SEOSERPER repo: a small FastAPI service that fronts the same Google Suggest upstream used by the Streamlit UI, wraps it in the resilience machinery the original Node/TS brief specified, and exposes a single stable HTTP contract (`GET /v1/suggest`) that any caller can use safely. The upstream remains unofficial and unstable — the service is a **best-effort** layer, not a guaranteed one.

The service is a **sub-feature**, not a replacement. Streamlit keeps calling the existing fetcher directly; the HTTP surface is additive and shares the same upstream through the same fetcher module.

## Fallback Chain

```mermaid
flowchart TB
    A[GET /v1/suggest?q=...] --> RL{Rate limit<br/>caller quota ok?}
    RL -- no --> RL429[429 rate_limited]
    RL -- yes --> C{fresh=true?}
    C -- no --> L1{L1 SQLite cache<br/>hit?}
    L1 -- yes --> OK[200 cached hit<br/>provider_used=cache]
    L1 -- no --> CB{Google circuit<br/>closed?}
    C -- yes --> CB
    CB -- closed --> G[GoogleSuggestProvider<br/>defensive parse]
    CB -- open --> SD[StaticDictionaryProvider<br/>SQLite analysis_jobs]
    G -- ok --> WR[write cache + return<br/>provider_used=google]
    G -- fail --> SD
    SD -- ok --> WR2[return<br/>provider_used=static<br/>degraded=true]
    SD -- empty --> EMP[return []<br/>degraded=true<br/>warnings=upstream_unavailable]
```

## Requirements

**API Surface**

- R1. Expose `GET /v1/suggest` with query params `q` (required), `hl` (default `zh-TW`), `gl` (default `TW`), `provider` (default `auto`), `limit` (default 10, max 20), `fresh` (default false). Validate via Pydantic.
- R2. Return the normalized response shape from the brief (`query`, `normalized_query`, `suggestions`, `provider_used`, `provider_chain`, `locale`, `cache`, `meta`). Never leak the raw upstream array to callers.
- R3. Status codes: 400 invalid/missing `q`; 429 caller rate limited; 502 all providers failed AND no safe degraded result; 503 circuit breaker open for every available provider; 200 + `degraded=true` in all other upstream-failure cases.
- R4. Prefer returning a 200 with `degraded=true, suggestions=[]` over a 502 whenever the fallback chain can produce *any* safe answer (including the empty degraded result).
- R5. Expose a minimal `GET /healthz` that reports `ok` plus per-provider health booleans (derived from circuit breaker state). No auth required.

**Providers**

- R6. `GoogleSuggestProvider` wraps the existing `seoserper.fetchers.suggest` call path. Parsing is defensive: any change in upstream shape, any non-array-at-index-1 surprise, any JSON parse failure is classified as an upstream error — it does not leak to the caller.
- R7. `StaticDictionaryProvider` sources fallback suggestions from the existing SQLite `analysis_jobs` table — completed jobs whose `query` column contains or prefixes the incoming `q`, filtered to the requested locale when available.
- R8. Provider interface is a `SuggestProvider` Protocol with `name`, `is_healthy()`, `suggest(req)`. Adding `BingSuggestProvider` / `YouTubeSuggestProvider` later requires no core changes — drop in a new class and register it in the provider chain.
- R9. SerpAPI is NOT in this chain. The `source_serp="SerpAPI"` integration in the existing SEOSERPER engine serves PAA + Related; its quota economics (100/month free) and domain (rendered SERP, not autocomplete) make it the wrong tool for high-volume suggest fan-out.

**Fallback Chain**

- R10. Resolution order is: L1 SQLite cache → `GoogleSuggestProvider` → `StaticDictionaryProvider` → empty result with `degraded=true`.
- R11. `provider_chain` in the response echoes the full chain that was considered, not just the one that answered. `provider_used` names the one that produced the returned suggestions (or `"cache"` on L1 hit, or `"static"` on fallback, or `"none"` when empty+degraded).

**Resilience**

- R12. Circuit breaker on `GoogleSuggestProvider`: open on 5 consecutive failures OR >50% failure rate over a rolling sample of at least 20 requests. Stay open 2 minutes. Half-open admits a single probe; success closes, failure resets the 2-minute window.
- R13. Rate limit: default 60 req/min per caller identity; stricter limit (configurable, default 10 req/min) when `fresh=true`. Exceeding it returns 429 with standard `Retry-After`.
- R14. Rate limit caller identity for MVP = source IP; a future `X-Caller-Id` header or API key is a non-goal for this pass but the interface must not assume IP-only.
- R15. In-process request coalescing: identical concurrent requests (same cache key) dedupe to a single upstream call; waiters receive the same result.

**Caching**

- R16. Cache key composition: `provider + hl + gl + normalized_q + limit`. Query normalization lowercases, strips whitespace, collapses internal spaces, and NFKC-normalizes Unicode.
- R17. TTL 12h for successful upstream responses; 5min for upstream-empty results to throttle repeated misses without freezing bad data for long.
- R18. `fresh=true` skips the cache read but still writes on success, still applies rate limit + circuit breaker + coalescing.
- R19. Cache backend is SQLite — reuse the schema pattern from the existing `seoserper.fetchers.serp_cache` module, co-located in the same DB file. No Redis dependency.

**Observability**

- R20. Structured logs via `structlog` (or stdlib `logging` with JSON formatter), request-scoped `request_id` threaded through every log line and echoed in `meta.request_id` of the response.
- R21. Counter / histogram hooks (module-level functions with no-op default implementation) for: request count, success count, degraded count, provider error count by provider, cache hit ratio, p50/p95 latency, circuit-open events, empty-result count. MVP emits them as structured log fields only; a Prometheus exporter is a non-goal for this pass.

**Integration with existing SEOSERPER**

- R22. The Streamlit UI does NOT migrate to call this HTTP service. Streamlit and the HTTP service are peers; both import `seoserper.fetchers.suggest` directly. The HTTP service adds its protection layers on top before calling through.
- R23. The service is a subpackage under the `seoserper` package so it ships and versions with the rest of SEOSERPER. No separate repo, no separate `pyproject.toml`. Exact subpath (e.g. `seoserper/service/` vs `seoserper/api/`) is a planning detail.
- R24. The service and Streamlit can run in the same venv and against the same SQLite DB file. Startup is two independent processes (one `streamlit run`, one `uvicorn`).

## Success Criteria

- An agent can call `GET /v1/suggest?q=台北&hl=zh-TW&gl=TW` against `localhost:<port>` and receive the normalized JSON contract.
- When `suggestqueries.google.com` begins returning non-2xx or a changed shape, the service degrades gracefully: callers see `degraded=true` with static-fallback suggestions or an empty array, never a 502 unless the static fallback is also genuinely unavailable.
- Over a 100-request agent batch against a repeating keyword set, cache hit ratio ≥ 60% (measurable via R21 metrics).
- Circuit-breaker tests deterministically open after 5 forced upstream failures and close after one successful half-open probe.
- P95 latency < 150ms on cache hit, < 1.2s on fresh upstream call (measured against the home IP's observed Google Suggest latency).

## Scope Boundaries

**Explicitly dropped from the original brief (tech stack):**
- Node.js / TypeScript / Fastify / Zod / pino / Vitest — replaced with Python / FastAPI / Pydantic / structlog / pytest to live inside SEOSERPER.
- Redis — replaced with SQLite reuse of the existing `serp_cache` pattern.

**Non-goals for MVP:**
- Auth (401 path). The service binds to localhost or a trusted internal interface for MVP; an API-key / bearer-token hook is shaped by R14 but not wired.
- Prometheus / OpenTelemetry exporter. Log-line metrics only.
- `BingSuggestProvider` / `YouTubeSuggestProvider`. Provider interface supports them; no MVP implementation.
- Admin UI / dashboard. FastAPI's built-in `/docs` (OpenAPI) is sufficient.
- Place / address / business autocomplete — out of scope by product definition (belongs to a different official provider).
- Agent SDK / client library. Callers talk plain HTTP.
- Streamlit UI changes of any kind.

**Carried forward from the suggest-only-pivot brainstorm:**
- Home-IP `suggestqueries.google.com` remains the only live upstream. If that endpoint's availability regresses, this service is immediately affected — the kill criterion from the parent doc (`overall_status=failed` rate >20% in a rolling 20-query window) also applies here.
- Locale commitment stays the Streamlit-MVP set: `en-US / zh-CN / zh-TW / ja-JP` are the quality-validated locales; others work but are not tuned.

## Key Decisions

- **Sub-feature of SEOSERPER, not a standalone repo.** Lives under the existing `seoserper` package, versions with it, shares its SQLite file.
- **Python / FastAPI, not Node / TS.** The brief's architecture concepts (provider abstraction, fallback chain, caching, rate limit, circuit breaker, coalescing) all port cleanly; the runtime substitution avoids a polyglot monorepo and reuses existing fetcher + storage code.
- **Provider chain = Google Suggest → Static (SQLite history) → Empty.** SerpAPI stays out of the suggest path — it's an economic and domain mismatch for autocomplete fan-out.
- **Static fallback sources from `analysis_jobs` history, not hand-curated seeds.** The history grows naturally with real usage, avoids stale seed lists, and aligns fallback quality with the user's own query gravity. Cold start will return empty + degraded, which is acceptable MVP behavior.
- **Cache + rate-limit storage = SQLite.** Reuses the `serp_cache` schema pattern. No Redis, no extra service to run. Cross-process coalescing is not a design goal; in-process `asyncio.Lock` suffices for MVP.
- **Streamlit stays parallel, not migrated.** Two callers of the same fetcher module. No UI rewrite, no risk to the existing self-use flow.
- **MVP has no auth.** 401 path exists in the error taxonomy for future, but the service binds locally and trusts its caller.

## Dependencies / Assumptions

- `seoserper.fetchers.suggest` stays importable with its current signature. Any breaking change to that module must land with a coordinated update to both Streamlit and the HTTP service (they share it).
- SQLite `analysis_jobs` table keeps its `query`, `lang`, `country`, `overall_status` columns. The static fallback reads these; a schema change requires a migration to the static provider's query.
- Google's `suggestqueries.google.com` remains reachable from the home IP (validated 2026-04-20, 30/30 ok across `en-US / zh-CN / ja-JP`). Kill criterion inherited from the parent doc.
- `httpx` is acceptable as a new dependency for the FastAPI async code path. The existing `requests`-based fetcher stays for Streamlit; the HTTP service gets an async-safe wrapper.

## Outstanding Questions

### Resolve Before Planning

(none — all product decisions resolved in this brainstorm)

### Deferred to Planning

- [Affects R19][Technical] SQLite schema for the two new tables (`suggest_cache`, `rate_limit_bucket`): column set, index strategy, idempotent migration pattern matching existing `storage.init_db` conventions, and whether they live in the same `seoserper.db` file or a dedicated one.
- [Affects R6][Technical] Whether `GoogleSuggestProvider` wraps the existing sync `seoserper.fetchers.suggest` via a thread-pool executor or introduces a parallel `httpx.AsyncClient`-based implementation. Sharing the module vs duplicating the endpoint knowledge is the tradeoff to settle.
- [Affects R7][Technical] Exact match algorithm for static fallback: case-insensitive prefix, SQLite `LIKE %q%`, or trigram-like fuzzy. Also: sort order (recency vs frequency), locale-filter fallback behavior when zero matches, and the limit cap when the bucket is sparse.
- [Affects R21][Needs research] Metrics emission: keep it log-only via structlog fields, or register a `prometheus_client` registry now that's unexported until a future endpoint is wired. Weight simplicity against future wiring cost.
- [Affects R23, R24][Technical] Code layout inside `seoserper/`: `seoserper/service/` vs `seoserper/api/` vs `seoserper/suggest_http/`. Also: how the user starts the service (a new `scripts/run_suggest_service.sh`, a console-script entry in `pyproject.toml`, or a README command).
- [Affects R14][Technical] Rate-limit key abstraction shape: design the `CallerIdentity` resolver so IP works today and swapping in `X-Caller-Id` / API key is a single-point change, without wiring auth now.
- [Affects R15][Technical] Coalescing correctness under partial failures: if the upstream call raises mid-flight, do all waiters see the same exception, or does one retry? Design the single-flight map so a failed leader doesn't poison the window.

## Next Steps

→ `/ce:plan` for the Suggest HTTP Service implementation (FastAPI app, provider module, SQLite-backed cache + rate limiter, circuit breaker, coalescing, tests). Estimated 1–2 medium units of work; no new top-level modules outside the `seoserper` package.
