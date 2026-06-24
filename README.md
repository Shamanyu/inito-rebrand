# Inito GEO Monitor

Repeatable pipeline that captures **what web pages and live AI-assistant answers currently say about
Inito** — what they claim about the brand, whether they bring up competitors and what they say, the
links and prices they show — in a clean, per-run **snapshot**. Apify does the crawling and live-assistant
access; a Claude judge writes the per-source narrative. **All outputs are CSV.**

Each run is **self-contained**: it writes its own timestamped folder under `data/` with one or two lean
sheets and nothing else. There is **no time series and no cross-run state** (only a fetch cache, for
cost). Run it whenever you want a fresh picture.

See `docs/REQUIREMENTS.md` and `docs/DESIGN.md` for the full spec and design.

## Pipeline

Two independent tracks, selected from the CLI.

### Track A — Web/SERP → `web_observations.csv` (1 row per source)

```
discover (parallel)         enrich              classify + write
─────────────────────       ──────              ────────────────
Google SERP (+AI Overview) ─┐                   ┌─ Claude judge:
Google News               ─┤   Website Content   │   says_about_inito (narrative)
Google Ads (Transparency) ─┼─► Crawler ─────────►├─ mentions_competition + summary
Reddit                    ─┘   (full page text)  └─ links_on_source, price, sentiment
```

Columns: `source · url · query · intent · topic_id · ownership · says_about_inito ·
mentions_competition · competition_summary · competitors_named · sentiment · price · links_on_source ·
nonprod_url · title`.

### Track B — LLM Visibility → `llm_observations.csv` (1 row per surface × prompt × run)

```
discover (parallel, live web)         classify + write
─────────────────────────────         ────────────────
ChatGPT  (tri_angle/gpt-search) ───►  Claude judge:
Perplexity (sonar API, direct)         mentioned · rank · recommended
  num_runs samples / (prompt×surface), says_about_inito · competition · sentiment
  US-pinned, one row per run           sources_cited (canonical) · price
```

Columns: `surface · run · prompt · intent · topic_id · mentioned · rank · recommended ·
says_about_inito · mentions_competition · competition_summary · competitors_named · sentiment · price ·
sources_cited · nonprod_url · response_text · status · error_note`.

Cited links are **canonicalised** (tracking params like `utm_*`, `disc_code`, `os`, `workflow` are
stripped). Any non-production Inito host (`preprod.`, `staging.`, …) is counted as `owned` and flagged
`nonprod_url=True` so an accidentally-public/cited staging page is visible.

## Repo layout

```
config.json     control surface — topics (web+llm catalog), ads_start_urls, llm_surfaces,
                claim regexes, domain/brand lists, actor slugs, limits
pipeline.py     orchestrator (both tracks) + CLI with interactive multiple-choice selection
docs/           REQUIREMENTS.md + DESIGN.md
tests/          offline pytest suite (network deps stubbed)
data/           outputs (gitignored, CSV only) — one self-contained folder per run + fetch_cache.csv
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # APIFY_TOKEN + ANTHROPIC_API_KEY (+ optional PERPLEXITY_API_KEY)
export $(grep -v '^#' .env | xargs)
```

`PERPLEXITY_API_KEY` is optional — only the Perplexity surface needs it (sonar API). Without it, that
surface cleanly emits error rows and the rest of the run is unaffected.

Before the first run, open each actor's page in the Apify Store and confirm its **slug** and **input
schema** (they get versioned). For ads, populate `config.ads_start_urls` with Google Ads Transparency
Center URLs (one advertiser/domain each; keep `region=US`).

## Run

```bash
python pipeline.py --list-topics  # show the editable topic catalog
python pipeline.py --refresh      # Track A — interactive: pick sources + queries
python pipeline.py --llm           # Track B — interactive: pick surfaces + prompts
```

Scope with selectors, or omit them for a multiple-choice menu:

```bash
python pipeline.py --llm --surfaces chatgpt --prompts 1,7 --num-runs 1 -y
python pipeline.py --llm --surfaces chatgpt --extra-prompts "Inito vs Oova::comparison" -y
python pipeline.py --refresh --sources serp,reddit --queries "Inito vs Mira" -y
```

- `--surfaces / --prompts / --sources / --queries` — comma-separated **indices or name substrings**, or `all`.
- `-y / --yes` — non-interactive (use specs / all, no prompts).
- `--num-runs` — samples per (prompt × surface); default 5 (`config.llm_num_runs`). Each sample is its own row.
- `--extra-prompts` — Track B ad-hoc **one-off** prompts not in config; `;`-separated, each optionally
  `text::intent` (default intent `adhoc`). Run + judged once; not written to config. Deduped against the selection.
- `--note` — short note folded into the run-folder name.

Each run writes a descriptive folder, e.g.
`data/2026-06-20T143005__llm__chatgpt+perplexity__7items__5runs__weekly/` containing just that run's sheet(s).

## Config

`config.json` is the control surface and is git-versioned on purpose:
- **topics** — one catalog for BOTH tracks: each has `id` (readable join key), `intent`, a `web` phrasing
  (Google) and an `llm` phrasing (ChatGPT/Perplexity). **Freely editable** — add / remove / reword on
  demand; each run is a self-contained snapshot, so nothing breaks. `--list-topics` prints the set.
- **ads_start_urls** — Google Ads Transparency Center URLs (advertiser/domain).
- **claim_patterns** — cheap regex hints feeding the judge / its offline fallback (price + "still describes
  the old product"). Never add patterns for shared attributes (hormones/app/dip-strip).
- **owned_domains / competitor_domains** — drive the `ownership` column (suffix match, so every
  `*.inito.com` subdomain counts as owned). **competitor_brands** — names for competition detection.
- **llm_surfaces** — live-web assistants (`chatgpt` via Apify actor, `perplexity` via sonar API).
- **limits.judge_model** — `claude-sonnet-4-6` (Opus available for max accuracy). IDs: https://docs.claude.com/en/docs/about-claude/models

## Tests

```bash
pytest -q
```

Runs fully offline — `tests/conftest.py` stubs the Apify and Anthropic clients before import, so the
judges fall back to their deterministic paths and no tokens are needed. Covers URL canonicalisation,
ownership (incl. preprod subdomains + ads), nonprod flagging, link extraction, competition detection,
claim hints (incl. the not-stale false-positive guard), SERP + ads parsing, the CSV fetch cache, both
sheet writers, the selection resolver, run-folder naming, and a CLI end-to-end.

## Cost control

Apify bills per actor compute/result and Claude per token. Levers: scope runs with the CLI selectors,
the `limits.*` caps, the 7-day fetch cache. Track B cost scales with surfaces × prompts × `num_runs` —
drop `--num-runs` for cheap spot-checks.

## Known limits

See `docs/OPEN-ITEMS.md`. In brief:
- ChatGPT (Apify actor) depends on scraper reliability (anti-bot / approval); Perplexity (sonar API) is
  reliable but needs a key. Failures fail fast into visible error rows.
- Only ChatGPT pins US (via the actor's `country`); Perplexity has no IP control. Reddit gets intermittent 429s.
- Ad→competitor matching is by domain label; ownership for app-store / Amazon / ads is heuristic.
