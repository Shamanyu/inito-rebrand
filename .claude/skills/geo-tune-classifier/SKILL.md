---
name: geo-tune-classifier
description: Use when improving how the Inito GEO monitor judges what a source says about the brand — adjusting claim regex hints (claim_patterns / current_signal_patterns / price_pattern), editing the Claude judge prompts/tools (says_about_inito + competition fields), or fixing a misclassification. Encodes the old-vs-current product taxonomy. Read geo-safe-change first.
---

# GEO — tune the classifier (regex hints + judge)

Classification is two-stage: `detect_claims` (cheap regex hints + offline fallback) →
`judge` / `judge_llm_response` (Claude, forced tool call, the **authoritative** output). The judge no
longer emits a stale/mixed/current **status** — it writes a plain-language **`says_about_inito`** narrative
plus competition fields. "Still describes the old product" lives inside that narrative, not a column.

## The taxonomy (authoritative — `docs/REQUIREMENTS.md` §3)

**OLD product** (note it in `says_about_inito` if presented as current): iPhone-only · attaches/clips to
phone · phone-camera-as-sensor · Lightning/charging-port · no Android · specific old iPhone models.

**CURRENT product** = InSight Wireless Reader: standalone · Wi-Fi · iOS *and* Android · built-in optical
sensor / Spectral Mapping.

**NOT noteworthy — never call these "old"** (common to BOTH products): four hormones (E3G/LH/PdG/FSH) ·
companion phone app · dip-the-strip workflow · accuracy/clinical claims. Distinguish *"syncs results to
your phone app"* (fine) from *"attaches to your phone / uses the phone camera"* (the old product).

## Adding / changing regex (`config.json`)

- `claim_patterns` has four buckets: `iphone_only`, `attach_to_phone`, `camera_dependent`, `no_android`.
  Case-insensitive (`detect_claims` lowercases). These are only **hints** — they feed the judge prompt and
  the offline fallback's old-product detection. **Never** add a pattern that could match a NOT-noteworthy
  shared attribute.
- `current_signal_patterns` are extra hints. `price_pattern` extracts the `price` column; price is never a
  trigger for anything on its own.
- Mind regex gap widths: a real bug was `attach.{0,20}` missing "attach the monitor to your phone" (21
  chars) — it's now `.{0,40}`. Keep gaps generous but anchored.

## Editing the judge

- `JUDGE_SYSTEM` (Track A) / `LLM_JUDGE_SYSTEM` (Track B) carry the taxonomy and tell the judge to write
  `says_about_inito` + capture competition. Add guidance when you see a real miss.
- `JUDGE_TOOL` (`classify_page`) returns: `says_about_inito`, `mentions_competition`,
  `competition_summary`, `competitors_named`, `sentiment_inito`, `price_mentioned`.
  `LLM_JUDGE_TOOL` (`analyze_llm_response`) adds: `inito_mentioned`, `inito_rank`, `inito_recommended`,
  `sources_cited`. They're forced tool schemas (`tool_choice` fixed) so output always matches.
- If you add/remove a field, update **four** places together: the tool schema, the row builder
  (`classify_web_record` / `_llm_row`), the column list (`WEB_COLUMNS` / `LLM_COLUMNS`), **and** the
  deterministic fallback (`_judge_web_fallback` / the fallback inside `judge_llm_response`) so offline
  tests still return the full contract.
- Competition detection in the offline fallback uses `config.competitor_brands` via `_competitors_in` —
  add brand names there, not in code.
- Judge model stays in `config.limits.judge_model` only.

## Tests (mandatory for classifier changes)

- Add a case to `tests/test_pipeline.py` for the new phrasing (regex hint) and a **false-positive guard**
  (a NOT-noteworthy sentence that must NOT trip any old-product flag — see
  `test_detect_claims_shared_attributes_not_stale`).
- The judge fallback is what runs offline (Claude stubbed). Assert `says_about_inito` / competition for
  old-product / neutral / competitor inputs (see `test_judge_web_fallback_*`). To exercise the *real*
  tool-block path, use a `geo-dry-run` with a fake Claude returning a tool_use block.
- Keep the two original regressions green: `attach_to_phone` 21-char gap, `searchQuery` dict-or-string.

## Verify
`pytest -q`, plus a `geo-dry-run` if you changed judge fields or a column list.
