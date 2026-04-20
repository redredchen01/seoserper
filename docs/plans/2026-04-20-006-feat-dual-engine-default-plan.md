---
title: "feat: Dual-engine default — one Submit fires Google + Bing in parallel"
type: feat
status: active
date: 2026-04-20
origin: conversation-driven (user ask "整合后预设就是会吐出 google+bing related searches")
supersedes_none: true
---

# Dual-Engine Default

## Overview

Default the UI to "Google + Bing" — a single Submit creates TWO parallel jobs (one per engine) and the main view renders both side-by-side. Single-engine submits stay available as non-default radio options. Zero storage schema change: two separate job rows, paired only in session state.

## Scope

- Radio gains a 3rd option: **"Google + Bing 对比"** (new default).
- Submit in compare mode: fires `engine.submit(engine="google")` + `engine.submit(engine="bing")` back-to-back; both workers run concurrently (existing engine threading).
- Main view: when `_pair_job_ids` is set, render two `st.columns` side-by-side, each containing the per-engine surfaces.
- History sidebar: shows the 2 rows as normal (no new "pair" grouping). A subtle dot marker can distinguish them but not required for MVP.
- 🔄 re-run on a paired-mode history row: re-runs only that single job (not the pair).
- Single-engine submits (Google-only, Bing-only) preserve existing behavior — same radio, just not the default.

## Non-goals

- No cross-engine deduplication or merging at the data layer.
- No "pair" foreign key in storage — purely session-state-level pairing.
- No retry logic for the pair as a unit — individual 🔄 per job covers the retry path.
- No MD/CSV export that merges both engines into one file — export each separately.

## Cost

Each "Google + Bing 对比" submit = 2 SerpAPI credits (1 per engine). User's ~250/mo quota → ~125 full compare submits/mo.

## Implementation

1. **Radio 3-option + default "both"** (app.py) — radio label "搜索引擎", options `["Google + Bing 对比", "Google", "Bing"]`, index=0 default. Map to internal value `"both" | "google" | "bing"`.
2. **Compare-mode submit** (app.py) — when internal value is `"both"`: call `engine.submit(query, lang, country, engine="google")` and `engine.submit(query, lang, country, engine="bing")`; stash `_pair_job_ids = (g_id, b_id)` in session state.
3. **Side-by-side render** (app.py) — new `_render_pair(g_job, b_job)` that lays out 2 `st.columns`, each calling the existing `_render_surface` loop for its engine's surfaces.
4. **Pair-aware progress polling** (app.py) — `_drain_progress` checks both pair jobs for RUNNING state, keeps rerun loop alive until both complete.
5. **Cache bypass + pair** — the "忽略缓存" checkbox invalidates both engines' cache keys when pair mode fires.
6. **Tests** — UI smoke for radio default, both submit creates 2 jobs, pair render shows 2 columns, bypass invalidates both keys.

## Tests

- Radio default is "Google + Bing 对比"
- Submit in compare mode creates 2 job rows (one google + one bing) within milliseconds
- When `_pair_job_ids` set, main view renders 2 columns (not 1)
- 🔄 on a paired history row re-runs single engine (pair not re-triggered)
- Bypass-cache prep invalidates BOTH engine keys when compare mode

## Verification

- Full suite green
- Live: compare submit on `coffee en-US` → 2 history rows appear, side-by-side view shows both engines' data (Google: 3 surfaces; Bing: 2 surfaces, PAA/Related possibly empty)
- Cost check: quota drops by 2 (1 Google + 1 Bing) per compare submit
