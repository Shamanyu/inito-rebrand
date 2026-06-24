# CLAUDE.md

Guidance for Claude Code (and humans) working in this repo.

## What this is

A GEO (generative engine optimization) monitor for the brand **Inito**. Each run produces a
**self-contained snapshot** of what web pages and live AI-assistant answers currently say about Inito:
a per-source narrative (`says_about_inito`), whether competitors come up and what is said about them,
the links/prices shown, sentiment, and brand-visibility signals (mentioned / rank / recommended) for
LLM answers. **There is no time series** — nothing accumulates across runs (only a fetch cache, for
cost). Want a fresh picture, run it again.

Product context (drives the narrative, not a status column): the OLD product clipped onto an iPhone and
used the phone camera/Lightning port (iPhone-only). The **current** product is the **InSight Wireless
Reader** — standalone, Wi-Fi, works on iOS *and* Android, no camera/clip. Both products measure the same
four hormones (E3G, LH, PdG, FSH), use a companion app, and use a dip-strip workflow — **those shared
attributes are unremarkable.** The judge should call out in `says_about_inito` when a source still
presents the OLD phone-dependent product as if current. See `docs/REQUIREMENTS.md` for the taxonomy.

## Architecture

Two independent tracks, both in `pipeline.py`. Outputs are **CSV only**. Every run writes a
self-contained, timestamped folder under `data/` containing just that run's lean sheet(s).

### Track A — Web/SERP → `web_observations.csv` (1 row per source)

1. **discover** (parallel) — Google SERP (`discover_serp`, incl. AI Overview + GPT/Perplexity panels),
   Google News (`discover_news`), Google Ads Transparency Center (`discover_ads`), Reddit
   (`discover_reddit`). Reddit needs a residential/US proxy (datacenter → 403). Runs under `_safe_discover`.
2. **enrich** — `enrich_content` runs the Website Content Crawler over deduped URLs → full page text
   (CSV fetch cache, 7-day TTL — the only cross-run file).
3. **classify** — `classify_web_record` calls `detect_claims` (regex hints: price + old-product) then
   `judge` (Claude) for the narrative fields; `ownership` tags each URL by domain (suffix match);
   `extract_links` pulls outbound links; `is_nonprod_owned` flags preprod/staging Inito hosts.
4. **write** — `write_web_sheet` → `web_observations.csv` (columns in `WEB_COLUMNS`).

### Track B — LLM Visibility → `llm_observations.csv` (1 row per surface × prompt × run)

Runs via `run_llm_visibility()`, invoked by `--llm`.

1. **discover_llm_visibility** — sends each selected prompt to each selected **surface** (ChatGPT via the
   `tri_angle/gpt-search` Apify actor; Perplexity via the **sonar API directly**, `perplexity_complete()`).
   `llm_num_runs` samples per (prompt × surface), each its own row — **no aggregation, no resume**.
   Parallel (ThreadPoolExecutor).
2. **judge_llm_response** — Claude classifies each response: mentioned, rank, recommended,
   `says_about_inito`, competition fields, sentiment, price, sources_cited.
3. **write** — `_llm_row` canonicalises cited URLs (strips tracking params) and flags `nonprod_url`;
   `write_llm_sheet` → `llm_observations.csv` (columns in `LLM_COLUMNS`).

Apify owns discovery/enrichment; our code owns classify → write.

### Actor inventory

| Actor slug | Purpose | Notes |
|---|---|---|
| `apify/google-search-scraper` | SERP + AI Overview + GPT/Perplexity panels; News via `tbm=nws` | Stable |
| `apify/website-content-crawler` | Full page text for enrichment | Stable |
| `trudax/reddit-scraper-lite` | Reddit posts/comments | Residential/US proxy |
| `lexis-solutions/google-ads-scraper` | Google Ads Transparency Center (by advertiser URL) | Driven by `config.ads_start_urls`; **not** keyword search |
| `tri_angle/gpt-search` | Live ChatGPT web search (Track B) | Input `prompts` + `country`; output `{prompt, response, citations}`. Needs one-time actor approval. |
| **Perplexity sonar API** (not an actor) | Live Perplexity answers (Track B) | `perplexity_complete()` → `api.perplexity.ai`; needs `PERPLEXITY_API_KEY`. |

**Removed (do not re-add without a new requirement):** Bing, Instagram, X/Twitter, YouTube, TikTok,
the Gemini actor, training-data-only LLM calls. Also **removed in the lean rewrite:** the whole
time-series layer (history CSVs, `metrics.csv`/`llm_metrics.csv`, Wilson/mean CIs, the `status`
stale/mixed/current column, `owned_stale`, the quote-grounded stale-attribution + action engine,
`--diff-only`/`--reeval`/`--force`, cross-run resume). History is in git if ever needed.

## Repo layout

```
config.json     control surface (topics [shared catalog: one query per topic, both tracks], ads_start_urls, llm_surfaces, claim regexes,
                domain + competitor_brand lists, actor slugs, limits)
pipeline.py     orchestrator (both tracks) + CLI with interactive selection
docs/           REQUIREMENTS.md + DESIGN.md (scope/design) + OPEN-ITEMS.md
tests/          offline pytest suite (network deps stubbed in conftest.py)
data/           outputs (gitignored), CSV only:
                  per run: data/<timestamp>__<track>__<surfaces>__<n>items.../web_observations.csv
                           or llm_observations.csv (self-contained)
                  cross-run: fetch_cache.csv only (cost saver, not analytics)
README.md       setup + run + cost
```

## Commands

