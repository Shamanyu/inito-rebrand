# Software Design Document — Inito GEO Monitor

**Status:** **Target design** (revised 2026-06-20 per stakeholder decisions). Describes the architecture
we are building toward; where it differs from the current code, that is called out as a **change**.
**Companion:** see [REQUIREMENTS.md](REQUIREMENTS.md).

> **Changes from the as-built MVP captured in this revision**
> 1. **CSV-only persistence** — all parquet writes (snapshots, history, fetch cache) become CSV.
> 2. **Track A surfaces** = Google SERP (+ AI Overview, + ChatGPT/Perplexity panels), Google News,
>    **Google Ads (new)**, Reddit. **Removed:** Bing, Instagram, X, YouTube, TikTok.
> 3. **Track B surfaces** = **ChatGPT** (`tri_angle/gpt-search`) + **Perplexity**
>    (`zhorex/perplexity-ai-scraper`), both live-web with citations. **Removed:** the API bulk LLM
>    runner and all training-data-only sources. **Gemini / Google AI Mode dropped for now.**
> 4. **Judge model** upgraded Haiku → **Sonnet** (`claude-sonnet-4-6`), Opus selectable.
> 5. **Parallel + fail-fast everywhere**; failures become **error rows/notes in the sheet**, no slow retries.
> 6. **Action engine reworked** — source-targeted, ownership-aware, prioritized, with cross-track linkage.
> 7. **3× sampling from distinct US IPs** — every (prompt × surface) is queried 3 times, each via a
>    distinct Apify proxy session pinned to the US (FR-B2/FR-B2a).
> 8. **Per-run timestamped output folder** — each run writes all its CSVs into `data/run_<timestamp>/`;
>    cumulative history/series stay at the `data/` root (NFR11).

---

## 1. Overview

A **single-file batch pipeline** (`pipeline.py`) running two independent tracks that share one shape:

```
discover  →  enrich / classify  →  persist  →  compute metrics + diff
```

- **Track A (Web/SERP):** find public web/SERP/ads/Reddit sources, classify their claims about Inito, track stale-source decay.
- **Track B (LLM Visibility):** ask live-web AI assistants (ChatGPT, Perplexity) the brand prompts, classify answers + citations, track presence/accuracy.

Crawling and assistant access are delegated to **Apify actors**. Classification is a **Claude Sonnet judge**
with a deterministic regex fallback. State is **CSV files only** (no parquet, no DB). The unit of execution
is a CLI invocation; the same code runs locally or as a scheduled Apify Actor.

### Design philosophy

1. **Config is the control surface** — behavior changes live in `config.json`, not code.
2. **The LLM judge is the arbiter; regex is a cheap pre-filter + offline fallback.**
3. **Nothing fragile can kill the core** — every external call is isolated; **fail fast**, log an error note, continue.
4. **Maximize parallelism** — discovery sources, LLM surfaces, and per-prompt jobs run concurrently.
5. **Append-only CSV history** — dated snapshots + frozen queries/prompts = a trustworthy time series.
6. **Live-web fidelity** — Track B reflects what a real user sees, with real citations (the fix targets).

## 2. System Context

```
                       ┌──────────────────────────────────────────┐
   .env  ──────────►   │              pipeline.py (CLI)            │
   config.json ─────►  │   refresh() / run_llm_visibility()        │
                       └───────┬───────────────────────┬──────────┘
                               │                        │
                ┌──────────────▼───────────┐   ┌────────▼───────────┐
                │   Apify actors            │   │  Anthropic Claude   │
                │  Track A:                 │   │  Sonnet judge       │
                │   google-search-scraper   │   │  (classify_page /    │
                │   website-content-crawler │   │   analyze_llm_resp.) │
                │   google-ads-scraper      │   └─────────────────────┘
                │   reddit-scraper-lite     │
                │  Track B:                 │
                │   tri_angle/gpt-search    │
                │   zhorex/perplexity-...   │
                └──────────────┬───────────┘
                               │
                       ┌────────────────────────┐   ┌──────────────────┐
                       │  data/ (CSV only)       │ ─►│  Google Sheets    │
                       │  root: history, series, │CSV│  (analyst layer)  │
                       │        fetch_cache      │   └──────────────────┘
                       │  run_<RUN_TS>/: per-run │
                       │        snapshots+exports│
                       └─────────────────────────┘
```

