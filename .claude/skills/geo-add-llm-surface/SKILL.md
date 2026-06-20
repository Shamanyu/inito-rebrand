---
name: geo-add-llm-surface
description: Use when adding a new Track B LLM visibility surface to the Inito GEO monitor (a new live-web AI assistant scraped through an Apify actor — e.g. re-adding Gemini, or a new assistant). Covers the live-web requirement, the runner+adapter, distinct-US-IP sampling, error rows, the action engine, and tests. Read geo-safe-change first.
---

# GEO — add a Track B LLM surface

A surface is a **live-web** AI assistant (real user experience, live search + citations). Each is
queried via its own Apify web-interface actor and adapted into the common LLM-row schema.

## Non-negotiable
- **Live web only.** No API/training-data-only calls (they don't reflect what a user sees, and miss
  citations — our fix targets). If the actor can't do live search with sources, it doesn't qualify.

## Steps

1. **Confirm the actor on Apify** — slug + input schema + output shape (where the answer text and the
   **citations/sources** are). Add the slug to `config.json` `actors` with a `_comment_<surface>`.
2. **Add the surface key to `config.json` `llm_surfaces`** (append-only spirit — it's part of the
   time series via per-surface metrics).
3. **Write `_run_<surface>(run_idx, prompts_cfg) -> List[dict]`** beside `_run_chatgpt`/`_run_perplexity`:
   - Build `run_input` (prompts + proxy/country). Pin **US**: a `country` string if the actor takes
     one, else `proxyConfiguration` via `_us_proxy()`.
   - `try: items = run_actor(...)` / `except Exception as e: return _error_rows(run_idx, "<surface>", prompts_cfg, e)`
     — fail-fast into visible error rows, one per prompt.
   - For each item, extract `prompt`, `response` text, and **citation URLs**; call
     `_llm_row(run_idx, "<surface>", prompt, intent, response, extra_sources=cites, priors=...)`.
     `_llm_row` runs the judge, merges sources, and attaches `action` + `priority` for you.
   - `priors` (optional): if the actor pre-extracts brand signals (like Perplexity brand_monitor's
     `mentioned`/`position`/`competitorsMentioned`), pass them so the judge has a fallback prior.
4. **Register it:** add `"<surface>": _run_<surface>` to `SURFACE_RUNNERS`.
5. **Distinct US IPs are automatic.** `num_runs` (default 3, `config.llm_num_runs`) issues each sample
   as a **separate actor run** → fresh US session → distinct IP. Don't try to force sessions inside
   one run. Keep each `_run_*` a single actor call over all prompts.
6. **Tests** (`tests/test_pipeline.py`): monkeypatch `pipe.run_actor` to return canned items; assert
   `discover_llm_visibility(["<surface>"], prompts, 1)` yields rows with `surface`, `action`,
   `priority`. Add a failure case (raise in `run_actor`) → `status=="error"`, `priority==6`. Mirror
   `test_discover_llm_visibility_runs_surface` and `..._error_rows_on_failure`.
7. **Verify:** `pytest -q`, then a **`geo-dry-run`** with `--surfaces <surface>` to confirm
   discover → judge → `derive_action` → `persist_llm` → `export_llm_csv` → `compute_llm_metrics`
   and that per-surface CIs appear in `llm_metrics.csv` (`llm_<surface>_mention` etc).

## Notes
- The LLM row schema + the `derive_action` priority table live in `docs/DESIGN.md` §5.1 / §9. If you
  add a signal the judge should emit, extend `LLM_JUDGE_TOOL` + `LLM_JUDGE_SYSTEM` and the row, then
  the fallback in `judge_llm_response` for offline behavior.
- `compute_llm_metrics` groups by `surface` automatically — no metric code change needed for a new
  surface (Wilson/mean CIs come for free).
- Resume keys on `(surface, run_index)` in `llm_visibility_history.csv`; a new surface just works.
