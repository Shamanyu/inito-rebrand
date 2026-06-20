# Open Items — Inito GEO Monitor

Tiny, living list. Updated 2026-06-20 (first live run). Keep it short — prune as items close.

## Known limitations (live now)
- **Track B IP control is not enforced.** 3 samples capture model variance, not IP variance (DESIGN §5.4).
- **AI-Overview stale claims aren't counted** in `owned_stale`/`stale_or_mixed` (platform excluded from `_WEB_PLATFORMS`).
- **Source attribution is now quote-grounded** (`verify_stale_attribution`): a stale claim is blamed on a
  source only if that source is judged stale in Track A history OR fetching it + the claim regex confirms
  it — so we no longer say "fix our own page" for a clean page. Residual: newly-fetched sources are
  verified by **regex**, not the full Sonnet judge (cheaper, slightly coarser); see next steps.
- **No accuracy measurement yet** — only regex↔judge kappa, not precision/recall vs a labeled gold set.
- **Reddit** returns results through intermittent `429` rate-limits (partial coverage).
- **CSV typing** — values reload as strings; only coerced columns are safe for math.

## Uncovered requirements → blocking bottleneck
| Want | Blocked by |
|---|---|
| Perplexity surface live | **Needs `PERPLEXITY_API_KEY`** in `.env` (sonar API; code is done + tested). |
| Google Ads track | **Needs `config.ads_start_urls`** = Transparency Center advertiser URLs (US). |
| Count AI-Overview staleness | Small code change (add `ai_overview` handling to metrics) — pending decision. |
| Cheaper/faster full Track A | Content crawler is the bottleneck (~minutes for ~200 pages) + sequential Sonnet judging. |

## Manual checks you can help with
1. **Add `PERPLEXITY_API_KEY`** to `.env` (perplexity.ai/settings/api) to turn on Perplexity.
2. **Populate `ads_start_urls`** from https://adstransparency.google.com (Inito + Mira/Proov, `region=US`); say if it required login.
3. **Rotate the keys** pasted in chat earlier (Apify + Anthropic) — standard hygiene.
4. **Decide:** should AI-Overview stale claims count toward the headline metrics? (recommend yes)

## Next steps (high level)
1. Finish the first full Track A run; review the real `owned_stale` fix-target list.
2. **Re-run Track B with the quote-grounded attribution fix** (the prior run's action column was misattributed).
3. **Accuracy: build a small gold set** (~40 pages + ~40 answers, hand-labeled) and score precision/recall on every change.
4. Upgrade source verification from regex → Sonnet judge (higher accuracy) once the gold set exists to measure it.
5. Turn on Perplexity (key) and add `ads_start_urls`; then AI-Overview metric inclusion, parallel judging, scheduling + Slack digest.
