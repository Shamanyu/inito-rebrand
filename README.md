# Inito GEO Monitor

Repeatable pipeline that finds and scores pages making stale/competitive claims about Inito, then
tracks the "stale-source count" decaying over time as fixes land. Apify does the crawling
(residential proxies → gets past Reddit's 403 and social blocks + proper SERP depth); a Claude
Haiku judge does the classification.

## Pipeline

Two independent tracks run on the same schedule:

### Track A — Web/SERP (stale claim detection)

```
discover            enrich                 classify                 persist + diff
─────────           ──────                 ────────                 ──────────────
Google SERP   ─┐                     ┌─ regex (4 claim phrases,   ┌─ observations_<date>.parquet
Reddit        ─┤   Website Content   │   price, product name)     │  observations_history.parquet
Instagram     ─┼─► Crawler  ────────►├─ Claude Haiku judge        ├─ latest_snapshot.csv  (→ Sheets)
X / Twitter   ─┤   (full page text)  │   (status, sentiment,      └─ metrics.csv  (the time series)
YouTube       ─┘                     └─  competitor framing)          + console DIFF vs last run
```

Each row is one URL with: ownership (owned / owned_marketplace / competitor / third_party),
status (stale / mixed / current), the four claim flags, sentiment, competitor framing, and SERP rank.

### Track B — LLM Visibility (brand presence in AI answers)

```
discover_llm_visibility                classify              persist + metrics
───────────────────────                ────────              ────────────────
fayoussef/bulk-llm-runner ──────────► Claude Haiku judge ─► llm_visibility_<date>.parquet
  openai/gpt-5                          (mentioned, rank,    llm_visibility_history.parquet
  google/gemini-2.5-pro                  recommended,        llm_visibility_latest.csv (→ Sheets)
  perplexity/sonar-pro                   stale_claim,        llm_metrics.csv (time series)
  anthropic/claude-sonnet-4.5            sentiment,
                                         competitor_preferred,
                                         action)
```

Each row is one (model × prompt × run) observation. Perplexity/sonar-pro always does live web
search — its results reflect current third-party content, not just training data. The `action`
column tells you what to do: fix stale claim, create content, submit provider correction, etc.

## Repo layout

```
config.json     control surface — queries, claim regexes, domain lists, actor slugs, limits,
                llm_visibility_prompts, llm_models, llm_num_runs
pipeline.py     orchestrator for both tracks + CLI
CLAUDE.md       working context for Claude Code (invariants, conventions, gotchas)
tests/          offline pytest suite (network deps stubbed)
data/           outputs (gitignored)
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # add APIFY_TOKEN + ANTHROPIC_API_KEY
export $(grep -v '^#' .env | xargs)
```

Before the first run, open each actor's page in the Apify Store and confirm its **slug** and
**input schema** — actors get versioned and input keys differ (esp. Reddit/IG/X). Update
`config.json → actors` and the `run_input` builders in `pipeline.py` to match. The SERP and
Website Content Crawler are Apify-official and stable; the social ones vary most.

## Run

```bash
python pipeline.py --refresh              # full sweep (SERP + Reddit + social + LLM visibility)
python pipeline.py --refresh --no-social  # SERP + Reddit + LLM visibility (no IG/X/YouTube)
python pipeline.py --llm                  # LLM visibility only — fast, no crawling
python pipeline.py --diff-only            # recompute metrics + print diff, no crawling
```

Output lands in `data/`:
- `latest_snapshot.csv` — SERP track results → import to Google Sheets
- `llm_visibility_latest.csv` — LLM track results (one row per model × prompt) → import to Sheets
- `observations_history.parquet` — full SERP history
- `llm_visibility_history.parquet` — full LLM history
- `metrics.csv` — SERP decay curve: `stale_or_mixed`, `owned_stale`, per-claim counts, sentiment
- `llm_metrics.csv` — LLM time series: mention rate, stale rate, sentiment per model

## Config

`config.json` is the control surface and is git-versioned on purpose:
- **queries** — frozen query + intent. Never edit an existing query (breaks the series); add a new one.
- **claim_patterns** — regex heuristics; the judge resolves ambiguity. Add patterns as new phrasings appear.
- **owned/competitor_domains** — drives the ownership column and the `owned_stale` metric.
- **limits.judge_model** — `claude-haiku-4-5-20251001` for cost. Current IDs: https://docs.claude.com/en/docs/about-claude/models
- **llm_models** — models run via `fayoussef/bulk-llm-runner`. Currently: gpt-5, gemini-2.5-pro, perplexity/sonar-pro, claude-sonnet-4.5. Append-only.
- **llm_visibility_prompts** — brand prompts sent to each LLM. Frozen like SERP queries — add, never edit.

## Tests

```bash
pytest -q
```

Runs fully offline — `tests/conftest.py` stubs the Apify and Anthropic clients before import, so
`judge()` falls back to its deterministic regex path and no tokens are needed. 18 tests cover URL
normalization, ownership routing, claim detection (incl. the two regression cases), SERP parsing,
and the metrics/diff math.

## Cost control

Apify bills per actor compute/result and Claude per token. Levers: `--no-social`, the `limits.*`
caps, weekly (not daily) cadence, Haiku for the judge. A weekly SERP+Reddit run is the cheap core;
add social monthly. Cache: enrich only URLs new or unseen for N days (small extension — diff the
URL set against `observations_history.parquet` before calling the content crawler).

## Productionize (scheduled, cloud)

Same code, two extra files (`.actor/actor.json`, `Dockerfile`), then:

```bash
apify push                 # deploy this repo as an Actor
# Apify Console → Schedules → weekly cron → store to dataset / KV store
```

Swap the local parquet writes for `Actor.push_data()` / a KV store, and add a Slack webhook in
`print_diff` so the weekly delta posts itself. The on-demand and scheduled paths share one codebase.

## Known limits

- Social actors (IG/X) break when platforms change markup — they're wrapped in try/except so a
  social failure never kills the SERP+Reddit core. Check logs for `skipped`.
- AI Overview capture depends on the SERP actor surfacing it; it's logged as a pseudo-URL
  (`aioverview::<query>`) so you keep the verbatim AI answer per query over time.
- Ownership for app-store / Amazon pages is heuristic (Inito's own app ids + `/dp/` ASINs);
  verify seller/owner in edge cases.
- **LLM track**: ChatGPT/Gemini/Claude via API use training-data knowledge only — no live search.
  Perplexity/sonar-pro is the exception: it always searches the live web, so its results more
  closely match what consumers see in the product. This gap is why API results differ from manual
  searches in the web UI.
- **Google AI Mode** (`scrape.badger/google-ai-mode-scraper`) is excluded from the LLM visibility
  run: the actor has hardcoded 10-attempt exponential backoff on 5xx errors with no external kill
  switch. It remains available in `discover_google_ai_mode()` for use in full `--refresh` runs.
- **Perplexity web scraper** (`zhorex/perplexity-ai-scraper`) is Cloudflare-blocked (all datacenter
  and residential proxy attempts timeout). Perplexity coverage is handled via API (sonar-pro).

## Push to GitHub

This repo is git-initialized with a clean first commit. To put it on `Shamanyu/inito-rebrand`:

```bash
git remote add origin https://github.com/Shamanyu/inito-rebrand.git
git push -u origin main
```

If the remote already has commits, `git pull --rebase origin main` first. The push needs your own
GitHub auth (PAT or SSH) — credentials are never bundled here.