```bash
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)        # APIFY_TOKEN, ANTHROPIC_API_KEY (+ optional PERPLEXITY_API_KEY)
python pipeline.py --list-topics            # show the editable topic catalog
python pipeline.py --refresh                # Track A; interactive source/query selection
python pipeline.py --llm                     # Track B; interactive surface/prompt selection
python pipeline.py --llm --surfaces chatgpt --prompts 1,7 --num-runs 1 -y   # scripted
python pipeline.py --llm --surfaces chatgpt --extra-prompts "Inito vs Oova::comparison" -y  # ad-hoc prompt
pytest -q                                    # offline tests
```

Run scoping: omit `--sources/--queries/--surfaces/--prompts` for an interactive multiple-choice menu;
pass them (comma-separated indices or name substrings, or `all`) for scripted runs; add `-y` to take
all/specs without prompting. `--num-runs` overrides samples-per-(prompt×surface). `--note` is folded
into the run-folder name. `--extra-prompts` injects Track B ad-hoc one-off prompts (`;`-separated, each
optionally `text::intent`) — run + judged once, never written to config, deduped against the selection.

## Skills (`.claude/skills/`)

Workflow skills encode older conventions; the lean rewrite changed several. Re-read the code as the
source of truth. Still broadly useful:

- **geo-safe-change** — read first for any change; the invariants + verify loop (note: time-series/
  staleness invariants no longer apply).
- **geo-add-source** — add a Track A discovery source (Apify actor).
- **geo-add-llm-surface** — add a Track B live-web assistant.
- **geo-tune-classifier** — adjust claim regex / judge prompts without false positives.
- **geo-dry-run** — hermetic end-to-end verification (stubbed actors + Claude). The template references
  removed metrics; assert on the two lean sheets instead.

## Invariants — do not break these

- **The judge writes the narrative; regex only hints.** `claim_patterns` are cheap heuristics that feed
  the judge and its offline fallback (price + "still describes the old product"). Never add patterns for
  shared attributes (hormones/app/dip-strip) — they aren't noteworthy.
- **Discovery must never break the core.** Each Track A source runs under `_safe_discover` (try/except);
  each Track B surface fails fast into one error/empty row per prompt. Keep failures visible, not fatal.
- **Empty actor responses** are flagged `status=empty` and never judged (judging empty text fabricates
  signals). Per-prompt fail-fast: one bad prompt becomes one error/empty row, the batch continues.
- **Snapshot, not series.** Do NOT reintroduce history/metrics/diff files or cross-run state. Each run
  folder is self-contained; the only persistent file is `fetch_cache.csv`.
- **Two lean sheets, fixed columns.** `WEB_COLUMNS` / `LLM_COLUMNS` are the output contract (tests assert
  the writers emit exactly these). Add a column by extending the list + the row builder + a test.
- **Cited URLs are canonical.** `normalize_url` strips tracking params (utm_*, disc_code, os, workflow,
  fragment) — keep it that way so citations dedupe and stay clickable.
- **Model id lives only in `config.json` (`limits.judge_model`).** Currently `claude-sonnet-4-6`.
  IDs: https://docs.claude.com/en/docs/about-claude/models
- **CSV only.** No parquet.

## Ownership rules (`ownership()` / `ownership_for_ad()`)

- `owned` — any `*.inito.com` host (suffix match, incl. preprod./staging.); the Inito app on App Store /
  Google Play (matched by app id); ads whose advertiser contains "inito".
- `owned_marketplace` — amazon.com `/dp/` ASINs (Inito's own listings; verify seller in edge cases).
- `competitor` — domains in `config.competitor_domains` (+ subdomains, suffix match); ads matched by the domain label.
- `third_party` — everything else, incl. app-store pages for non-Inito apps.
- `nonprod_url` (separate boolean column) — True when the row's URL / a cited source is a non-production
  Inito host (`preprod.`, `staging.`, `dev.`, …). Still `owned`, but flagged because such a page should
  not be publicly reachable or cited.

## Gotchas

- **Actor slugs/input schemas drift.** Confirm each `config.actors` entry on its Apify Store page before
  a first run. SERP + Content Crawler (Apify-official) are stable; the others vary.
- **Ads = Transparency Center, not keywords.** `lexis-solutions/google-ads-scraper` takes `startUrls`
  (one advertiser/domain URL each). Empty `config.ads_start_urls` → ads source skipped.
- **AI Overviews / SERP panels** are stored as pseudo-URLs (`aioverview::<query>`, `chatgptsearch::`,
  `perplexitysearch::`); excluded from page-fetch enrichment (judged on their snippet text).
- **US pinning:** ChatGPT pins US via the actor's `country` field; Perplexity (sonar API) has no IP
  control — its samples capture model variance only.
- **Cost:** Sonnet for the judge; scope runs with the CLI selectors; fetch cache skips URLs seen < 7 days;
  SERP depth is 20 results/query.

## Tests

`pytest -q` runs fully offline — `tests/conftest.py` stubs `apify_client` and `anthropic` before import,
so `judge()`/`judge_llm_response()` exercise their deterministic fallbacks and no network/keys are
needed. When you change a discovery parser, `ownership`, `classify_web_record`, `_llm_row`, the sheet
writers, the selection resolver, or the column lists, add/extend a test. The suite guards the
`attach_to_phone` regex gap, the `searchQuery` dict-or-string shape, the not-stale false-positive guard,
preprod ownership, and citation canonicalisation — keep those covered.

## Definitely don't

- Commit `.env` or `data/` (both gitignored).
- Put secrets in `config.json`.
- Add a new surface/provider call without routing the model id + actor slug through `config.json`.
- Reintroduce parquet, the removed actors, training-data-only LLM calls, or the time-series/metrics layer.
