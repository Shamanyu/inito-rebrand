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

A surface can be an **Apify actor** (like ChatGPT) **or a direct API** (like Perplexity/sonar). Same
contract either way: a `_run_<surface>(run_idx, prompts_cfg)` registered in `SURFACE_RUNNERS`.

## Steps

1. **Pick the mechanism.** Prefer a reliable live-search **API** if one exists (sonar-style) — web-UI
   scrapers are anti-bot-fragile (zhorex's Perplexity scraper failed live). Confirm where the **answer
   text + citations** are. If it's an actor, add its slug to `config.json` `actors`; if an API, add its
   model/knobs to `limits` and read its key from env (optional, like `PPLX_KEY`).
2. **Add the surface key to `config.json` `llm_surfaces`** (it's part of the time series via per-surface metrics).
3. **Write `_run_<surface>(run_idx, prompts_cfg) -> List[dict]`** beside `_run_chatgpt`/`_run_perplexity`:
   - **Actor path:** build `run_input` (prompts + `country`/proxy — don't name a proxy group the account
     lacks); `try: items = run_actor(...)` / `except: return _error_rows(...)` (fail-fast).
   - **API path:** if the key env is empty, `return _error_rows(...)`; else loop prompts and call your
     `<api>_complete()` helper with a **per-prompt** try/except (one bad prompt → one error row).
   - Either way, call `_llm_row(run_idx, "<surface>", prompt, intent, response, extra_sources=cites)`.
     `_llm_row` skips empty responses (`status=empty`), runs the judge, merges sources, attaches `action`+`priority`.
4. **Register it:** add `"<surface>": _run_<surface>` to `SURFACE_RUNNERS`.
5. **Sampling:** `num_runs` (default 3) gives independent samples (model variance). True per-sample IP
   control is **not** guaranteed (see DESIGN §5.4) — don't promise it.
6. **Tests** (`tests/test_pipeline.py`): for an actor, monkeypatch `pipe.run_actor`; for an API, monkeypatch
   the `*_complete` helper + its key. Assert rows have `surface`, `action`, `priority`; add a failure case
   → `status=="error"`, `priority==6`. Mirror `test_run_perplexity_*` and `..._error_rows_on_failure`.
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
