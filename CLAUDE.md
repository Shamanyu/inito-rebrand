# CLAUDE.md

Guidance for Claude Code (and humans) working in this repo.

## What this is

A GEO (generative engine optimization) monitor for the brand **Inito**. It finds web pages and live
AI-assistant answers that make **stale** claims about Inito's old product (iPhone-only, clips to phone,
camera-based, no Android) or **competitive** claims (rivals framed as better), scores them, and tracks
the counts over time so you can prove the stale-source footprint is shrinking as fixes land.

The old product clipped onto an iPhone and used the phone camera/Lightning port (iPhone-only). The
**current** product is the **InSight Wireless Reader** — standalone, Wi-Fi, works on iOS *and* Android,
no camera/clip. Both products measure the same four hormones (E3G, LH, PdG, FSH), use a companion app,
and use a dip-strip workflow — **those shared attributes are NOT stale.** Every classification decision
hinges on old-vs-current phone-dependence. See `docs/REQUIREMENTS.md` §4 for the full taxonomy.

## Architecture

Two independent tracks, both in `pipeline.py`. Outputs are **CSV only** (no parquet). Every run writes a
self-contained, timestamped folder under `data/`.

### Track A — Web/SERP (stale claim detection)

1. **discover** (parallel) — Google SERP (`discover_serp`, incl. AI Overview + GPT/Perplexity panels),
   Google News (`discover_news`), Google Ads Transparency Center (`discover_ads`), Reddit
   (`discover_reddit`). Reddit needs a residential/US proxy (datacenter → 403).
2. **enrich** — `enrich_content` runs the Website Content Crawler over deduped URLs → full page text
   (CSV fetch cache, 7-day TTL).
3. **classify** — `detect_claims` (regex) then `judge` (Claude Sonnet) for final
   status/sentiment/competitor-framing. `ownership` tags each URL by domain; ads by advertiser.
4. **persist + diff** — `persist` writes a dated snapshot + rolling history; `compute_metrics` +
   `print_diff` produce `metrics.csv` (the time series) and the run-over-run delta.

### Track B — LLM Visibility (brand presence in live AI answers)

Runs via `run_llm_visibility()`, invoked by `--llm`.

1. **discover_llm_visibility** — sends each selected prompt to each selected **surface** (ChatGPT via the
   `tri_angle/gpt-search` Apify actor; Perplexity via the **sonar API directly**, `perplexity_complete()`).
   **3 samples per (prompt × surface)** by default, pooled for CIs. Parallel (ThreadPoolExecutor).
   Resume skips **(surface, run_index, prompt)** combos already written today (per-prompt, not per-run).
2. **judge_llm_response** — Claude Sonnet classifies each response: mentioned, rank, recommended,
   stale_product_described, stale_excerpt, sources_cited, sentiment, competitors, confidence.
3. **verify_stale_attribution** + **derive_action** — attribution is quote-grounded: a cited source is
   only blamed for a stale claim if it's verified to actually contain stale text (already stale/mixed
   in the Track A web history, already in the fetch cache and trips the claim regex, or freshly
   fetched). `derive_action` then assigns one prioritized, source-targeted action per row from
   `verified_stale_sources` — never "fix our own page" just because the brand site appears in the
   citation list (the misattribution bug this replaced). `--reeval` re-runs this step alone on today's
   already-captured responses (no re-query, no crawl, no fetch of new sources).
4. **persist_llm** → `llm_visibility_<date>.csv` + history; `export_llm_csv` →
   `llm_visibility_latest.csv` (clickable sources, `action`, `priority`, error notes).
5. **compute_llm_metrics** — Wilson CI for binary rates, mean±SE for sentiment, per-surface breakdown.

Apify owns discovery/enrichment; our code owns classify → persist → metrics.

### Actor inventory

