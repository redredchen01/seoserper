# SEOSERPER Spike Execution Plan

**Date:** 2026-04-20  
**Duration:** 3 days (2026-04-20 to 2026-04-22)  
**Goal:** Validate Google SERP data collection feasibility (≥80% success rate)

---

## Overview

This 3-day spike tests whether we can reliably extract Google SERP data (Suggestions/PAA/Related) using Playwright across 3 language variants. The spike produces:
- ≥60 real Google queries (distributed across 3 days)
- HTML fixtures for en-US, zh-CN, ja-JP
- Failure taxonomy classification (6 categories)
- Wilson CI confidence interval on success rate
- Ship/No-Ship decision

---

## Pre-Requisites

### ✅ Environment Setup

```bash
cd /Users/dex/YD\ 2026/SEOSERPER
python3 -m venv .venv
source .venv/bin/activate
pip install playwright requests beautifulsoup4
playwright install chromium
```

### ✅ Verify Setup

```bash
python scripts/spike.py --help
# Should show: run, analyze subcommands
```

---

## Execution Plan

### Day 1: en-US Baseline (2026-04-20)

**Morning (9:00-11:00 UTC)**
```bash
# Burst 1: 5 queries
python scripts/spike.py run --locale en-us --limit 5 --pacing 90
# Expected: 5 outcomes logged to spike_results.jsonl

# Monitor: Check spike_results.jsonl (should have 5 lines)
tail -5 scripts/spike_results.jsonl | jq .status
```

**Afternoon (14:00-16:00 UTC)**
```bash
# Burst 2: 5 queries
python scripts/spike.py run --locale en-us --limit 5 --pacing 90

# Cumulative: 10 outcomes
```

**Evening (19:00-21:00 UTC)**
```bash
# Burst 3: 5 queries
python scripts/spike.py run --locale en-us --limit 5 --pacing 90

# Day 1 Total: 15 queries
# Check progress:
grep '"locale": "en-us"' scripts/spike_results.jsonl | wc -l
# Should output: 15
```

**Daily Debrief (Day 1 Evening)**
```bash
# Interim analysis (not final)
python scripts/spike.py analyze --limit-to-locale en-us

# Expected output:
# - Success rate (target: ≥70%)
# - Failure breakdown (captcha %, consent %, selector %, etc.)
# - Fixture count
```

---

### Day 2: zh-CN Variant (2026-04-21)

**Morning (9:00-11:00 UTC)**
```bash
# Burst 1: 5 queries (zh-CN)
python scripts/spike.py run --locale zh-cn --limit 5 --pacing 90

# Note: May encounter different selector behavior (Chinese Google)
# If blocked → log failure, don't retry same query
```

**Afternoon (14:00-16:00 UTC)**
```bash
# Burst 2: 5 queries
python scripts/spike.py run --locale zh-cn --limit 5 --pacing 90
```

**Evening (19:00-21:00 UTC)**
```bash
# Burst 3: 5 queries
python scripts/spike.py run --locale zh-cn --limit 5 --pacing 90

# Day 2 Total: 15 queries (zh-CN)
# Cumulative: 30 queries (en-us + zh-cn)
```

**Daily Debrief (Day 2 Evening)**
```bash
# Analyze both locales
python scripts/spike.py analyze

# Compare: en-us vs zh-cn success rates
# Expected: ±5% variance acceptable
```

---

### Day 3: ja-JP Variant + Final Gate (2026-04-22)

**Morning (9:00-11:00 UTC)**
```bash
# Burst 1: 5 queries (ja-JP)
python scripts/spike.py run --locale ja-jp --limit 5 --pacing 90
```

**Afternoon (14:00-16:00 UTC)**
```bash
# Burst 2: 5 queries
python scripts/spike.py run --locale ja-jp --limit 5 --pacing 90
```

**Evening (19:00-21:00 UTC)**
```bash
# Burst 3: 5 queries
python scripts/spike.py run --locale ja-jp --limit 5 --pacing 90

# Day 3 Total: 15 queries (ja-JP)
# Grand Total: 45 queries (could extend to 60 if time permits)
```

