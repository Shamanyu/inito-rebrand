# Requirements Document — Inito GEO Monitor

**Status:** Revised per stakeholder feedback (2026-06-20). Reverse-engineered from the MVP codebase, then re-scoped.
**Owner:** Inito growth / brand team.
**Last updated:** 2026-06-20.

> **Revision note (this version):** Removed Bing + all social actors (IG/X/YouTube/TikTok); upgraded the
> judge model above Haiku; replaced the API-based bulk LLM runner with **web-interface LLM scrapers that
> behave like a real user with live web search**; deepened the stale-vs-current product taxonomy;
> reworked the recommended-actions model; mandated **CSV-only** outputs and **parallel, fail-fast**
> execution with error notes written to the sheet.
>
> **Surface decisions (confirmed, post-first-run):** Track B = **ChatGPT** (`tri_angle/gpt-search` Apify
> actor) + **Perplexity** (**sonar API, direct** — web scrapers are anti-bot-walled, so we call the API
> the product runs on). **Gemini dropped for now.** **Google Ads** added to Track A
> (`lexis-solutions/google-ads-scraper`). Google AI Overview is a free passive capture in the SERP scrape.
>
> **Sampling & output (confirmed):** every (prompt × surface) is sampled **3×** (`num_runs = 3`), pooled
> for Wilson CIs. The samples capture model variance; only ChatGPT pins US (actor `country`), Perplexity
> (API) has no IP control — the original "3 distinct US IPs" goal is aspirational, not enforced. Every run
> writes all its CSVs into a **timestamped output folder**. See FR-B2/FR-B2a and NFR11.

---

## 1. Background & Problem Statement

Inito is an at-home fertility monitor brand that **redesigned its hardware**. The product line moved from
a phone-dependent device to a standalone wireless reader:

| Attribute | OLD product (now discontinued) | CURRENT product — **InSight Wireless Reader** |
|---|---|---|
| Form factor | Clip/attachment that aligns the device to the phone | Standalone wireless reader |
| Sensor | The **phone's camera** reads the strip | Built-in optical sensor (Spectral Mapping) |
| Connectivity | Lightning / charging port + camera | **Wi-Fi** (results sent to the app) |
| Platform | **iPhone-only** (iPhone 7+, specific models) | **iOS *and* Android** (Android is new) |
| Hormones measured | Estrogen (E3G), LH, PdG, FSH — *same* | Estrogen (E3G), LH, PdG, FSH — *same* |
| Companion app | Yes (Inito app) — *same concept* | Yes (Inito app, iOS + Android) |
| Intro price | (older pricing) | ~$99 with 15 strips; ~$49/cycle thereafter |

The rebrand created a **stale-information footprint**: reviews, blogs, Reddit threads, marketplace
listings, and — critically — **AI assistants** (ChatGPT, Gemini, Perplexity, Google AI answers) still
describe the *old* phone-dependent product as current. This:

1. Misleads buyers (e.g. "Inito is iPhone-only" turns away Android users — a segment now explicitly served).
2. Hands competitors (Mira, Proov, etc.) an unearned edge when sources frame them as better.
3. Erodes trust when generative engines repeat outdated specs **and cite the web pages they came from**.

Because modern AI assistants **search the live web and cite sources**, stale AI answers are now
**traceable to specific web pages** — which makes the problem fixable at the source. This system
quantifies the footprint, points to the exact pages to fix, and proves the count is shrinking over time.

## 2. Goals

| # | Goal |
|---|---|
| G1 | **Measure** stale and competitively-negative claims about Inito across the web and live-search AI assistants. |
| G2 | **Trace & prioritize** — for every stale/negative AI answer, identify the **cited source page(s)** responsible and the single best corrective action. |
| G3 | **Track decay over time** — a time series proving the stale-source count trends to zero as fixes land. |
| G4 | **Be fast, cheap, repeatable** — runs on demand or on a schedule, **fully parallel, fail-fast**, no standing service. |
| G5 | **Be analyst-friendly** — **all outputs are CSV** that import directly into Google Sheets. |

### Non-Goals (MVP)

