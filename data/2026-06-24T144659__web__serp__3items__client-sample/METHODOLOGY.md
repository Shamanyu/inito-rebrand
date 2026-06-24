# Sample run — methodology & provenance (Web / Google)

**File:** `web_observations.csv` · **Date:** 2026-06-24 · **Surface:** Google Search (organic + AI Overview)

## What this is
A snapshot of what the open web currently says about Inito for three queries — one row per source page
Google surfaced (plus Google's AI Overview where shown). Companion to the ChatGPT sample
(`..._llm__chatgpt__3items__3runs__client-sample/`); same three topics, the web side.

| Query (Google) | |
|---|---|
| Inito fertility monitor | |
| Does Inito work on Android? | |
| Inito vs Mira | |

18 rows = top organic results across the three queries + 2 Google AI Overviews.

## Geography & method
- **Region:** United States — Google SERP is run with `countryCode = US`.
- **One pass per query** (no iterations — unlike the live-assistant track, a SERP is deterministic enough
  that repeat sampling adds little). Each result page is then fetched and read in full.
- Pages that are bot-walled / behind a CAPTCHA (some Amazon, Play Store, PMC) are reported honestly as
  "did not load" rather than guessed at.

## How to read the key columns
- `source` — `web` (organic result) or `ai_overview` (Google's AI summary).
- `ownership` — `owned` (any inito.com page), `owned_marketplace` (Inito's Amazon listings),
  `competitor` (rival-owned domains), `third_party` (everyone else).
- `says_about_inito` — plain-language summary of the page's claims, including whether it describes the
  **current** InSight Wireless Reader or the **old** iPhone-clip / phone-camera product.
- `mentions_competition` / `competitors_named` / `competition_summary` — rival brands and framing.
- `price` — first Inito price found on the page. `links_on_source` — external links the page points to.
- `nonprod_url` — TRUE if the page is a non-production Inito host (none in this web sample).

## Notable in this sample
- **Inito's own Apple App Store listing still describes the OLD product** ("Attach… to your phone",
  iPhone-only) — an owned property to fix.
- A third-party review (naturalwomanhood), a Reddit thread, and the **"Inito vs Mira" Google AI Overview**
  all present the **old** phone-camera product as current.
- Inito's homepage and buy page correctly describe the current InSight Wireless Reader.