**Final Analysis (Day 3 Evening)**
```bash
python scripts/spike.py analyze

# Expected output: Wilson CI + Ship decision
# - SHIP: success_rate ≥80%, all locales ≥70%
# - NOTE-AND-SHIP: success_rate 60-80%, document limitations
# - BRAINSTORM: success_rate <60%, need different approach
```

---

## Success Criteria

### Gate 1: Overall Success Rate
- **Target:** ≥80% of 60 queries succeed (48+ successes)
- **Definition:** Status == "ok" (data extracted, no blocking)
- **Pass Condition:** Proceed to MVP development immediately

### Gate 2: Per-Locale Success
- **Target:** Each locale (en-US, zh-CN, ja-JP) ≥70%
- **Purpose:** Verify selector stability across language variants
- **Pass Condition:** Helps prioritize which locales to test first in MVP

### Gate 3: Failure Taxonomy
- **Target:** Classify all failures into 6 categories (see below)
- **Purpose:** Understand what error handling MVP needs to implement
- **Pass Condition:** >80% of failures fit one of 6 categories

### Gate 4: Fixtures Collected
- **Target:** ≥15 unique HTML fixtures per locale
- **Purpose:** Code regression testing (frozen selectors)
- **Pass Condition:** Tests/fixtures/serp/{locale}/ populated

---

## Failure Taxonomy (6 Categories)

When a query fails (status != "ok"), classify as one of:

1. **blocked_by_captcha**
   - Pattern: "Please prove you're not a bot" or reCAPTCHA frame visible
   - Action: Log it, don't retry
   - Expected frequency: 5-15% (Google rate-limiting)

2. **blocked_by_consent**
   - Pattern: "Before you continue" or GDPR consent banner
   - Action: Log it, don't retry (MVP can handle with --accept-consent flag)
   - Expected frequency: 0-5%

3. **blocked_rate_limit**
   - Pattern: HTTP 429 or "Try again later"
   - Action: Log it, implement exponential backoff in MVP
   - Expected frequency: 0-10%

4. **selector_not_found**
   - Pattern: PAA/Related section not found, or partial extraction
   - Action: Log selector path, fixture HTML for debugging
   - Expected frequency: 0-15% (Google layout varies)

5. **network_error**
   - Pattern: Timeout, DNS error, connection refused
   - Action: Log error message
   - Expected frequency: 0-5%

6. **browser_crash**
   - Pattern: Playwright process dies or page unresponsive
   - Action: Log, restart browser
   - Expected frequency: 0-3%

---

## Daily Monitoring

### Each Burst (3-5 queries)

```bash
# After each burst, verify:
tail -5 scripts/spike_results.jsonl | jq '{timestamp, locale, query, status}'

# Expected output (sample):
# {
#   "timestamp": "2026-04-20T09:15:00Z",
#   "locale": "en-us",
#   "query": "how to learn python",
#   "status": "ok"
# }
```

### End of Day

```bash
# Count outcomes by status
python -c "
import json
outcomes = [json.loads(line) for line in open('scripts/spike_results.jsonl')]
by_status = {}
for o in outcomes:
    by_status[o['status']] = by_status.get(o['status'], 0) + 1
print(json.dumps(by_status, indent=2))
"

# Expected breakdown (target):
# {
#   "ok": 48,
#   "blocked_by_captcha": 8,
#   "blocked_by_consent": 2,
#   "network_error": 2
# }
```

---

## Decision Gate Output

After 3 days and ≥60 queries, run final analysis:

```bash
python scripts/spike.py analyze
```

Expected output format:

```
SPIKE RESULTS (60 queries over 3 days)
=====================================

Overall Success Rate: 82% (49/60)
  Wilson CI (95%): [72%, 90%]
  Confidence: STRONG (>30 samples)

Per-Locale Breakdown:
  en-us: 87% (13/15) ✅ PASS
  zh-cn: 80% (12/15) ✅ PASS
  ja-jp: 80% (12/15) ✅ PASS

Failure Taxonomy:
  blocked_by_captcha: 7 (11.7%)
  blocked_by_consent: 2 (3.3%)
  selector_not_found: 2 (3.3%)
  network_error: 2 (3.3%)
  browser_crash: 0 (0%)

Fixtures Collected:
  en-us: 13 HTML files
  zh-cn: 12 HTML files
  ja-jp: 12 HTML files

RECOMMENDATION: ✅ SHIP
- Success rate 82% ≥ 80% target
- All locales ≥ 70%
- Failure taxonomy well-understood
- MVP can implement MVP now

Next: Start Unit 1 (schema + storage) immediately
```