| Actor slug | Purpose | Notes |
|---|---|---|
| `apify/google-search-scraper` | SERP + AI Overview + GPT/Perplexity panels; News via `tbm=nws` | Stable |
| `apify/website-content-crawler` | Full page text for enrichment | Stable |
| `trudax/reddit-scraper-lite` | Reddit posts/comments | Residential/US proxy |
| `lexis-solutions/google-ads-scraper` | Google Ads Transparency Center (by advertiser URL) | Driven by `config.ads_start_urls`; **not** keyword search |
| `tri_angle/gpt-search` | Live ChatGPT web search (Track B) | Input `prompts` + `country`; output `{prompt, response, citations}`. Needs one-time actor approval. |
| **Perplexity sonar API** (not an actor) | Live Perplexity answers (Track B) | `perplexity_complete()` → `api.perplexity.ai`; needs `PERPLEXITY_API_KEY`. Web scrapers (zhorex) are anti-bot-walled. |

**Removed (do not re-add without a new requirement):** Bing, Instagram, X/Twitter, YouTube, TikTok,
`fayoussef/bulk-llm-runner` (API/training-data), `scrape.badger/google-ai-mode-scraper`, the Gemini
actor. History in git if needed.

## Repo layout

```
config.json     control surface (queries, ads_start_urls, llm_visibility_prompts, llm_surfaces,
                claim regexes, domain lists, actor slugs, limits)
pipeline.py     orchestrator (both tracks) + CLI with interactive selection
docs/           REQUIREMENTS.md + DESIGN.md (source of truth for scope/design)
tests/          offline pytest suite (network deps stubbed in conftest.py)
data/           outputs (gitignored), CSV only:
                root cumulative: observations_history.csv, llm_visibility_history.csv,
                  metrics.csv, llm_metrics.csv, fetch_cache.csv
                per run: data/<timestamp>__<track>__<surfaces>__<n>items.../ (self-contained)
README.md       setup + run + cost + scheduling
```

## Commands

```bash
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)        # APIFY_TOKEN, ANTHROPIC_API_KEY (+ optional PERPLEXITY_API_KEY)
python pipeline.py --refresh               # Track A; interactive source/query multiple-choice
python pipeline.py --llm                    # Track B; interactive surface/prompt multiple-choice
python pipeline.py --llm --surfaces chatgpt --prompts 1,7 --num-runs 1 -y   # scripted, no prompts
python pipeline.py --llm --surfaces chatgpt --extra-prompts "Inito vs Oova::comparison" -y  # ad-hoc one-off prompt
python pipeline.py --diff-only             # recompute metrics + diff, no crawling
python pipeline.py --reeval                # Track B: re-run attribution/action/metrics on today's
                                            #   stored responses only — no re-query, no crawl
pytest -q                                   # offline tests
```

Run scoping: omit `--sources/--queries/--surfaces/--prompts` for an interactive multiple-choice menu;
pass them (comma-separated indices or name substrings, or `all`) for scripted runs; add `-y` to take
all/specs without prompting. `--num-runs` overrides samples-per-(prompt×surface). `--note` is folded
into the run-folder name. `--extra-prompts` injects Track B **ad-hoc one-off** prompts not in config
(`;`-separated, each optionally `text::intent`, default intent `adhoc`) — run + judged once, **never
written to config** (keeps `llm_visibility_prompts` append-only), deduped against the config selection.
`--force` ignores today's per-(surface, run, prompt) resume state and re-queries everything selected.

## Skills (`.claude/skills/`)

Workflow skills encode the conventions below so feature work stays safe and fast. Use them:

- **geo-safe-change** — read first for any change; the invariants + verify loop (this section, expanded).
- **geo-add-source** — add a Track A discovery source (Apify actor).
- **geo-add-llm-surface** — add a Track B live-web assistant.
- **geo-tune-classifier** — adjust claim regex / judge prompts without false positives.
- **geo-dry-run** — hermetic end-to-end verification (stubbed actors + Claude); ships a harness template.

## Invariants — do not break these

- **`config.json` queries + llm_visibility_prompts are append-only.** Editing an existing string
  silently breaks the time series. Add a new entry and leave the old one.
