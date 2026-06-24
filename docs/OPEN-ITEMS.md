# Open Items — Inito GEO Monitor

Tiny, living list. Updated 2026-06-24 (lean snapshot rewrite). Keep it short — prune as items close.

## Known limitations (live now)
- **Track B IP control is not enforced.** Only ChatGPT pins US (actor `country`); Perplexity (sonar) has
  no IP control — its samples capture model variance only.
- **Snapshot only — no trend.** By design there is no time series; comparing runs over time is now an
  out-of-pipeline job (diff two run folders' sheets yourself if needed).
- **Narrative quality depends on the judge.** Offline (no key) the judge falls back to a coarse heuristic
  (`says_about_inito` ≈ "mentions Inito" / "describes the OLD product"); real runs use Claude.
- **Reddit** returns results through intermittent `429` rate-limits (partial coverage).
- **No accuracy measurement yet** — no labeled gold set scoring the judge's narrative/competition calls.

## Deferred (decided, not built)
| Item | Note |
|---|---|
| Generic (non-web / training-data) LLM runs | Explicitly deferred. Today both surfaces are live-web-grounded. Extension point: add a non-grounded runner in `SURFACE_RUNNERS` + a `mode` column. |
| Trend / week-over-week view | Dropped with the time-series layer. Reintroduce only on a new requirement. |

## Blocked on input
| Want | Blocked by |
|---|---|
| Perplexity surface live | **Needs `PERPLEXITY_API_KEY`** in `.env` (sonar API; code done + tested). |
| Google Ads track | **Needs `config.ads_start_urls`** = Transparency Center advertiser URLs (US). |

## Manual checks you can help with
1. **Add `PERPLEXITY_API_KEY`** to `.env` (perplexity.ai/settings/api) to turn on Perplexity.
2. **Populate `ads_start_urls`** from https://adstransparency.google.com (Inito + Mira/Proov, `region=US`).
3. **Investigate why a `preprod.inito.com` URL is publicly reachable / cited by ChatGPT** — the pipeline
   now flags it (`nonprod_url=True`), but the staging page being indexable at all is an infra/SEO fix.

## Next steps (high level)
1. First full live run of both tracks; review the `web_observations.csv` / `llm_observations.csv` by eye.
2. Build a small gold set (~40 pages + ~40 answers) to score the judge's narrative/competition accuracy.
3. Turn on Perplexity (key) and add `ads_start_urls`.
4. (If wanted later) parallel judging, scheduling + a Slack digest of each snapshot.