---

## Troubleshooting

### If Blocked by Captcha Frequently (>20%)

- **Sign**: More than 1 in 5 queries blocked
- **Cause**: Google rate-limiting (too many requests)
- **Fix**: Increase `--pacing` to 120+ (2+ min between requests)
- **Alternative**: Add proxy rotation (out of MVP scope, note for Phase 2)

### If Selector Not Found (>15%)

- **Sign**: PAA or Related section missing from parsed HTML
- **Cause**: Google layout change or locale-specific differences
- **Action**: Inspect fixture HTML, debug selector with `--no-headless`
- **MVP Impact**: Add fallback extractors (document in Unit 4)

### If Network Errors (>5%)

- **Sign**: Timeouts or DNS failures
- **Cause**: ISP blocking, network instability, or Google IP ban
- **Fix**: Increase `--pacing`, check ISP, try VPN
- **MVP Impact**: Implement retry with exponential backoff (Unit 5)

### If Browser Crashes (>2%)

- **Sign**: "Browser context was closed" errors
- **Cause**: Memory leak or Playwright incompatibility
- **Fix**: Update Playwright: `pip install --upgrade playwright`
- **MVP Impact**: Add graceful restart logic (Unit 5)

---

## Daily Wrap-Up Template

Create `SPIKE_DAY{N}_SUMMARY.md` at end of each day:

```markdown
# Day 1 Summary (2026-04-20)

## Execution
- Burst 1 (9:00): 5 queries ✅
- Burst 2 (14:00): 5 queries ✅
- Burst 3 (19:00): 5 queries ✅
- Total: 15 queries

## Results
- Success: 13/15 (87%)
- Blocked by captcha: 2
- Fixtures: 13 HTML files saved

## Observations
- en-us Google responsive, selectors stable
- All 3 PAA sections extracted successfully
- Related searches sometimes partial (low count)

## Day 2 Plan
- Start zh-CN testing
- Monitor for consent banner
- Expect similar success rate

## Risks
- None yet
```

---

## Signal to MVP Team

**Approval Trigger (Manual):**

When `python scripts/spike.py analyze` outputs one of:

```
RECOMMENDATION: ✅ SHIP
```

Send signal to resume MVP Unit 1 development:

```bash
# From SEOSERPER root
echo "Spike gate passed. Starting MVP Phase 1 (Units 1-8)." > SPIKE_GATE_PASSED
git add SPIKE_GATE_PASSED && git commit -m "spike: gate passed, ready for MVP"
```

**Deferred Trigger (Manual):**

If analysis outputs:

```
RECOMMENDATION: ⏳ NOTE-AND-SHIP
```

Proceed to MVP with documented limitations (e.g., "captcha handling omitted from Phase 1").

**Stop Trigger (Manual):**

If analysis outputs:

```
RECOMMENDATION: 🔄 BRAINSTORM
```

Schedule architecture review before proceeding.

---

## Timeline Summary

| Day | Locale | Queries | Cumulative | Gate |
|-----|--------|---------|-----------|------|
| 1 | en-us | 15 | 15 | Check en-us baseline |
| 2 | zh-cn | 15 | 30 | Check locale parity |
| 3 | ja-jp | 15 | 45 | **Final decision** |
| Optional | Mixed | 15 | 60 | Higher confidence |

---

## Reference

- **Plan:** `docs/plans/2026-04-20-001-feat-google-serp-analyzer-mvp-plan.md`
- **Requirements:** `docs/brainstorms/2026-04-20-google-serp-analyzer-requirements.md`
- **Spike Script:** `scripts/spike.py`
- **Keywords:** `scripts/spike_keywords.txt` (auto-loaded)
- **Results:** `scripts/spike_results.jsonl` (appended to)
- **Fixtures:** `tests/fixtures/serp/{locale}/*.html`

---

**Status: 🔄 SPIKE IN PROGRESS**

Execution starts 2026-04-20. Final recommendation expected 2026-04-22 evening.
