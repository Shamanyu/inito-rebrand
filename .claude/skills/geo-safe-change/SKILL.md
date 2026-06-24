---
name: geo-safe-change
description: Read FIRST before adding or changing any feature in the Inito GEO monitor (pipeline.py / config.json / tests). Encodes the repo's hard invariants, where everything lives, and the required verify loop so a change never breaks the two tracks, the lean output sheets, or the offline tests. Use whenever the task touches discovery, classification, the output sheets, the CLI, or config.
---

# GEO — safe-change guardrails

The umbrella skill. Start here, then jump to the specific workflow:
`geo-add-source` (Track A actor), `geo-add-llm-surface` (Track B), `geo-tune-classifier`
(claim regex + judge), `geo-dry-run` (hermetic end-to-end check).

Authoritative context: `docs/REQUIREMENTS.md`, `docs/DESIGN.md`, `CLAUDE.md`.

## What the system is (one breath)

A **per-run snapshot** generator for the brand **Inito**. Two tracks in `pipeline.py`:
**A — Web/SERP** (Google SERP+News+Ads+Reddit → crawl → judge) writes `web_observations.csv`;
**B — LLM visibility** (live-web ChatGPT + Perplexity → judge) writes `llm_observations.csv`. The judge
writes a plain-language **narrative** of what each source says about Inito (+ competition, links, price,
sentiment); for LLM answers it also captures mentioned/rank/recommended. Outputs are **CSV only**, one
self-contained timestamped folder per run. **There is no time series** — nothing accumulates across runs
except `fetch_cache.csv` (a cost saver).

## Hard invariants — do not break

1. **Snapshot, not series.** Do NOT reintroduce history/metrics/diff files or any cross-run state. Each
   run folder is self-contained; the only persistent file is `data/fetch_cache.csv`.
2. **Two lean sheets, fixed columns.** `WEB_COLUMNS` / `LLM_COLUMNS` (top of `pipeline.py`) are the
   output contract — the writers reindex to them and tests assert it. Add a column by extending the list
   **and** the row builder (`classify_web_record` / `_llm_row`) **and** a test.
3. **The Claude judge writes the narrative; regex only hints.** `claim_patterns` are a cheap pre-filter
   feeding the judge + its offline fallback (price + "still describes the old product"). Never make regex
   the final call.
4. **Never call shared attributes noteworthy.** Four hormones (E3G/LH/PdG/FSH), the companion app, the
   dip-strip workflow, accuracy — common to *both* products. Only phone-dependence (iPhone-only, clip,
   camera, Lightning port, no Android) is the OLD product worth flagging in `says_about_inito`.
5. **Cited/owned URLs are canonical.** `normalize_url` strips tracking params (`utm_*`, `disc_code`,
   `os`, `workflow`, fragment) so citations dedupe and stay clickable — keep it that way.
6. **Fail fast, stay visible.** No slow/backoff retry loops. A failed source/surface/judge logs and
   continues; Track A uses `_safe_discover`, Track B emits one `status="empty"/"error"` row per prompt.
   Failures must surface in the sheet, never abort the run or get silently dropped.
7. **Ownership is suffix-matched; nonprod is flagged.** Any `*.inito.com` host (incl. preprod./staging.)
   is `owned`; `is_nonprod_owned` additionally sets the `nonprod_url` column. Don't regress either.
8. **Model id + actor slugs live only in `config.json`.** Judge model = `limits.judge_model`
   (`claude-sonnet-4-6`). Never hardcode a model or slug in `pipeline.py`.
9. **Don't reintroduce removed pieces:** Bing, Instagram, X, YouTube, TikTok, the Gemini actor, parquet,
   any training-data-only (no-live-search) LLM call, OR the deleted time-series layer (history CSVs,
   metrics/CIs, the `status` stale/mixed/current column, `owned_stale`, the stale-attribution/action
   engine, `--diff-only`/`--reeval`/`--force`, cross-run resume). They were removed on purpose.

## Where things live

| Concern | Location |
|---|---|
| All knobs (topics, regex, domains, competitor brands, actor slugs, limits, surfaces) | `config.json` |
| Output column contract | `WEB_COLUMNS` / `LLM_COLUMNS` (top of `pipeline.py`) |
| Track A discovery | `discover_serp/news/ads/reddit`, registered in `WEB_DISCOVERERS` (+ `WEB_SOURCES`) |
| Track B discovery | `_run_chatgpt/_run_perplexity`, registered in `SURFACE_RUNNERS` |
| Classification | `detect_claims` (regex hints) → `judge` / `judge_llm_response` (Claude, forced tool call) |
| Web row builder | `classify_web_record(record, body)` |
| LLM row builder | `_llm_row(...)` (+ `_blank_llm_row`, `_error_rows`) |
| Ownership / nonprod | `ownership(url)`, `ownership_for_ad(advertiser, url)`, `is_nonprod_owned(url)` |
| URL + link helpers | `normalize_url`, `domain_of`, `extract_links`, `_host_matches` |
| Sheet writers (CSV) | `write_web_sheet`, `write_llm_sheet` (per-run folder only) |
| Run folders | `make_run_dir`, `run_dir_name`, `_cleanup_empty` |
| CLI + selection | `main`, `resolve_selection`, `prompt_select`, `parse_extra_prompts`, `list_topics` |

## The change workflow (staff-engineer default)

1. **Read the relevant docs/sections first.** Don't infer scope from code alone.
2. **Make the smallest surgical change** that fully solves the task. Boy-scout neighboring code you
   touch — but don't sprawl.
3. **Route everything new through `config.json`** (slug, model, limit, domain, topic).
4. **Keep the column contract intact** so the writers don't drop/KeyError: list + row builder + test together.
5. **Add or extend tests** (`tests/test_pipeline.py`) for anything you change — especially `detect_claims`,
   `ownership`, discovery parsing, `classify_web_record`, `_llm_row`, the writers, or the selection
   resolver. Add a **false-positive guard** when touching the classifier.
6. **Verify:**
   - `pytest -q` — must stay green (fully offline; deps stubbed in `tests/conftest.py`).
   - For anything touching **orchestration, a sheet writer, a new actor, or the CLI**, run a hermetic
     **`geo-dry-run`** — it catches integration bugs unit tests can't.
7. **Update docs** (`docs/`, `README.md`, `CLAUDE.md`) when behavior or scope changes.
8. **Never run the live pipeline** (real Apify/Anthropic = cost + network) unless the user explicitly
   asks. Dry runs are stubbed and free.

## Before any first live run of a new actor
Confirm the actor's **slug + input schema** on its Apify Store page (they version) and align the
`run_input` builder. Populate any required config (e.g. `ads_start_urls`).

## On the topic catalog (note for `geo-add-*` / config edits)
`config.json` `topics` is now **freely editable** — add / remove / reword on demand; each run is a
self-contained snapshot so there is no time series to protect (the old append-only rule is gone). `id`s
are just readable join keys. `--list-topics` prints the set.
