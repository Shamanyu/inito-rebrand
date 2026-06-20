# Inito GEO Monitor

Repeatable pipeline that finds and scores pages + live AI-assistant answers making stale/competitive
claims about Inito, then tracks the "stale-source count" decaying over time as fixes land. Apify does
the crawling and live-assistant access; a Claude Sonnet judge does the classification. **All outputs are
CSV**; every run lands in its own self-contained, timestamped folder.

See `docs/REQUIREMENTS.md` and `docs/DESIGN.md` for the full spec and design.

## Pipeline

Two independent tracks, selected from the CLI:

### Track A — Web/SERP (stale claim detection)

```
discover (parallel)        enrich              classify                persist + diff
─────────────────────      ──────              ────────                ──────────────
Google SERP (+AI Overview) ─┐                  ┌─ regex (claim phrases, ┌─ observations_<date>.csv
Google News               ─┤  Website Content  │  price, product name)  │  observations_history.csv
Google Ads (Transparency) ─┼─►Crawler ────────►├─ Claude Sonnet judge   ├─ latest_snapshot.csv (→Sheets)
Reddit                    ─┘  (full page text) └─ (status, sentiment,    └─ metrics.csv (time series)
                                                  competitor framing)        + console DIFF vs last run
```

Each row is one URL/ad with: ownership (owned / owned_marketplace / competitor / third_party), status
(stale / mixed / current), the four claim flags, sentiment, competitor framing, and SERP rank.

### Track B — LLM Visibility (brand presence in live AI answers)

```
discover (parallel, live web)        classify              persist + metrics
─────────────────────────────        ────────              ─────────────────
ChatGPT  (tri_angle/gpt-search) ───► Claude Sonnet judge ─► llm_visibility_<date>.csv
Perplexity (sonar API, direct)       (mentioned, rank,      llm_visibility_history.csv
  3 samples / (prompt×surface),       recommended, stale,   llm_visibility_latest.csv (→Sheets)
  pooled for Wilson CIs               sentiment, sources,   llm_metrics.csv (time series)
                                      action + priority)    llm_visibility_stats.csv (per-prompt CIs)
```

Both surfaces search the live web and cite sources, so every stale claim is traceable to a fixable
page. The `action` column says what to do (fix our page, outrank a source, build comparison content,
…), sorted by `priority`.

## Repo layout

```
config.json     control surface — queries, ads_start_urls, llm_visibility_prompts, llm_surfaces,
                claim regexes, domain lists, actor slugs, limits
pipeline.py     orchestrator (both tracks) + CLI with interactive multiple-choice selection
docs/           REQUIREMENTS.md + DESIGN.md
tests/          offline pytest suite (network deps stubbed)
data/           outputs (gitignored, CSV only)
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # APIFY_TOKEN + ANTHROPIC_API_KEY (+ optional PERPLEXITY_API_KEY)
export $(grep -v '^#' .env | xargs)
```

`PERPLEXITY_API_KEY` is optional — only the Perplexity surface needs it (sonar API). Without it,
that surface cleanly emits error rows and the rest of the run is unaffected.

Before the first run, open each actor's page in the Apify Store and confirm its **slug** and **input
schema** (they get versioned). For ads, populate `config.ads_start_urls` with Google Ads Transparency
Center URLs (one advertiser/domain each; keep `region=US`) — Inito's own first to catch stale ad copy.

## Run

```bash
python pipeline.py --refresh     # Track A — interactive: pick sources + queries
python pipeline.py --llm          # Track B — interactive: pick surfaces + prompts
python pipeline.py --diff-only    # recompute metrics + diff, no crawling
python pipeline.py --reeval       # Track B — re-run attribution/action/metrics on today's stored
                                  #   responses; no ChatGPT/Apify re-query, no crawling
```

Run however you want — scope with selectors, or omit them for a multiple-choice menu:

```bash
python pipeline.py --llm --surfaces chatgpt --prompts 1,7 --num-runs 1 -y
python pipeline.py --llm --surfaces chatgpt --extra-prompts "Inito vs Oova::comparison" --num-runs 1 -y
python pipeline.py --refresh --sources serp,reddit --queries "Inito vs Mira" -y
```

- `--surfaces / --prompts / --sources / --queries` — comma-separated **indices or name substrings**, or `all`.
- `-y / --yes` — non-interactive (use specs / all, no prompts).
- `--num-runs` — samples per (prompt × surface); default 3 (`config.llm_num_runs`).
- `--extra-prompts` — Track B ad-hoc **one-off** prompts not in config; `;`-separated, each optionally
  `text::intent` (default intent `adhoc`). Run + judged once, **never written to config** (the time
  series stays append-only). Deduped against the selected config prompts.
- `--force` — Track B: ignore today's resume state and re-query everything selected (for an intentional
  re-run within the same day).
- `--note` — short note folded into the run-folder name.

Each run writes a descriptive folder, e.g.
`data/2026-06-20T143005__llm__chatgpt+perplexity__7items__3runs__weekly/` containing all of that run's
CSVs plus a `run_info.csv`. Cumulative history + time series live at the `data/` root.

## Config

`config.json` is the control surface and is git-versioned on purpose:
- **queries / llm_visibility_prompts** — frozen + intent. Never edit an existing entry (breaks the series); add a new one.
- **ads_start_urls** — Google Ads Transparency Center URLs (advertiser/domain).
- **claim_patterns** — regex heuristics; the judge resolves ambiguity. Add patterns for new phrasings (never for shared attributes like hormones/app).
- **owned/competitor_domains** — drive the ownership column and the `owned_stale` metric.
- **llm_surfaces** — live-web assistants to query (`chatgpt` via Apify actor, `perplexity` via sonar API).
- **limits.judge_model** — `claude-sonnet-4-6` (Opus available for max accuracy). IDs: https://docs.claude.com/en/docs/about-claude/models

## Tests

```bash
pytest -q
```

Runs fully offline — `tests/conftest.py` stubs the Apify and Anthropic clients before import, so the
judges fall back to their deterministic regex paths and no tokens are needed. Covers URL normalization,
ownership (incl. ads), claim detection (incl. regressions + the not-stale false-positive guard), SERP +
ads parsing, the CSV fetch cache, metrics/diff, the action engine + cross-track linkage, the selection
resolver, and run-folder naming.

## Cost control

Apify bills per actor compute/result and Claude per token. Levers: scope runs with the CLI selectors,
the `limits.*` caps, weekly (not daily) cadence, the 7-day fetch cache. Track B cost scales with
surfaces × prompts × `num_runs` — drop `--num-runs` for cheap spot-checks.

## Productionize (scheduled, cloud)

Same code, two extra files (`.actor/actor.json`, `Dockerfile`), then `apify push` and a weekly cron in
the Apify Console. Add a Slack webhook in `print_diff` so the weekly delta posts itself. On-demand and
scheduled share one codebase.

## Known limits

See `docs/OPEN-ITEMS.md` for the live, focused list. In brief:
- ChatGPT (Apify actor) depends on scraper reliability (anti-bot / approval); Perplexity (sonar API) is
  reliable but needs a key. Failures fail fast into visible error rows.
- The 3 samples capture model variance; only ChatGPT pins US (via the actor's `country`), Perplexity has
  no IP control. Reddit gets intermittent 429s.
- Ad→competitor matching is by domain label; ownership for app-store / Amazon / ads is heuristic.