- **The LLM judge is the arbiter of `status`, not regex.** `claim_patterns` are cheap heuristics that
  feed the judge and serve as the offline fallback. Improve recall by adding patterns; never rely on
  regex alone. Never add patterns for shared attributes (hormones/app/dip-strip) — they aren't stale.
- **Discovery must never break the core.** Each Track A source runs under `_safe_discover` (try/except);
  each Track B surface fails fast into one error row per prompt. Keep failures visible, not fatal.
- **`owned_stale` is the headline metric.** Inito's own pages/ads (owned / owned_marketplace) carrying
  stale claims are the priority; this number should trend to zero first.
- **Model id lives only in `config.json` (`limits.judge_model`).** Currently `claude-sonnet-4-6`.
  Don't hardcode it elsewhere. IDs: https://docs.claude.com/en/docs/about-claude/models
- **CSV only.** No parquet. CSV-roundtripped booleans come back as strings — coerce via `_coerce_web` /
  `_coerce_llm` (or `_to_bool`) before doing math on them.

## Ownership rules (`ownership()` / `ownership_for_ad()`)

- `owned` — inito.com + subdomains; the Inito app on App Store / Google Play (matched by app id);
  ads whose advertiser contains "inito".
- `owned_marketplace` — amazon.com `/dp/` ASINs (Inito's own listings; verify seller in edge cases).
- `competitor` — domains in `config.competitor_domains` (miracare, proovtest, ovul, …); ads matched by
  the domain label.
- `third_party` — everything else, incl. app-store pages for non-Inito apps.

## Gotchas

- **Actor slugs/input schemas drift.** Before first run, confirm each `config.actors` entry on its
  Apify Store page and align the `run_input` builders. SERP + Content Crawler (Apify-official) are
  stable; the others vary.
- **Ads = Transparency Center, not keywords.** `lexis-solutions/google-ads-scraper` takes
  `startUrls` (one advertiser/domain URL each). Populate `config.ads_start_urls` from
  https://adstransparency.google.com (keep `region=US`). Empty list → ads source skipped.
- **Track B sampling:** the 3 samples per (prompt × surface) capture model variance. ChatGPT pins US via
  the actor's `country` field; Perplexity (sonar API) has no IP control. The earlier "3 distinct US IPs"
  goal is aspirational, not enforced — don't claim it as a guarantee.
- **AI Overviews** are stored as pseudo-URLs `aioverview::<query>` (also `chatgptsearch::`,
  `perplexitysearch::`) so the verbatim answer per query is tracked; excluded from page-fetch enrichment.
  Note: `ai_overview` rows are NOT in `_WEB_PLATFORMS`, so they don't count toward `stale_or_mixed`/
  `owned_stale` today (see `docs/OPEN-ITEMS.md`).
- **Cost:** Sonnet for the judge (accuracy over Haiku — accepted); scope runs with the CLI selectors;
  fetch cache skips URLs seen < 7 days; SERP depth is 20 results/query.
- **LLM resume** reads `llm_visibility_history.csv` to skip **(surface, run_index, prompt)** combos done
  today (per-prompt). If something is skipped incorrectly, delete today's rows from that CSV.
- **Empty actor responses** are flagged `status=empty` and never judged (judging empty text fabricates
  signals). Per-prompt fail-fast: one bad prompt/response becomes one error/empty row, batch continues.

## Tests

`pytest -q` runs fully offline — `tests/conftest.py` stubs `apify_client` and `anthropic` before import,
so `judge()`/`judge_llm_response()` exercise their deterministic fallbacks and no network/keys are
needed. When you change `detect_claims`, `ownership`, discovery parsing, the action engine, the
selection resolver, or the metrics math, add/extend a test. The suite guards the two original
regressions (the `attach_to_phone` gap, the `searchQuery` dict-or-string shape) plus the new not-stale
false-positive guard — keep those covered.

## Definitely don't

- Commit `.env` or `data/` (both gitignored).
- Put secrets in `config.json`.
- Add a new surface/provider call without routing the model id + actor slug through `config.json`.
- Reintroduce parquet, the removed actors, or training-data-only LLM calls.
