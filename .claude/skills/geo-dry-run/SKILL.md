---
name: geo-dry-run
description: Use to verify Inito GEO pipeline changes end-to-end without hitting Apify/Anthropic (no network, no cost). A hermetic harness stubs the actors + Claude, points DATA at a temp dir, and drives the real CLI so integration bugs unit tests miss show up. Run this for any change to orchestration, persistence, the CLI, a new actor/surface, or judge fields.
---

# GEO — hermetic dry run

Unit tests cover functions in isolation; the dry run exercises the **seams**: CLI → orchestration →
run-folder plumbing, the non-fallback judge tool-block path, CSV roundtrip/coercion, resume, the
multi-day diff, and ads → `owned_stale`. It already caught two real issues (diff-only blanking
`kappa_regex_judge`; empty orphan run folders).

## How it works
1. Stub `apify_client` + `anthropic` **before** importing `pipeline` (so the real packages and keys
   aren't needed, and `ApifyClient()`/`Anthropic()` at import are no-ops).
2. Point `pipe.DATA` + `pipe.FETCH_CACHE_PATH` at a temp dir.
3. Replace `pipe.run_actor` with a fake that returns canned datasets keyed by the `label` arg.
4. Replace `pipe.claude` with a fake whose `messages.create` returns a real `tool_use` block (so the
   actual judge parsing runs, not just the fallback). **Inspect only the page/response body** of the
   prompt, never the regex-hint key names (a fake that greps the whole prompt will false-positive on
   `claim_attach_to_phone` etc).
5. Set `pipe.CFG["ads_start_urls"]` if testing ads (empty config = source skipped, correctly).
6. Drive the real CLI via `pipe.main([...])`. Bump `pipe.RUN_DATE` between runs to test the diff.
7. Tree the temp dir and print the key CSVs.

## Use the template
`harness_template.py` in this skill folder is a ready-to-run harness. Copy it to the scratchpad,
adjust the fake datasets / scenarios for your change, and run with `python3`. **Never** commit it or
write into the repo's `data/` — it's a throwaway.

## What to assert by eye
- Run folders are **self-contained + descriptively named**; resume / zero-discovery leaves **no
  orphan folder**.
- `owned_stale` reflects Inito-owned stale pages/ads; `competitor_negative`, `share_of_voice_category`
  look right; `kappa_regex_judge` is populated (incl. after `--diff-only`).
- Track B rows have source-targeted `action` + `priority`, priority-sorted in
  `llm_visibility_latest.csv`; per-surface CIs in `llm_metrics.csv`.
- No KeyErrors; CSV booleans coerce (claim counts / kappa computed from CSV-read history are correct).

## Scope
This is for **verification only**. It does not replace `pytest -q` (run both). It does not touch the
network — never use it to justify skipping a real run the user explicitly asked for.
