# Requirements — Inito GEO Monitor (lean snapshot model)

> Rewritten 2026-06-24. The earlier version specified a time-series, staleness-scoring system; per
> stakeholder feedback the product pivoted to a **lean per-run snapshot** of what sources say about the
> brand. This doc reflects the current scope. Git history holds the old spec.

## 1. Problem

We need an on-demand, repeatable read on **how Inito is currently portrayed** across the open web and
live AI assistants: what each source says about the brand, whether it brings up competitors (and what it
says), the links and prices it shows, and — for AI answers — whether Inito is mentioned / recommended.
A recurring concern is sources still describing the **old phone-dependent product** as if it were current.

## 2. Goals

- One command → a clean, self-contained **snapshot** (CSV) of current brand portrayal.
- Two surfaces: the web/SERP (Track A) and live AI assistants (Track B).
- Smallest set of files with only the most relevant data; lean, human-readable sheets.
- Capture, per source: a plain-language summary of what it says about Inito, competition mentions +
  summary, links, price, sentiment; plus mentioned/rank/recommended for AI answers.

### Non-goals
- **No time series / trend tracking** — each run stands alone (diff run folders externally if ever needed).
- No staleness *score* or `owned_stale` headline metric — "still describes the old product" is captured
  inside the narrative, not as a tracked status column.
- No generic (training-data / non-web) LLM runs yet — deferred; both surfaces are live-web-grounded.

## 3. Product knowledge — old vs. current *(authoritative for the judge's narrative)*

The judge must get old-vs-current right and **avoid false positives** on attributes common to both
products. It surfaces this inside `says_about_inito` (e.g. "still describes the old iPhone-clip product"),
**not** as a separate status column.

### 3.1 OLD product (note it explicitly if presented as current)
| Old signal | Example phrasing |
|---|---|
| iPhone-only / iOS-only | "Inito only works with iPhone" |
| No Android support | "not available on Android" |
| Attaches/clips to the phone | "clip it onto your phone" |
| Camera-as-sensor | "uses your iPhone's camera to read the strip" |
| Lightning / charging-port dependence | "plug into the Lightning port" |
| Specific old iPhone model requirements | "requires iPhone 7 or newer" |

### 3.2 CURRENT product — the InSight Wireless Reader
"InSight Wireless Reader", "Wi-Fi enabled", "works on both iOS and Android", "built-in optical sensor",
"Spectral Mapping", "no phone camera needed".

### 3.3 NOT noteworthy — common to BOTH products (must NOT be called out as old)
Four hormones (Estrogen/E3G, LH, PdG, FSH) on one strip; the companion app; dip-the-strip workflow;
accuracy/clinical-validation claims; "syncs results to your phone app" (≠ clipping to the phone / using
its camera).

### 3.4 Other dimensions captured
- **Ownership** — `owned` (any `*.inito.com`) | `owned_marketplace` (amazon /dp/) | `competitor` | `third_party`.
- **nonprod_url** — boolean: the URL / a cited source is a non-production Inito host (preprod./staging./…).
- **Intent** — `brand_entity`, `attribute_probe`, `category`, `comparison`, `adversarial`, `community`, `use_case`, `purchase`.
- **Sentiment** toward Inito (−1..+1); **competition** (mentions + summary + named brands).

## 4. Functional requirements

### 4.1 Track A — Web/SERP → `web_observations.csv` (1 row per source)
- Discover via Google SERP (+AI Overview & GPT/Perplexity panels), Google News, Google Ads Transparency
  Center (config-driven), Reddit. Each source isolated under `_safe_discover`.
- Enrich real URLs to full page text (Website Content Crawler), 7-day CSV fetch cache.
- Classify each source with the Claude judge → `says_about_inito`, competition fields, sentiment, price;
  extract outbound links; tag ownership + nonprod.
- Output columns: see `WEB_COLUMNS` in `pipeline.py`.

### 4.2 Track B — LLM visibility → `llm_observations.csv` (1 row per surface × prompt × run)
- Surfaces: ChatGPT (`tri_angle/gpt-search` Apify actor, US-pinned) and Perplexity (sonar API direct).
  Both are **live-web-grounded** (no training-data-only calls).
- `num_runs` samples per (prompt × surface), default 5, **one row per run** (no aggregation, no resume).
- Judge each response → mentioned/rank/recommended, `says_about_inito`, competition, sentiment, price,
  sources_cited. Cited URLs are **canonicalised** (tracking params stripped) and nonprod hosts flagged.
- Empty/error responses become visible `status=empty/error` rows (never judged).
- Output columns: see `LLM_COLUMNS` in `pipeline.py`.

### 4.3 Shared judge
Claude (`limits.judge_model`, currently `claude-sonnet-4-6`) via a forced tool call. Cheap regex
(`detect_claims`) only hints the judge + drives the offline fallback (price + old-product detection).

### 4.4 CLI
`--list-topics`, `--refresh` (Track A), `--llm` (Track B). Interactive multiple-choice selection when
selectors are omitted; `--sources/--queries/--surfaces/--prompts` (indices/names/`all`), `-y`,
`--num-runs`, `--extra-prompts`, `--note`.

### 4.5 Configuration (`config.json`)
`topics` (freely editable shared catalog — one `query` per topic, used by both tracks), `ads_start_urls`, `llm_surfaces`, `llm_num_runs`,
`claim_patterns`/`current_signal_patterns`/`price_pattern`, `owned_domains`/`competitor_domains`/
`competitor_brands`, `actors`, `limits`.

## 5. Non-functional
- **CSV only.** Each run self-contained under `data/<timestamp>__…/`. Only cross-run file: `fetch_cache.csv`.
- Failures visible, never fatal. Secrets only in `.env`. `data/` + `.env` gitignored.
- Offline test suite (`pytest -q`) with network deps stubbed.

## 6. Assumptions & risks
- Apify actor slugs/schemas drift — confirm before first run.
- ChatGPT actor reliability varies (anti-bot/approval); Perplexity needs a key; Reddit needs a residential proxy.
- Only ChatGPT pins US; Perplexity has no IP control.
