# Design — Inito GEO Monitor (lean snapshot model)

> Rewritten 2026-06-24 alongside `REQUIREMENTS.md`. The system is now a per-run snapshot generator, not
> a time-series tracker. Git history holds the prior (metrics/attribution/diff) design.

## 1. Overview & philosophy

Single orchestrator `pipeline.py`, two independent tracks, CLI-driven. Apify owns discovery + enrichment;
our code owns classify → write. Each run is **self-contained**: one timestamped folder under `data/` with
one or two lean CSVs. **No state survives a run except `fetch_cache.csv`** (a cost saver).

Principles: keep failures visible not fatal; let the Claude judge write the narrative (regex only hints);
fixed output columns as a contract; canonical (de-tracked) URLs; nothing accumulates.

## 2. Layers

```
discover → enrich → classify → write
```
- **discover** — `WEB_DISCOVERERS` (serp/news/ads/reddit) and `SURFACE_RUNNERS` (chatgpt/perplexity).
- **enrich** (Track A only) — `enrich_content` + the fetch cache.
- **classify** — `judge` / `judge_llm_response` (Claude tool call, deterministic offline fallback);
  `detect_claims` (regex hints); `ownership`, `extract_links`, `is_nonprod_owned`, `normalize_url`.
- **write** — `write_web_sheet` / `write_llm_sheet` (reindex to `WEB_COLUMNS` / `LLM_COLUMNS`).

## 3. Track A — Web/SERP

`refresh(sources, queries, out_dir)`:
1. Discover in parallel under `_safe_discover` (one source failing never aborts the run).
2. Dedupe by `normalize_url` (keep best/lowest non-zero SERP rank; preserve precomputed ad ownership).
3. `enrich_content` → full page text (cache first).
4. `classify_web_record` per source → one `web_observations.csv` row.

Discovery record (pre-classification): `{url, platform, query, intent, topic_id, rank, title, snippet
[, advertiser, ownership]}`. AI Overviews / SERP panels are pseudo-URLs (`aioverview::`,
`chatgptsearch::`, `perplexitysearch::`), judged on snippet text, not crawled.

Output `WEB_COLUMNS`: `source, url, query, intent, topic_id, ownership, says_about_inito,
mentions_competition, competition_summary, competitors_named, sentiment, price, links_on_source,
nonprod_url, title`.

## 4. Track B — LLM visibility

`run_llm_visibility(...)` → `discover_llm_visibility` fans out `(surface × run)` jobs in a thread pool;
each job samples all prompts. **No resume / no force** — every run is fresh. `_llm_row` judges a response,
merges + canonicalises cited URLs (judge sources + actor citations + inline), caps at 15, flags
`nonprod_url`. Empty text → `status=empty` (never judged); actor failure → one `status=error` row/prompt.

Output `LLM_COLUMNS`: `surface, run, prompt, intent, topic_id, mentioned, rank, recommended,
says_about_inito, mentions_competition, competition_summary, competitors_named, sentiment, price,
sources_cited, nonprod_url, response_text, status, error_note`.

Sampling: `llm_num_runs` (default 5), one row per run — captures model variance. ChatGPT pins US via the
actor's `country`; Perplexity (sonar) has no IP control.

## 5. Classification

Two-stage: cheap `detect_claims` regex hints (price + which old-product phrases appear) feed the Claude
judge, which returns the narrative via a forced tool call (`classify_page` / `analyze_llm_response`).
**Fallback contract:** if Claude is unavailable, `_judge_web_fallback` / the LLM fallback return the same
keys deterministically (`says_about_inito` ≈ "describes the OLD product" / "mentions Inito", competitors
from `competitor_brands`, price from regex). This is what the offline tests exercise.

Ownership (`ownership(url)`): competitor (suffix) → app-store (by app id) → owned (`*.inito.com` suffix)
→ amazon `/dp/` marketplace → third_party. `ownership_for_ad` matches by advertiser identity first.
`is_nonprod_owned` flags preprod/staging/dev/… Inito hosts (still owned, but surfaced as `nonprod_url`).

## 6. Persistence

CSV only. `make_run_dir` builds a descriptive folder name (`run_dir_name`, pure/testable);
`_cleanup_empty` removes an orphan folder if a run discovered nothing. The two sheet writers each emit a
fixed column order. The only cross-run file is `fetch_cache.csv` at the `data/` root (7-day TTL).

## 7. Error handling
- Track A discovery: `_safe_discover` try/except → empty list, run continues.
- Track B: per-surface/per-prompt fail-fast into visible `error`/`empty` rows; `run_actor` has no retry.
- Missing `PERPLEXITY_API_KEY` → Perplexity emits error rows; the rest is unaffected.

## 8. CLI
`main(argv)` dispatches `--list-topics` / `--llm` / `--refresh`. `prompt_select` → `resolve_selection`
(indices / name substrings / `all`, deduped). `parse_extra_prompts` injects ad-hoc one-off Track B prompts.

## 9. Testing
`pytest -q` offline (`conftest.py` stubs `apify_client` + `anthropic`, points `DATA`/`FETCH_CACHE_PATH`
at tmp). Covers URL canonicalisation, ownership (incl. preprod) + ads, nonprod flagging, link extraction,
competition detection, claim hints + not-stale guard, SERP/ads parsing, fetch cache, both sheet writers,
the selection resolver, run-folder naming, and a CLI end-to-end. Plus the `geo-dry-run` hermetic harness
(real judge tool-block path) for the seams.

## 10. Known limitations / tech debt
See `OPEN-ITEMS.md`. Headlines: no trend view (by design); judge narrative unmeasured (no gold set);
generic/non-web LLM mode deferred; Reddit rate-limits; Perplexity no IP control.