**External deps:** Apify (crawling + assistant access), Anthropic (judge). **State:** CSV in `data/`.
**Consumer:** analysts via CSV → Sheets.

## 3. Module / Layer Decomposition (target)

`pipeline.py` stays a single module at this size. Logical layers:

| Layer | Functions (target) | Responsibility |
|---|---|---|
| **Bootstrap** | `_require_env`, `apify`/`claude` clients, `CFG`, `RUN_DATE`, **`RUN_TS`/`RUN_DIR`** *(new)*, path helpers `out()`/`root()` | Validate env, load config, init clients, stamp the run + create its output folder. |
| **Helpers** | `log`, `normalize_url`, `domain_of`, `run_actor` | URL hygiene + the single Apify call wrapper (fail-fast). |
| **Discover (A)** | `discover_serp`, `discover_news`, `discover_ads` *(new)*, `discover_reddit` | Turn actor outputs into normalized records. **Removed:** `discover_bing`, `discover_social`, `discover_google_ai_mode`, `discover_perplexity_web` (the latter is repurposed under Track B). |
| **Enrich (A)** | `enrich_content`, `load_fetch_cache`, `save_fetch_cache` | Fetch page text with a CSV TTL cache. |
| **Classify (shared)** | `detect_claims`, `judge`, `ownership`, `judge_llm_response` | Regex + Sonnet judges + ownership routing. |
| **Persist (A)** | `persist` | Dated CSV snapshot + CSV history + latest CSV + review queue. |
| **Metrics (A)** | `compute_metrics`, `_sov`, `_kappa_regex_vs_judge`, `_run_quality_score`, `print_diff` | Time series + quality + diff. |
| **Discover (B)** | `discover_chatgpt` (`tri_angle/gpt-search`), `discover_perplexity` (`zhorex`), + per-actor → row adapters | Query live assistants in parallel; capture answer + citations. **Removed:** `discover_llm_visibility` (bulk runner). |
| **Persist/Export (B)** | `persist_llm`, `export_llm_csv`, `export_serp_csv`, `derive_action`, `_sources_to_plain` | CSV outputs + action derivation. |
| **Metrics (B)** | `compute_llm_metrics`, `_wilson_ci`, `_mean_ci` | Wilson/mean CIs per prompt/surface/overall. |
| **Cross-track** | `link_stale_sources` *(new)* | Join Track B cited stale URLs to Track A observations (FR-ACT7). |
| **Orchestration** | `refresh`, `run_llm_visibility`, `diff_only`, `_safe_discover`, `__main__` | Parallel stage sequencing + CLI. |

## 4. Track A — Web/SERP Data Flow (target)

```
                 ┌─ discover_serp  (organic + AI Overview + GPT/Perplexity panels) ─┐
   refresh() ───►├─ discover_news  (google-search-scraper, tbm=nws) ────────────────┤
   (parallel)    ├─ discover_ads   (lexis-solutions/google-ads-scraper) ────────────┤──► [records]
                 └─ discover_reddit(trudax/reddit-scraper-lite, residential proxy) ──┘     │
                                                                                            ▼
                                         dedupe by normalize_url (keep best non-zero rank)
                                                                                            │
                                       enrich_content(urls)  ──► {url: page_text}           ▼
                                          │  (fetch_cache.csv, TTL 7d; skips pseudo-urls)
                                          ▼
                  per record: detect_claims(text) → judge(url,text,flags) → ownership(url)
                                          │
                                          ▼
                          persist(rows) ──► observations_<date>.csv
                                          │   observations_history.csv
                                          │   latest_snapshot.csv
                                          │   review_queue.csv (conf<0.6)
                                          ▼
                          compute_metrics(df_all, rows) → metrics.csv ──► print_diff
```

### 4.1 Discovery record schema (pre-classification)

```python
{ "url", "platform", "query", "intent", "rank", "title", "snippet" }
```

