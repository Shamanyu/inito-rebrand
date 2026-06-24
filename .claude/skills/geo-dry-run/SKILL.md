---
name: geo-dry-run
description: Use to verify Inito GEO pipeline changes end-to-end without hitting Apify/Anthropic (no network, no cost). A hermetic harness stubs the actors + Claude, points DATA at a temp dir, and drives the real CLI so integration bugs unit tests miss show up. Run this for any change to orchestration, the sheet writers, the CLI, or a new actor/surface/judge field.
---

# GEO — hermetic dry run

Unit tests cover functions in isolation; the dry run exercises the **seams**: CLI → orchestration →
run-folder plumbing, the non-fallback judge tool-block path, and the two sheet writers' column contracts.

## How it works
1. Stub `apify_client` + `anthropic` **before** importing `pipeline` (so the real packages and keys
   aren't needed, and `ApifyClient()`/`Anthropic()` at import are no-ops).
2. Point `pipe.DATA` + `pipe.FETCH_CACHE_PATH` at a temp dir.
3. Replace `pipe.run_actor` with a fake that returns canned datasets keyed by the `label` arg.
4. Replace `pipe.claude` with a fake whose `messages.create` returns a real `tool_use` block (so the
   actual judge parsing runs, not just the fallback). **Inspect only the page/response body** of the
   prompt, never the regex-hint key names (a fake that greps the whole prompt false-positives on
   `claim_attach_to_phone` etc). Return the **current tool schemas**: `classify_page` →
   `{says_about_inito, mentions_competition, competition_summary, competitors_named, sentiment_inito,
   price_mentioned}`; `analyze_llm_response` → those plus `{inito_mentioned, inito_rank,
   inito_recommended, sources_cited}`.
5. Set `pipe.CFG["ads_start_urls"]` if testing ads (empty config = source skipped, correctly).
6. Drive the real CLI via `pipe.main([...])`.
7. Tree the temp dir and print the two sheets.

## Use the template
`harness_template.py` in this skill folder is a ready-to-run harness. Copy it to the scratchpad, adjust
the fake datasets / scenarios for your change, and run with `python3`. **Never** commit it or write into
the repo's `data/` — it's a throwaway.

## What to assert by eye
- Run folders are **self-contained + descriptively named**; zero-discovery leaves **no orphan folder**.
- `web_observations.csv` has exactly `WEB_COLUMNS`; one row per source; `says_about_inito` reads sensibly;
  `mentions_competition` / `competitors_named` populated when a rival is named; `links_on_source` + `price`
  filled; `ownership` correct (incl. `*.inito.com` → owned) and `nonprod_url=True` for preprod/staging.
- `llm_observations.csv` has exactly `LLM_COLUMNS`; one row per (surface × prompt × run); `sources_cited`
  canonical (tracking params stripped) and deduped; empty/failed responses are `status=empty/error` rows.
- No KeyErrors; the only cross-run file is `fetch_cache.csv`.

## Scope
Verification only. Does not replace `pytest -q` (run both). Never touches the network — don't use it to
justify skipping a real run the user explicitly asked for.
