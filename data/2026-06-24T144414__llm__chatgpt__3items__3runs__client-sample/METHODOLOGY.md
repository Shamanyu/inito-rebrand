# Sample run — methodology & provenance

**File:** `llm_observations.csv` · **Date:** 2026-06-24 · **Surface:** ChatGPT (live web search)

## What this is
A snapshot of how ChatGPT (with live web browsing) currently answers three questions about Inito, with
**3 independent iterations per question** so you can see how consistent (or not) the answer is.

| Question | Iterations |
|---|---|
| Inito fertility monitor | 3 |
| Does Inito work on Android? | 3 |
| Inito vs Mira | 3 |

= 9 rows total. Each row is one iteration; `run` (1–3) identifies the iteration.

## Geography & sampling
- **Region:** United States. Every query is sent through an Apify proxy pinned to **country = US**, and
  each iteration draws a **fresh US web session**, so the iterations plausibly traverse **different US
  IP addresses**.
- **Why no specific IPs/cities are listed:** the ChatGPT access tool returns only the prompt, the
  answer, and the cited sources — it does **not** expose the per-request egress IP address or city. We
  therefore cannot truthfully attribute a specific IP/location to each row, and we don't guess. The
  honest, verifiable geo statement is: **US-pinned, 3 independent sessions per query.**
- The 3 iterations capture **model + retrieval variance** (ChatGPT can answer differently each time),
  which is the main thing that varies run-to-run.

## How to read the key columns
- `mentioned` / `rank` / `recommended` — is Inito named, where, and is it endorsed.
- `says_about_inito` — plain-language summary of what the answer claims about Inito, including whether it
  describes the **current** InSight Wireless Reader or the **old** iPhone-clip / phone-camera product.
- `mentions_competition` / `competitors_named` / `competition_summary` — rival brands raised and framing.
- `sources_cited` — the live web pages ChatGPT cited (tracking parameters stripped for clarity).
- `nonprod_url` — TRUE when ChatGPT cited a non-production Inito host (e.g. `preprod.inito.com`,
  `staging.inito.com`). These pages should not be publicly reachable or cited — flagged for follow-up.

## Notable in this sample
- **"Inito vs Mira": all 3 iterations describe the OLD phone-camera product** as current.
- **"Inito fertility monitor": 1 of 3 iterations** slips into the old-product description.
- **"Does Inito work on Android?": all 3 correct** (current standalone reader).
- ChatGPT repeatedly cited **preprod/staging Inito URLs** (see `nonprod_url` + `sources_cited`).
