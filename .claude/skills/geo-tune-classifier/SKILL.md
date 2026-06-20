---
name: geo-tune-classifier
description: Use when improving how the Inito GEO monitor decides stale vs current — adding/adjusting claim regex (claim_patterns / current_signal_patterns), editing the Claude judge prompts/tools, or fixing a misclassification (false positive or false negative). Encodes the old-vs-current product taxonomy. Read geo-safe-change first.
---

# GEO — tune the classifier (regex + judge)

Classification is two-stage: `detect_claims` (cheap regex pre-filter + offline fallback) →
`judge` / `judge_llm_response` (Claude Sonnet, forced tool call, the **authoritative** verdict).

## The taxonomy (authoritative — `docs/REQUIREMENTS.md` §4)

**STALE** = the OLD phone-dependent product presented as current:
iPhone-only · attaches/clips to phone · phone-camera-as-sensor · Lightning/charging-port ·
no Android · specific old iPhone models.

**CURRENT** = InSight Wireless Reader: standalone · Wi-Fi · iOS *and* Android · built-in optical
sensor / Spectral Mapping.

**NOT STALE — never flag these** (common to BOTH products): four hormones (E3G/LH/PdG/FSH) ·
companion phone app · dip-the-strip workflow · accuracy/clinical claims. Distinguish *"syncs results
to your phone app"* (not stale) from *"attaches to your phone / uses the phone camera"* (stale).

**MIXED** = both old and new, **or** old specs quoted to refute/correct them.

## Adding / changing regex (`config.json`)

- `claim_patterns` has four buckets: `iphone_only`, `attach_to_phone`, `camera_dependent`,
  `no_android`. Patterns are case-insensitive (`detect_claims` lowercases). Add new phrasings to
  improve recall — **never** add a pattern that could match a NOT-STALE shared attribute.
- `current_signal_patterns` feeds the offline fallback's mixed/current decision and the judge's hint.
- `price_pattern` extracts prices; **price alone is not a stale trigger** (ambiguous) — don't make it one.
- Editing regex is allowed and encouraged (unlike `queries`, which are frozen). Mind the regex gap
  widths: a real bug was `attach.{0,20}` missing "attach the monitor to your phone" (21 chars) — the
  pattern is now `.{0,40}`. Keep gaps generous but anchored.

## Editing the judge

- `JUDGE_SYSTEM` (Track A) / `LLM_JUDGE_SYSTEM` (Track B) carry the taxonomy + worked examples,
  including the not-stale and refutation-edge cases. Add an example when you see a real miss.
- `JUDGE_TOOL` / `LLM_JUDGE_TOOL` are forced tool schemas (`tool_choice` fixed) — output always
  matches. If you add/remove a field, update: the tool schema, the row built in `refresh` /
  `_llm_row`, **and** the deterministic fallback in `judge` / `judge_llm_response` so offline tests
  still return the full contract.
- Judge model stays in `config.limits.judge_model` only.

## Tests (mandatory for classifier changes)

- Add a case to `tests/test_pipeline.py` for the new phrasing (regex) and a **false-positive guard**
  (a NOT-STALE sentence that must NOT trip any claim flag — see
  `test_detect_claims_shared_attributes_not_stale`).
- The judge fallback is what runs offline (Claude stubbed). Assert status via the fallback for
  stale/current/mixed/neutral inputs. To exercise the *real* tool-block path, use a `geo-dry-run`
  with a fake Claude returning a tool_use block.
- Keep the two original regressions green: `attach_to_phone` 21-char gap, `searchQuery` dict-or-string.

## Verify
`pytest -q`, plus a `geo-dry-run` if you changed judge fields or the row schema.