- Not real-time — batch (on-demand or scheduled cron).
- Does not *auto-submit* corrections to publishers/providers; it recommends and targets them.
- Not a general brand monitor — every rule is specific to Inito's old-vs-current product.
- No bespoke web UI — Google Sheets is the presentation layer.
- Does not store PII — works on public pages and public AI answers only.

## 3. Stakeholders

| Stakeholder | Interest |
|---|---|
| Brand / Growth team | Owns the metric; drives source corrections; reports decay to leadership. |
| Content / SEO team | Acts on per-source actions (fix/update/outreach, comparison content, ranking gaps). |
| Leadership | Wants the headline number (`owned_stale`) trending to zero. |
| Engineer / maintainer | Runs the pipeline, manages actor slugs, controls cost. |

## 4. Product Knowledge — Stale vs. Current Taxonomy *(authoritative for judging)*

This section is the ground truth the classifier (regex + LLM judge) must encode. Getting old-vs-current
right — and **avoiding false positives** on attributes common to both products — is the core of the system.

### 4.1 STALE signals — content describing the OLD product as current

A source/answer is **stale** when it presents any of these as the current state of the product:

| Stale signal | Example phrasing |
|---|---|
| iPhone-only / iOS-only | "Inito only works with iPhone", "compatible with iPhone models only" |
| No Android support | "not available on Android", "Android isn't supported" |
| Attaches/clips to the phone | "clip it onto your phone", "attachment that aligns to your camera" |
| Camera-as-sensor | "uses your iPhone's camera to read the strip", "camera and lighting" |
| Lightning / charging-port dependence | "plug into the Lightning port", "connect via the charging port" |
| Specific old iPhone model requirements | "requires iPhone 7 or newer", "iPhone 13 Pro Max compatible" |

### 4.2 CURRENT signals — content correctly describing the InSight Wireless Reader

| Current signal | Example phrasing |
|---|---|
| Named current product | "InSight Wireless Reader" |
| Wi-Fi / wireless | "Wi-Fi enabled", "wirelessly sends results" |
| Cross-platform | "works on both iOS and Android", "now on Android" |
| Standalone sensor | "built-in optical sensor", "Spectral Mapping", "no phone camera needed" |

### 4.3 NOT-STALE — attributes common to BOTH products (must NOT be flagged)

These are true of the old *and* current product. Flagging them is a **false positive** and must be avoided:

- Measures **four hormones** (Estrogen/E3G, LH, PdG, FSH) on a single strip.
- Has a **companion app** on the phone (the app exists for both; the *reader* is what changed).
- **Dip-the-strip** workflow; results in ~10 minutes in the app.
- Accuracy / clinical-validation / "tracks the full fertile window" claims.
- Connecting to **a phone app** in general (≠ clipping to the phone or using its camera).
  - ⚠️ Distinguish "syncs results to your phone app" (**not stale**) from "attaches to your phone / uses the phone camera" (**stale**).

### 4.4 MIXED — both old and new, or old-quoted-to-correct