Ads carry extra fields folded into the standard shape: `platform="ads"`, `title`=ad headline,
`snippet`=ad copy, `url`=landing-page URL, plus an `advertiser` attribute. Pseudo-URLs still encode AI
answers captured in the SERP: `aioverview::<query>`, `chatgptsearch::<query>`, `perplexitysearch::<query>`.

### 4.2 Classified row schema (persisted, CSV)

`url, domain, platform, query, intent, rank, ownership, status, current_product_named,
claim_iphone_only, claim_attach_to_phone, claim_camera_dependent, claim_no_android,
price_mentioned, sentiment_inito, competitor_framing, confidence, title, run_date`
(+ `advertiser` for ads rows; + `error_note` where a source failed).

### 4.3 Dedup rule

Records keyed by `normalize_url`; on collision, the record with the **lowest non-zero SERP rank** wins.

## 5. Track B — LLM Visibility Data Flow (target)

```
run_llm_visibility()
   └─ parallel over surfaces × prompts × runs:
        ├─ discover_chatgpt   → tri_angle/gpt-search   (live ChatGPT search + citations)
        └─ discover_perplexity→ zhorex/perplexity...   (live Perplexity + citations, brand_monitor)
        │     each result → judge_llm_response() + merge actor citations + inline URLs
        │     resume: skip (surface, run_index) already in today's CSV history
        │     on actor failure: FAIL FAST → one error row per prompt (error_note set)
   └─ persist_llm(rows) → llm_visibility_<date>.csv + llm_visibility_history.csv
        └─ export_llm_csv() → llm_visibility_latest.csv (action, clickable sources, error notes)
   └─ link_stale_sources(rows) → cross-reference cited stale URLs with Track A observations
   └─ compute_llm_metrics(df) → llm_metrics.csv + llm_visibility_stats.csv
   └─ export_serp_csv() → serp_latest.csv
```

### 5.1 LLM observation row schema (CSV)

`run_date, run_index, surface, prompt, intent, response_text, inito_mentioned, inito_rank,
inito_recommended, stale_product_described, stale_excerpt, sources_cited(JSON string),
sentiment_inito, competitors_named(JSON string), competitor_preferred, confidence, action, priority,
error_note` (`error_note` populated on failure rows).

> Naming change: the prior `model` column becomes **`surface`** (ChatGPT/Perplexity are products/UIs, not API model ids).

### 5.2 Per-surface adapters

Each actor has a different input/output schema, so each surface has a thin adapter that:
1. Builds the actor `run_input` (prompts, **proxyConfiguration** per § 5.4, brand name / wait timeout as the actor requires).
2. Calls `run_actor` (fail-fast).
3. Maps each item to the common row schema, extracting **answer text + citations** (actor-provided +
   inline-regex URLs, deduped). For `zhorex` brand_monitor, fold in its pre-extracted
   mention/position/competitor signals as judge priors.

### 5.3 Parallelism & resume

- Jobs = `surfaces × num_runs` where **`num_runs = 3`**, run via a thread pool (cap ~10 concurrent).
- **Resume key** = `(surface, run_index)` present in `llm_visibility_history.csv` for `RUN_DATE` with real
  data (non-null `inito_mentioned`), so a crashed run re-runs only missing combos.

### 5.4 Distinct-IP sampling (FR-B2/FR-B2a)

Goal: the 3 samples per (prompt × surface) come from **3 different US IPs**, so we sample real
geographic/IP variance in the live answer, not the same cached egress three times.

```python
def _proxy_for(run_index: int) -> dict:
    return {
        "useApifyProxy": True,
        "apifyProxyGroups": [CFG.get("llm_proxy_group", "DATACENTER")],  # RESIDENTIAL fallback if blocked
        "apifyProxyCountry": "US",                                       # hard-pinned US
        # A *fixed* session id pins one IP for this run; a *different* session per run_index
        # forces a different egress IP across the 3 samples of the same (prompt, surface).
        "apifyProxySession": f"inito_{surface_key}_{run_index}_{RUN_TS}",
    }
```

- Each `run_index` (1,2,3) → unique `apifyProxySession` → distinct sticky IP.
- `RUN_TS` in the session string prevents reusing yesterday's pinned IP.
- Country is enforced on **every** Track B actor call. If a surface bot-blocks datacenter ranges,
  flip `llm_proxy_group` to `RESIDENTIAL` (still US) in config — no code change.
