# Inito GEO Monitor

Repeatable pipeline that finds and scores pages making stale/competitive claims about Inito, then
tracks the "stale-source count" decaying over time as fixes land. Apify does the crawling
(residential proxies → gets past Reddit's 403 and social blocks + proper SERP depth); a Claude
Haiku judge does the classification.

## Pipeline

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

## Repo layout

```
config.json     control surface — queries, claim regexes, domain lists, actor slugs, limits
pipeline.py     orchestrator (discover → enrich → classify → persist → diff) + CLI
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
python pipeline.py --refresh              # full sweep (SERP + Reddit + social)
python pipeline.py --refresh --no-social  # SERP + Reddit only — cheaper, good for weekly
python pipeline.py --diff-only            # recompute metrics + print diff, no crawling
```

Output lands in `data/`. Import `latest_snapshot.csv` into Google Sheets, or point Looker
Studio / your warehouse at `observations_history.parquet`. `metrics.csv` is the decay curve to
chart: `stale_or_mixed`, `owned_stale`, per-claim counts, `mean_sentiment`, `share_of_voice_category`.

## Config

`config.json` is the control surface and is git-versioned on purpose:
- **queries** — frozen query + intent. Never edit an existing query (breaks the series); add a new one.
- **claim_patterns** — regex heuristics; the judge resolves ambiguity. Add patterns as new phrasings appear.
- **owned/competitor_domains** — drives the ownership column and the `owned_stale` metric.
- **limits.judge_model** — `claude-haiku-4-5` for cost. Current IDs: https://docs.claude.com/en/docs/about-claude/models

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

## Push to GitHub

This repo is git-initialized with a clean first commit. To put it on `Shamanyu/inito-rebrand`:

```bash
git remote add origin https://github.com/Shamanyu/inito-rebrand.git
git push -u origin main
```

If the remote already has commits, `git pull --rebase origin main` first. The push needs your own
GitHub auth (PAT or SSH) — credentials are never bundled here.
