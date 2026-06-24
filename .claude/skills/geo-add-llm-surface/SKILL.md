---
name: geo-add-llm-surface
description: Use when adding a new Track B LLM visibility surface to the Inito GEO monitor (a new live-web AI assistant via an Apify actor or a live-search API — e.g. re-adding Gemini). Covers the live-web requirement, the runner+adapter, sampling, error rows, and tests. Read geo-safe-change first.
---

# GEO — add a Track B LLM surface

A surface is a **live-web** AI assistant (real user experience, live search + citations). Each is queried
via its own Apify web-interface actor or a live-search API, and adapted into the common LLM-row schema
(`LLM_COLUMNS`) → `llm_observations.csv`.

## Non-negotiable
- **Live web only.** No API/training-data-only calls (they don't reflect what a user sees, and miss
  citations). If it can't do live search with sources, it doesn't qualify. (Generic/non-web mode is a
  deferred, separate idea — see `docs/OPEN-ITEMS.md`; don't smuggle it in here.)

A surface can be an **Apify actor** (like ChatGPT) **or a direct API** (like Perplexity/sonar). Same
contract either way: a `_run_<surface>(run_idx, prompts_cfg)` registered in `SURFACE_RUNNERS`.

## Steps

1. **Pick the mechanism.** Prefer a reliable live-search **API** if one exists (sonar-style) — web-UI
   scrapers are anti-bot-fragile. Find where the **answer text + citations** are. Actor → add its slug to
   `config.json` `actors`; API → add its model/knobs to `limits` and read its key from env (optional,
   like `PPLX_KEY`).
2. **Add the surface key to `config.json` `llm_surfaces`** so it's selectable.
3. **Write `_run_<surface>(run_idx, prompts_cfg) -> List[dict]`** beside `_run_chatgpt`/`_run_perplexity`:
   - **Actor path:** build `run_input` (prompts + `country`/proxy — don't name a proxy group the account
     lacks); `try: items = run_actor(...)` / `except: return _error_rows(...)` (fail-fast).
   - **API path:** if the key env is empty, `return _error_rows(...)`; else loop prompts with a
     **per-prompt** try/except (one bad prompt → one error row).
   - Either way, call `_llm_row(run_idx, "<surface>", prompt, intent, response, extra_sources=cites,
     topic_id=...)`. `_llm_row` skips empty responses (`status="empty"`), runs the judge, merges +
     **canonicalises** sources, and flags `nonprod_url`. (It no longer attaches any action/priority —
     that engine was removed.)
4. **Register it:** add `"<surface>": _run_<surface>` to `SURFACE_RUNNERS`.
5. **Sampling:** `num_runs` (default 5) gives independent samples, **one row per run** (no aggregation,
   no resume). True per-sample IP control is **not** guaranteed (only ChatGPT pins US) — don't promise it.
6. **Tests** (`tests/test_pipeline.py`): for an actor, monkeypatch `pipe.run_actor`; for an API,
   monkeypatch the `*_complete` helper + its key. Assert rows carry `surface`, `status=="ok"`, and the
   full `LLM_COLUMNS`; add a failure case → `status=="error"` with `error_note`. Mirror
   `test_run_perplexity_*` and `test_discover_llm_visibility_error_rows_on_failure`.
7. **Verify:** `pytest -q`, then a **`geo-dry-run`** with `--surfaces <surface>` to confirm
   discover → `judge_llm_response` → `_llm_row` → `write_llm_sheet` emits the sheet with no KeyErrors.

## Notes
- The LLM row schema is `LLM_COLUMNS` (top of `pipeline.py`). If you add a signal the judge should emit,
  extend `LLM_JUDGE_TOOL` + `LLM_JUDGE_SYSTEM`, the row in `_llm_row`, **and** the offline fallback in
  `judge_llm_response` so offline tests still return the full contract.
- Cited URLs must stay canonical (`normalize_url`) and capped (15) — `_llm_row` already does this; reuse it.
