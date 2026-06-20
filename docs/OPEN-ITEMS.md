# Open Items — Inito GEO Monitor

Tiny, living list. Updated 2026-06-20 (first live run). Keep it short — prune as items close.

## Known limitations (live now)
- **Track B IP control is not enforced.** 3 samples capture model variance, not IP variance (DESIGN §5.4).
- **AI-Overview stale claims aren't counted** in `owned_stale`/`stale_or_mixed` (platform excluded from `_WEB_PLATFORMS`).
- **Stale-claim → source attribution is heuristic** — the LLM's stale text is attributed to its cited sources, which may not be the true origin.
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
2. Turn on Perplexity (key) → rerun Track B for full 3-surface coverage.
3. Add `ads_start_urls` → first ads sweep (catches stale copy in our *own* ads).
4. Then iterate: AI-Overview metric inclusion, parallel judging for cost/speed, scheduling + Slack digest.