- **Caveat (design note):** Apify guarantees the *session→IP* mapping, not that three sessions land on
  three *unique* IPs from a small pool. With the US datacenter pool this is effectively always distinct;
  if a surface needs residential and the pool is thin, distinctness is best-effort. We log the resolved
  session per row so non-distinct IPs are auditable.

## 6. Classification Subsystem (shared core)

### 6.1 Two-stage classification

1. **`detect_claims(text)`** — case-insensitive regex from `config.claim_patterns` for the §4 stale
   signals + `current_signal_patterns` + `price_pattern`. Cheap, high-recall pre-filter.
2. **`judge(url, text, flags)`** — Claude **Sonnet** with a **forced tool call** (`classify_page`) so output
   always matches schema. The system prompt encodes the full §4 taxonomy — including the **not-stale common
   attributes** (4 hormones, companion app, dip-strip workflow, accuracy) and the **mixed/refutation** edge
   case — to suppress false positives. The judge's `status` is authoritative.

### 6.2 Fallback contract

On LLM call/parse failure, `judge()` returns the same shape from regex flags
(`any stale + current_signal → mixed`; `any stale → stale`; else `current`), `confidence=0.5`,
`_fallback=True`. Guarantees downstream never `KeyError`s; exactly what the offline tests exercise.

### 6.3 LLM-response judge

`judge_llm_response()` mirrors the pattern with the `analyze_llm_response` tool; fallback uses substring
`"inito"` + regex URL extraction for `sources_cited`.

### 6.4 Ownership routing (`ownership(url)`)

```
domain ∈ competitor_domains                          → competitor
domain ∈ owned_domains:
    app store/play & matches owned_app_id            → owned
    app store/play & NOT owned app id                → third_party
    otherwise (inito.com & subdomains)               → owned
amazon.com & path contains /dp/                      → owned_marketplace
everything else                                      → third_party
```
Applied to ads via the **landing-page** domain.

## 7. Persistence Design — **CSV only**, per-run folders

### 7.1 Run identity & folder layout (NFR11)

- A run stamps a single **`RUN_TS`** at startup (UTC ISO8601, filesystem-safe, e.g. `2026-06-20T14-30-05Z`)
  and creates **`RUN_DIR = data/run_<RUN_TS>/`**. `RUN_DATE` (date only) remains the time-series key.
- **All of a run's CSVs are written into `RUN_DIR`.** Cumulative cross-run files live at the **`data/` root**:

```
data/
├─ observations_history.csv     llm_visibility_history.csv     (cumulative — diff/resume)
├─ metrics.csv                  llm_metrics.csv                (cumulative time series)
├─ fetch_cache.csv                                            (cumulative TTL cache)
└─ run_<RUN_TS>/  observations_<date>.csv, latest_snapshot.csv, serp_latest.csv,
                  review_queue.csv, llm_visibility_<date>.csv, llm_visibility_latest.csv,
                  llm_visibility_stats.csv, metrics.csv (this run), llm_metrics.csv (this run)
```

- **Why split, not folder-only:** resume (FR-B3) and diff (FR-A14) need history that outlives any one
  run. Keeping the rolling history + series at root preserves that; copying the run's metric rows into
  `RUN_DIR` keeps each folder self-describing. A single helper resolves paths:
  `out(name) -> RUN_DIR / name` for per-run files, `root(name) -> DATA / name` for cumulative files.
  > **Decision to confirm:** if you'd rather each run folder be *fully* self-contained (history copied in
  > too, root holding only the newest pointer), say so — it's a one-line change to the path helper.

### 7.2 Write patterns

- **No parquet.** Snapshots, rolling history, the fetch cache, metrics, and all exports are CSV.
- **Snapshot + history pattern:** each run writes an immutable `RUN_DIR/*_<date>.csv`, then upserts into
  root `*_history.csv` by **dropping today's rows and re-appending** (idempotent re-runs per date).
