---
name: geo-safe-change
description: Read FIRST before adding or changing any feature in the Inito GEO monitor (pipeline.py / config.json / tests). Encodes the repo's hard invariants, where everything lives, and the required verify loop so a change never breaks the time series, the two tracks, or the headline metric. Use whenever the task touches discovery, classification, persistence, metrics, the CLI, or config.
---

# GEO — safe-change guardrails

The umbrella skill. Start here, then jump to the specific workflow:
`geo-add-source` (Track A actor), `geo-add-llm-surface` (Track B), `geo-tune-classifier`
(claim regex + judge), `geo-dry-run` (hermetic end-to-end check).

Authoritative context: `docs/REQUIREMENTS.md`, `docs/DESIGN.md`, `CLAUDE.md`.

## What the system is (one breath)

A batch GEO monitor for the brand **Inito**. Two tracks in `pipeline.py`:
**A — Web/SERP** (Google SERP+News+Ads+Reddit → crawl → classify stale claims) and
**B — LLM visibility** (live-web ChatGPT + Perplexity → classify brand presence). Every
decision hinges on **old (phone-dependent) vs current (InSight Wireless Reader)**. Outputs are
**CSV only**, one self-contained timestamped folder per run.

## Hard invariants — do not break

1. **`config.json` `topics` is append-only.** One unified catalog feeds BOTH tracks — each entry has a
   stable `id` (cross-surface join key), `intent`, `web` phrasing (Track A) and `llm` phrasing (Track B).
   Editing an existing `web`/`llm` string or `id` silently breaks the time series + resume. Add a new
   topic; never edit/delete an old one. Read via `web_topics()` / `llm_topics()`.
2. **The LLM judge is the arbiter of `status`/visibility, not regex.** `claim_patterns` are a
   cheap pre-filter + offline fallback. Improve recall by adding patterns — never make regex the
   final call.
3. **Never flag shared attributes as stale.** Four hormones (E3G/LH/PdG/FSH), the companion app,
   the dip-strip workflow, accuracy — common to *both* products. Flagging them is a false positive.
   Only phone-dependence (iPhone-only, clip, camera, Lightning port, no Android) is stale.
4. **CSV only — no parquet.** CSV-roundtripped booleans come back as the strings `"True"`/`"False"`;
   coerce with `_to_bool` / `_coerce_web` / `_coerce_llm` before any math.
5. **Fail fast, stay visible.** No slow/backoff retry loops. A failed source/surface/judge logs and
   continues; Track A uses `_safe_discover`, Track B emits one error row per prompt (`status="error"`,
   `error_note`). Failures must surface in the sheet, never abort the run or get silently dropped.
6. **`owned_stale` is the headline metric.** Inito-owned pages/ads with stale claims are priority 1;
   this number trends to zero first. Don't change its definition without sign-off.
7. **Model id + actor slugs live only in `config.json`.** Judge model = `limits.judge_model`
   (`claude-sonnet-4-6`). Never hardcode a model or slug in `pipeline.py`.
8. **Don't reintroduce removed pieces:** Bing, Instagram, X, YouTube, TikTok, `fayoussef/bulk-llm-runner`,
   `scrape.badger/google-ai-mode-scraper`, the Gemini actor, parquet, or any training-data-only
   (no-live-search) LLM call. They were removed on purpose.

## Where things live

| Concern | Location |
|---|---|
| All knobs (queries, prompts, regex, domains, actor slugs, limits, surfaces) | `config.json` |
| Track A discovery | `discover_serp/news/ads/reddit`, registered in `WEB_DISCOVERERS` |
| Track B discovery | `_run_chatgpt/_run_perplexity`, registered in `SURFACE_RUNNERS` |
| Classification | `detect_claims` (regex) → `judge` / `judge_llm_response` (Claude, forced tool call) |
| Ownership | `ownership(url)`, `ownership_for_ad(advertiser, url)` |
| Persistence (CSV) | `persist`, `persist_llm` (per-run files in `out_dir`; cumulative at `DATA` root) |
| Metrics | `compute_metrics`, `compute_llm_metrics`, `_wilson_ci`, `_mean_ci`, `_kappa_regex_vs_judge` |
| Actions | `derive_action(row)` (priority + source-targeted), `link_stale_sources` (cross-track) |
| Run folders | `make_run_dir`, `run_dir_name`, `_finalize_run`, `_cleanup_empty` |
| CLI + selection | `main`, `resolve_selection`, `prompt_select` |

## The change workflow (staff-engineer default)

1. **Read the relevant docs/sections first.** Don't infer scope from code alone.
2. **Make the smallest surgical change** that fully solves the task. Boy-scout neighboring code you
   touch (naming, dead refs, comments) — but don't sprawl beyond the task.
3. **Route everything new through `config.json`** (slug, model, limit, domain, query/prompt).
4. **Keep the row schemas intact** so persistence/metrics don't KeyError. Web row + LLM row schemas
   are in `docs/DESIGN.md` §4.2 / §5.1.
5. **Add or extend tests** (`tests/test_pipeline.py`) for anything you change — especially
   `detect_claims`, `ownership`, discovery parsing, the action engine, the selection resolver, or
   metrics math. Add a **false-positive guard** when touching the classifier.
6. **Verify:**
   - `pytest -q` — must stay green (fully offline; deps stubbed in `tests/conftest.py`).
   - For anything touching **orchestration, persistence, a new actor, or the CLI**, run a hermetic
     **`geo-dry-run`** — it catches integration bugs unit tests can't (it already caught the
     diff-only kappa-blanking bug and empty-folder wart).
7. **Update docs** (`docs/`, `README.md`, `CLAUDE.md`) when behavior or scope changes.
8. **Never run the live pipeline** (real Apify/Anthropic = cost + network) unless the user explicitly
   asks. Dry runs are stubbed and free.

## Before any first live run of a new actor
Confirm the actor's **slug + input schema** on its Apify Store page (they version) and align the
`run_input` builder. Populate any required config (e.g. `ads_start_urls`).