- A page with both old and current descriptions → **mixed**.
- A page that quotes old specs **specifically to refute/correct them** ("the old Inito clipped onto your
  iPhone, but the new InSight Reader is wireless and works on Android") → **mixed**, **not stale**.

### 4.5 Other classification dimensions

- **Status** — `stale` | `mixed` | `current` | `unknown`.
- **Ownership** — `owned` | `owned_marketplace` | `competitor` | `third_party`.
- **Intent** — `brand_entity`, `attribute_probe`, `category`, `comparison`, `adversarial`, `community`, `use_case`, `purchase`.
- **Sentiment** toward Inito (−1..+1) and **competitor framing** (a rival framed as better).

## 5. Functional Requirements

Both tracks follow: **discover → classify (regex + LLM judge) → persist → compute metrics + diff.**

### 5.1 Track A — Web / SERP stale-claim detection

**In scope:** Google organic SERP, Google AI Overview (passive capture), ChatGPT/Perplexity SERP panels
(as surfaced by the SERP actor), Google News, **Google Ads**, Reddit. **Removed entirely:** Bing,
Instagram, X/Twitter, YouTube, TikTok.

| ID | Requirement |
|---|---|
| FR-A1 | Discover candidate URLs from: Google organic SERP, Google AI Overview, ChatGPT/Perplexity SERP panels, Google News, Google Ads, Reddit. |
| FR-A2 | Each record carries: `url`, `platform`, `query`, `intent`, SERP `rank`, `title`, `snippet`. |
| FR-A3 | Deduplicate by normalized URL, keeping the best (lowest non-zero) SERP rank. |
| FR-A4 | Normalize URLs: lowercase host, strip `www.`, drop tracking params + fragments, strip trailing slash; preserve meaningful path (e.g. Amazon `/dp/` ASINs). |
| FR-A5 | Enrich real http(s) URLs with full page text via the content crawler; pseudo-URLs (AI Overview etc.) are excluded from enrichment. |
| FR-A6 | Cache fetched page text for a TTL (default 7 days) so unchanged URLs aren't re-crawled. |
| FR-A7 | Detect claims with case-insensitive regex per the §4 taxonomy (stale signals, current signals, price). |
| FR-A8 | Judge each source with the LLM judge (see FR-J1) returning final `status`, the confirmed stale claims, `current_product_named`, `price_mentioned`, `sentiment_inito`, `competitor_framing`, `confidence`. The judge — not regex — is authoritative. |
| FR-A9 | Fall back deterministically to regex if the judge call/parse fails, returning the full contract so downstream never errors. |
| FR-A10 | Tag ownership by domain / app-id / ASIN rules (§ Ownership). |
| FR-A11 | Persist a dated snapshot CSV + a rolling history CSV + `latest_snapshot.csv` for Sheets. |
| FR-A12 | Queue low-confidence rows (`confidence < 0.6`) into `review_queue.csv`. |
| FR-A13 | Compute run metrics: total URLs, `stale_or_mixed`, `owned_stale`, `competitor_negative`, per-claim counts, mean sentiment, category share-of-voice, plus quality metrics (regex↔judge kappa, mean confidence, % low-confidence, composite run-quality 0–100). |
| FR-A14 | Diff the latest run vs previous and print per-metric deltas with direction arrows. |
| FR-A15 | Export AI Overviews + top-5 organic to `serp_latest.csv`. |
| FR-A16 | Run independent discovery sources **in parallel**; one source failing logs an error note and skips — it never aborts the run. |
| FR-A17 | Discover **Google Ads** (`lexis-solutions/google-ads-scraper`) for the brand/category/comparison queries: capture advertiser, ad copy/headlines, and landing-page URL. Ads are classified for stale claims and competitor framing like any other source, with ownership applied to the landing-page domain (`owned` = Inito ads with stale copy → high priority; `competitor` = rival ads framing against Inito). |

### 5.2 Track B — LLM brand visibility *(live web-interface assistants only)*

**Principle (per feedback #3 & #8):** Track B must measure **what a real user sees** when they ask an AI
assistant on the web. Therefore:

- **Removed:** the API-based bulk LLM runner (`fayoussef/bulk-llm-runner`) and **any source that answers
  from training data only with no live web search**.
- **Required:** each LLM surface is queried through a **web-interface scraper actor** that drives the live
  product UI (or its live-search API equivalent) so answers reflect **current web content and real citations**.

**Target surfaces (confirmed, post-first-run):**

| Surface | Mechanism | What it represents |
|---|---|---|
| ChatGPT (web search) | `tri_angle/gpt-search` Apify actor | Live ChatGPT search with citations. Needs one-time actor approval. |
| Perplexity | **sonar API, direct** (`perplexity_complete()`) | Perplexity's always-live web answer + citations. Web scrapers (zhorex) are anti-bot-walled; sonar is the reliable equivalent. Needs `PERPLEXITY_API_KEY`. |
| Google AI Overview | *(via Track A SERP scrape)* | Google's generative answer, captured passively (no dedicated actor). |

**Dropped for now:** Google Gemini and the dedicated Google AI Mode actor. A surface is "live web" whether
via an actor or an always-live-search API (sonar) — the rule is no training-data-only calls.

| ID | Requirement |
|---|---|
| FR-B1 | Send every prompt in `llm_visibility_prompts` to every configured surface (`config.llm_surfaces`). |
| FR-B2 | For **every (prompt × surface), issue 3 samples** (`num_runs = 3`), run in parallel; pool for Wilson/mean CIs (FR-B9). |
| FR-B2a | **US pinning / IP variance:** ChatGPT pins US via the actor's `country` field; Perplexity (sonar API) has no IP control. The original "3 distinct US IPs" goal is **aspirational, not enforced** (an actor's `proxyConfiguration` object can't set a per-run session, and the account had no DATACENTER proxy). The 3 samples primarily capture **model variance**. Revisit if a per-surface IP guarantee becomes a hard requirement. |
| FR-B3 | Resume: skip **(surface, run_index, prompt)** combos already completed today (per-prompt, so a partial run doesn't block the rest). |
| FR-B4 | On failure, **fail fast** (no slow retries) and emit one **error row per affected prompt** with a note. Empty responses are flagged `status=empty` and never judged. |
| FR-B5 | Each answer must capture the verbatim response text **and its cited sources** (the actor's extracted citations + inline URLs). Live citations are mandatory — they are how we trace a stale claim to a fixable page. |
| FR-B6 | Judge each response (LLM judge): `inito_mentioned`, `inito_rank`, `inito_recommended`, `stale_product_described`, `stale_excerpt` (verbatim), `sources_cited`, `sentiment_inito`, `competitors_named`, `competitor_preferred`, `confidence`. |
| FR-B7 | Derive a prioritized **action** per row (see § 5.4). |
| FR-B8 | Persist a dated CSV + rolling history CSV + `llm_visibility_latest.csv` (renamed, ordered, Sheets-friendly columns with clickable source URLs + action + any error note). |
| FR-B9 | Compute metrics with CIs: Wilson 95% CI for binary rates (mention/recommend/stale), mean ± SE for sentiment; per-prompt × surface drill-down + per-surface + overall, all CSV. |

### 5.3 Classification / Judge (shared)

| ID | Requirement |
|---|---|
| FR-J1 | The judge runs on a Claude model **above Haiku** — default **Claude Sonnet (`claude-sonnet-4-6`)**; **Opus (`claude-opus-4-8`)** selectable for maximum accuracy. The model id lives only in `config.limits.judge_model`. |
| FR-J2 | The judge uses a forced structured tool call so output always matches the schema. |
| FR-J3 | The judge system prompt encodes the full §4 taxonomy, including the not-stale common attributes and the mixed/refutation edge case. |

### 5.4 Recommended Actions *(rethought — feedback #5)*

Because every Track B answer now comes with **live citations**, actions must be **source-targeted,
ownership-aware, and prioritized**, not generic. The action engine must:

| ID | Requirement |
|---|---|
| FR-ACT1 | For a **stale claim in an AI answer**, identify which **cited source URL(s)** carry the stale content and target the fix at those pages (not at the model/provider, and never "submit to training data"). |
| FR-ACT2 | Set action **by ownership of the offending source**: `owned`/`owned_marketplace` → *fix our own page now* (highest priority); `competitor` → *publish authoritative counter-content / request marketplace correction*; `third_party` → *outreach to the publisher to update; else outrank with corrected content*. |
| FR-ACT3 | For **Inito not mentioned** on a live-search answer, treat it as a **search-visibility gap** for that query — recommend creating/optimizing content that ranks for the prompt's terms (because live-search assistants pull from top web results), not "enter training data." |
| FR-ACT4 | For **competitor preferred**, name the competitor and the source that favored them, and recommend targeted comparison content aimed at outranking that source. |
| FR-ACT5 | For **positive/recommended**, recommend monitoring only. For **neutral mention**, recommend positioning reinforcement. For **error rows**, the action is the error note (fail-fast; surface, don't retry-loop). |
| FR-ACT6 | Assign each action a **priority** so the sheet sorts by impact, roughly: owned-stale > stale-claim-cited-on-high-intent prompt > competitor-preferred-with-stale-source > not-mentioned-on-high-intent > neutral > positive. |
| FR-ACT7 | **Cross-track linkage (should-have):** when a stale source URL cited in Track B also appears in Track A's web observations, join them so the analyst gets full page context for the same fix target. |

### 5.5 Orchestration / CLI

| ID | Requirement |
|---|---|
| FR-O1 | `--refresh` runs Track A (SERP + News + Ads + Reddit). |
| FR-O2 | `--llm` runs Track B (live-web assistants) only. |
| FR-O3 | `--diff-only` recomputes metrics + diff from CSV history, no crawling. |
| FR-O4 | A combined run option executes both tracks; **both tracks and their internal sources run as parallel as possible** (NFR2). |
| FR-O5 | Validate required env vars at startup; exit with a clear message if missing. |
| FR-O6 | `--reeval` re-scores today's already-captured Track B responses (attribution + action + metrics) with no re-query and no crawl. |
| FR-O7 | Run scoping: `--surfaces` / `--prompts` (indices or name substrings, or `all`), `-y` non-interactive, `--num-runs` samples-per-(prompt×surface). `--extra-prompts` injects ad-hoc **one-off** prompts not in config (`;`-sep, optional `text::intent`, default `adhoc`), never persisted (keeps `llm_visibility_prompts` append-only). `--force` ignores today's resume state. |

### 5.6 Configuration

| ID | Requirement |
|---|---|
| FR-C1 | All queries, prompts, claim regexes, domain lists, actor slugs, model ids, and limits live in `config.json`. |
| FR-C2 | The judge model id lives only in `config.limits.judge_model`. |
| FR-C3 | `queries` and `llm_visibility_prompts` are **append-only** (frozen + intent) to preserve the time series. |
| FR-C4 | Removed actors (Bing, IG, X, YouTube, TikTok, bulk-llm-runner, the Gemini/Google-AI-Mode actors) must be deleted from config, not left dormant. |

## 6. Non-Functional Requirements

| ID | Category | Requirement |
|---|---|---|
| NFR1 | **Cost** | Cache page fetches; per-source `limits.*` caps; weekly cadence default. Judge upgraded to Sonnet (accuracy over raw cost — accepted trade-off). |
| NFR2 | **Parallelism & speed** *(feedback #6)* | Run everything that can run concurrently in parallel — discovery sources, LLM surfaces, and per-prompt jobs. No long sleeps. |
| NFR3 | **Fail fast** *(feedback #6)* | No slow/backoff retry loops. A failed actor/source/judge fails immediately, logs, and writes an **error note to the sheet**; the run continues. |
| NFR4 | **CSV-only persistence** *(feedback #7)* | **No parquet.** All snapshots, rolling history, caches, metrics, and exports are CSV. Resume/diff logic reads CSV. |
| NFR5 | **Live-web fidelity** *(feedback #8)* | Track B sources must reflect live web search as a user experiences it; no training-data-only sources. |
| NFR6 | **Reproducibility** | Dated immutable snapshots; append-only series; frozen queries/prompts. |
| NFR7 | **Testability** | Core logic unit-testable fully offline with network deps stubbed. |
| NFR8 | **Statistical validity** | Binary rates with Wilson CIs; sentiment with mean±SE. |
| NFR9 | **Security** | Secrets only in `.env` (gitignored); never in `config.json`; `data/` gitignored. |
| NFR10 | **Usability** | All outputs CSV with clear names, an `action` column, clickable source URLs, and visible error notes. |
| NFR11 | **Per-run output folder** *(new)* | Every run creates a folder named by its **timestamp** (e.g. `data/run_2026-06-20T14-30-05Z/`) and writes **all of that run's CSV files inside it**. The cumulative cross-run files (rolling history + the metric time series + fetch cache) remain at the `data/` root as the source of truth for resume/diff, and a copy of the run's metric rows is also placed in the run folder so each folder is self-describing. See § 7. |

## 7. Data Outputs — **CSV only** (gitignored `data/`)

**Layout (per NFR11):** each run writes its CSVs into a timestamped folder
`data/run_<ISO8601-timestamp>/`. Cumulative files that must survive across runs (rolling history, the
metric time series, the fetch cache) live at the `data/` **root**; the run folder additionally holds a
copy of that run's metric rows so it is self-contained.

```
data/
├─ observations_history.csv        (root, cumulative — powers Track A diff)
├─ llm_visibility_history.csv      (root, cumulative — powers Track B resume + diff)
├─ metrics.csv                     (root, cumulative time series)
├─ llm_metrics.csv                 (root, cumulative time series)
├─ fetch_cache.csv                 (root, cumulative TTL cache)
└─ run_2026-06-20T14-30-05Z/       (one folder per run, named by timestamp)
   ├─ observations_<date>.csv
   ├─ latest_snapshot.csv
   ├─ serp_latest.csv
   ├─ review_queue.csv
   ├─ llm_visibility_<date>.csv
   ├─ llm_visibility_latest.csv
   ├─ llm_visibility_stats.csv
   ├─ metrics.csv                  (this run's row(s), copied in)
   └─ llm_metrics.csv              (this run's row(s), copied in)
```

| File | Track | Purpose |
|---|---|---|
| `observations_<date>.csv` | A | Dated snapshot of all classified web sources. |
| `observations_history.csv` | A | Rolling full history (for diffing). |
| `latest_snapshot.csv` | A | Latest web results → Sheets. |
| `serp_latest.csv` | A | AI Overviews + top-5 organic → Sheets. |
| `review_queue.csv` | A | Low-confidence rows for human review. |
| `fetch_cache.csv` | A | URL→text cache with fetch date. |
| `metrics.csv` | A | Web decay time series. |
| `llm_visibility_<date>.csv` | B | Dated snapshot of LLM observations. |
| `llm_visibility_history.csv` | B | Rolling LLM history (also powers resume). |
| `llm_visibility_latest.csv` | B | Latest LLM results → Sheets (with `action`, sources, error notes). |
| `llm_visibility_stats.csv` | B | Per-prompt × surface CI drill-down. |
| `llm_metrics.csv` | B | LLM visibility time series. |

## 8. Key Metrics

- **`owned_stale` (headline)** — Inito-owned pages with stale claims. **Target: 0 first.**
- `stale_or_mixed` — total stale/mixed web sources; trend down.
- `competitor_negative` — competitor pages framing Inito negatively.
- Per-claim counts (iPhone-only, attach-to-phone, camera-dependent, no-Android) → all to 0.
- LLM `mention_rate` ↑, `stale_rate` ↓, `sentiment` ↑, `recommended` ↑ (each with 95% CI).
- `share_of_voice_category` — fraction of category queries where an owned domain ranks top-10.
- **Fix-target count** — distinct cited source URLs needing correction (new, from FR-ACT1).

## 9. Assumptions & Constraints

- **A1** — Apify actor slugs/input schemas drift. Validated live (2026-06-20): `apify/google-search-scraper` (✓ incl. AI Overviews), `apify/website-content-crawler` (✓), `trudax/reddit-scraper-lite` (✓ w/ intermittent 429s), `tri_angle/gpt-search` (✓ after one-time approval). `lexis-solutions/google-ads-scraper` needs Transparency-Center URLs (untested). Perplexity is the **sonar API**, not an actor.
- **A2** — Track B uses **live-web** assistants; results reflect current web content + citations. (No training-data-only sources remain.)
- **A3** — Web-interface scrapers are slower per query (~15–30s, browser rendering) and answers vary between runs — hence `num_runs` sampling + CIs.
- **A4** — Some web-interface scrapers may rate-limit or get bot-blocked at volume; proxy choice (datacenter vs residential) is per-actor and must be verified.
- **A5** — Ownership for app-store/Amazon is heuristic (Inito app ids + `/dp/` ASINs); edge cases need manual seller verification.
- **A6** — Residential proxies are required to reach Reddit (otherwise 403).
- **A7** — Price alone is not a reliable stale signal (it's ambiguous); price is recorded but not, by itself, a stale trigger.

## 10. Open Risks (post-MVP)

- Web-interface scraper reliability/anti-bot is the biggest dependency risk for Track B (mitigated by fail-fast + error notes + multiple surfaces).
- AI provider/publisher correction workflow remains manual.
- No alerting yet (proposed: Slack webhook on the weekly diff).
- Cross-track source linkage (FR-ACT7) depends on URL-normalization parity between tracks.