- **Fetch cache:** `fetch_cache.csv` with `url, text, fetch_date`; TTL filtering on read; eviction on write.
  - ⚠️ **CSV trade-off (design note):** page text contains newlines/commas/quotes — the cache and snapshot
    writers must rely on proper CSV quoting (pandas default `QUOTE_MINIMAL`) and tolerate large cells.
    No typed schema (everything is strings on reload) — readers must coerce booleans/numbers as needed.
- **Time series upsert:** `metrics.csv` / `llm_metrics.csv` follow the same "drop today, append today" rule.
- **Why CSV:** direct Sheets import, human-diffable, zero extra tooling — at the cost of size and typing,
  which is acceptable at this scale.

## 8. Metrics & Statistics Design

### Track A
- Core counts: `stale_or_mixed`, `owned_stale` (headline), `competitor_negative`, per-claim counts, mean sentiment.
- **Share of voice** (`_sov`): fraction of `category`-intent queries where an owned domain ranks 1–10.
- **Quality:** regex↔judge Cohen's kappa, mean judge confidence, % low-confidence.
- **Run quality score (0–100)** = coverage + confidence + kappa + stale-progress, NaN-guarded.

### Track B
- **Binary rates** (mention, recommend, stale) with **Wilson 95% CI** (small n, 0/1 proportions).
- **Sentiment** as **mean ± 1.96·SE**.
- Granularities: per-prompt×surface (`llm_visibility_stats.csv`), per-surface, overall pooled (`llm_metrics.csv`).
- **Fix-target count** (new): distinct cited source URLs flagged stale, after cross-track linkage.

## 9. Recommended-Action Engine (reworked — REQUIREMENTS § 5.4)

`derive_action(row)` produces one prioritized, source-targeted action per Track B row:

| Condition | Action | Priority (high→low) |
|---|---|---|
| `error_note` set | Surface the error (fail-fast; no retry loop) | (shown, sorts low) |
| stale claim cited, source `owned`/`owned_marketplace` | **Fix our own page now** (name the URL) | 1 (highest) |
| stale claim cited, source `third_party` | Outreach to publisher to correct; else outrank with corrected content | 2 |
| stale claim cited, source `competitor` | Publish authoritative counter-content / request marketplace correction | 2 |
| `competitor_preferred` and not recommended | Build comparison content targeting this prompt; aim to outrank the cited source | 3 |
| `inito_mentioned == false` (high-intent prompt) | Search-visibility gap — create/optimize content ranking for the prompt's terms | 3 |
| `inito_mentioned == false` (other) | Visibility gap — lower-priority content opportunity | 4 |
| neutral mention | Strengthen positioning content | 4 |
| recommended / positive | Monitor only | 5 |

Source targeting comes from `sources_cited`; ownership of each cited URL is computed with `ownership()`.
**Cross-track linkage** (`link_stale_sources`) checks whether a cited stale URL already exists in
`observations_history.csv` so the analyst inherits full page-level context for the same fix.

## 10. Error Handling & Resilience (fail-fast)

| Mechanism | Where | Purpose |
|---|---|---|
| `_require_env` hard exit | startup | Fail fast on missing secrets. |
| `_safe_discover` try/except | Track A discovery | One source failing → error note + `[]`, never aborts. |
| `run_actor` fail-fast | all actor calls | Bounded/zero retries, **no sleep**. |
| Judge fallback | `judge`, `judge_llm_response` | Deterministic regex result on LLM failure. |
| Error rows | Track B discovery | Failures surface as visible rows with `error_note`, not silent gaps. |
| Resume | Track B | Re-run only missing (surface, run_index) combos. |
| NaN guards | metrics/diff | Missing columns/values never crash the math. |

## 11. Configuration Design (`config.json`)

Sections: `market`; `queries` (frozen+intent, append-only); `reddit_searches`; `llm_visibility_prompts`
(frozen+intent); `llm_surfaces` (was `llm_models`) + sampling/proxy knobs (**`llm_num_runs = 3`**,
`llm_proxy_group = "DATACENTER"`, `proxy_country = "US"`); `claim_patterns` +
`current_signal_patterns` + `price_pattern`; `owned_domains`/`owned_app_ids`/`competitor_domains`;
`actors` (slugs); `limits` (caps + `judge_model`).

