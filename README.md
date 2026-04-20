# SEOSERPER

Local-first Google SERP analyzer. Pulls **Autocomplete Suggestions**, **People Also Ask**, and **Related Searches** for any query, saves history to SQLite, exports Markdown or CSV.

Solo-operator tool, self-use. No account, no cloud, no CI.

## Modes

| Mode | Trigger | Surfaces |
|------|---------|----------|
| **Full** | `SERPAPI_KEY` set | Suggestions (free) + PAA + Related (via SerpAPI, 1 search/submit) |
| **Suggest-only** | `SERPAPI_KEY` unset (default) | Suggestions only — no quota cost |

SerpAPI free tier is 100 searches/month, no credit card. See `seoserper/config.py` module docstring for the full setup + locale + quota details.

## Quick start

```bash
# 1. Clone + deps (Python 3.10+ required)
pip install -e .

# 2. Optional: configure SerpAPI for Full mode
cp .env.example .env          # edit and paste your key
export $(grep -v '^#' .env | xargs)

# 3. Run
streamlit run app.py
```

Open http://localhost:8501. Pick a locale (English / 简体中文 / 繁體中文 / 日本語), submit a query.

## Export formats

| Format | Content |
|--------|---------|
| Markdown | H1 + frontmatter + sections — paste into Notion / 飞书 / VS Code |
| CSV | Flat `surface,rank,text,answer_preview` rows — open in Excel / LibreOffice (UTF-8 BOM for zh/ja compat) |

## Cache

Full-mode responses are cached by `(query, lang, country)` for 24 hours. Repeat queries don't burn SerpAPI quota. Reset / prune:

```bash
python3 scripts/reset_serp_cache.py              # nuke all
python3 scripts/reset_serp_cache.py --prune-only # drop only expired
```

## Tests

```bash
pip install -e '.[dev]'
pytest tests/ -q
```

## Plans

Design history lives under `docs/plans/` — active plan is the highest-numbered `status: active` file. Completed plans stay for context.