**Confirmed `actors` set (target):**
```
serp     : apify/google-search-scraper
content  : apify/website-content-crawler
ads      : lexis-solutions/google-ads-scraper
reddit   : trudax/reddit-scraper-lite
chatgpt  : tri_angle/gpt-search
perplexity: zhorex/perplexity-ai-scraper
```
**Deleted from config:** `bing`, `instagram`, `twitter`, `youtube`, `tiktok`, `llm_runner`,
`google_ai_mode`, `perplexity_web` (folded into `perplexity`).

**Invariants:** queries/prompts append-only; judge model id only in `limits.judge_model`
(`claude-sonnet-4-6`); add regex to improve recall, never make regex the final arbiter.

## 12. CLI / Orchestration Design

```
python pipeline.py --refresh     # Track A (SERP + News + Ads + Reddit), sources in parallel
python pipeline.py --llm         # Track B (ChatGPT + Perplexity), surfaces in parallel
python pipeline.py --diff-only   # recompute metrics + diff from CSV history, no crawl
```
A combined invocation runs both tracks. **Fix carried from the as-built quirk:** the `--refresh` + LLM
combination must dispatch both tracks (the old `elif` shadowing is removed in the target design).

## 13. Testing Strategy

- **Fully offline** — `tests/conftest.py` stubs `apify_client` + `anthropic` before import so `judge()`
  takes its fallback; no network/keys/tokens.
- `pipe` fixture redirects `DATA` and the fetch-cache path to a tmp dir.
- Coverage to maintain/extend: URL normalization, ownership (incl. app-store/Amazon + **ads landing-page**),
  claim detection (incl. the two guarded regressions and **new not-stale false-positive cases** — hormones,
  companion app, dip-strip), SERP + **ads** parsing, CSV fetch cache, review queue, kappa, metrics + two-run
  decay diff, share of voice, Track B discovery/persist/metrics, and the **action engine + cross-track linkage**.
- **Rule:** changing `detect_claims`, `ownership`, discovery parsing, the action engine, or metrics math
  requires extending tests.

## 14. Deployment / Productionization

- **Local:** CLI on demand; CSV outputs → Sheets.
- **Cloud:** add `.actor/actor.json` + `Dockerfile`, `apify push`, schedule a weekly cron. Swap local CSV
  writes for `Actor.push_data()` / KV store if needed; add a Slack webhook in `print_diff` to post the
  weekly delta. On-demand and scheduled share one codebase.

## 15. Known Limitations & Tech Debt (target build watch-list)

1. **Web-interface scraper fragility** is the top risk for Track B — anti-bot/login/rate-limits on
   `tri_angle/gpt-search` and `zhorex/perplexity-ai-scraper`. Mitigated by fail-fast + error notes + two surfaces.
2. **CSV typing** — values reload as strings; readers must coerce. Large page-text cells stress CSV quoting.
3. **Ads ownership** — landing-page domain heuristic; verify in edge cases (redirects, trackers).
4. **Cross-track linkage** depends on URL-normalization parity between the two tracks.
6. **Distinct-IP best-effort** — session→IP is guaranteed by Apify, but 3 sessions landing on 3 *unique*
   IPs depends on pool size (reliable on US datacenter; best-effort on thin residential pools). Resolved
   session is logged per row for audit.
7. **Run-folder vs cumulative split** — pending your confirmation (§7.1); fully-self-contained folders are a one-line change.
5. Single-file module — fine now; a package split (discover/classify/persist/metrics) is the natural next refactor.

## 16. Component Responsibility Summary

| Concern | Owned by |
|---|---|
| Crawling / SERP / ads / Reddit / live-assistant access | Apify actors (via `run_actor`) |
| Final claim classification | Claude Sonnet judge (regex = pre-filter + fallback) |
| Ownership routing | `ownership()` + `config` domain lists |
| State & time series | `data/` **CSV** (snapshot+history pattern) |
| Statistics | `_wilson_ci`, `_mean_ci`, `_kappa_regex_vs_judge`, `_run_quality_score` |
| Recommended actions | `derive_action` + `link_stale_sources` (source-targeted, prioritized) |
| Behavior control | `config.json` |
| Orchestration, parallelism & resilience | `refresh` / `run_llm_visibility` / `_safe_discover` / CLI |
