# CLAUDE.md

Guidance for Claude Code (and humans) working in this repo.

## What this is

A GEO (generative engine optimization) monitor for the brand **Inito**. It finds web/social pages
that make **stale** claims about Inito's old product (iPhone-only, clips to phone, camera-based, no
Android) or **competitive** claims (rivals framed as better), scores them, and tracks the counts
over time so you can prove the stale-source footprint is shrinking as fixes land.

The old product clipped onto an iPhone and used the phone camera (iPhone-only). The **current**
product is the **InSight Wireless Reader** — Wi-Fi, works on iOS *and* Android, no camera/clip.
Every classification decision hinges on old-vs-current.

## Architecture

Four stages, `pipeline.py`:

1. **discover** — Apify actors emit URLs: Google SERP (`discover_serp`), Reddit (`discover_reddit`),
   IG/X/YouTube (`discover_social`). Residential proxies are why Reddit (normally 403 to datacenter
   IPs) and social are reachable here.
2. **enrich** — `enrich_content` runs the Website Content Crawler over deduped URLs → full page text.
3. **classify** — `detect_claims` (regex heuristics) then `judge` (Claude Haiku) for final
   status/sentiment/competitor-framing. `ownership` tags each URL by domain.
4. **persist + diff** — `persist` writes a dated snapshot + rolling history; `compute_metrics` +
   `print_diff` produce `metrics.csv` (the time series) and the run-over-run delta.

Apify owns stages 1–2; our code owns 3–4.

## Repo layout

```
config.json     control surface (queries, claim regexes, domain lists, actor slugs, limits)
pipeline.py     the orchestrator (stages above) + CLI
tests/          offline pytest suite (network deps stubbed in conftest.py)
data/           outputs (gitignored): observations_*.parquet, *_history.parquet, metrics.csv, latest_snapshot.csv
README.md       setup + run + cost + scheduling
```

## Commands

```bash
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)        # APIFY_TOKEN, ANTHROPIC_API_KEY
python pipeline.py --refresh               # full sweep
python pipeline.py --refresh --no-social   # SERP + Reddit only (cheap)
python pipeline.py --diff-only             # recompute metrics, no crawling
pytest -q                                  # offline tests
```

## Invariants — do not break these

- **`config.json` queries are append-only.** Editing an existing query string silently breaks the
  time series. Add a new entry and leave the old one.
- **The LLM judge is the arbiter of `status`, not regex.** `claim_patterns` are cheap heuristics
  that feed the judge and serve as the offline fallback. Improve recall by adding patterns; never
  rely on regex alone for the final call.
- **Social discovery must never break the core.** Each social actor is wrapped in try/except so an
  Instagram/X markup change can't kill the SERP+Reddit run. Keep it that way.
- **`owned_stale` is the headline metric.** Inito's own pages (owned / owned_marketplace) carrying
  stale claims are the priority; this number should trend to zero first.
- **Model string lives only in `config.json` (`limits.judge_model`).** Don't hardcode it elsewhere.
  Current IDs: https://docs.claude.com/en/docs/about-claude/models

## Ownership rules (`ownership()`)

- `owned` — inito.com + subdomains; the Inito app on App Store / Google Play (matched by app id).
- `owned_marketplace` — amazon.com `/dp/` ASINs (Inito's own listings; verify seller in edge cases).
- `competitor` — domains in `config.competitor_domains` (miracare, proovtest, ovul, …).
- `third_party` — everything else, incl. app-store pages for non-Inito apps.

## Gotchas

- **Actor slugs/input schemas drift.** Before first run, confirm each `config.actors` entry on its
  Apify Store page and align the `run_input` builders. SERP + Content Crawler (Apify-official) are
  stable; Reddit/IG/X vary most.
- **AI Overviews** are stored as pseudo-URLs `aioverview::<query>` so the verbatim AI answer per
  query is tracked over time; they're excluded from page-fetch enrichment.
- **Cost:** Haiku for the judge, `--no-social` for the weekly core, monthly for social. Add a
  fetch-cache (skip URLs unseen < N days) before scaling cadence.

## Tests

`pytest -q` runs fully offline — `tests/conftest.py` stubs `apify_client` and `anthropic` before
import, so `judge()` exercises its deterministic fallback and no network/keys are needed. When you
change `detect_claims`, `ownership`, `discover_serp` parsing, or the metrics math, add/extend a test.
The suite already guards the two fixed bugs (the `attach_to_phone` gap and the `searchQuery`
dict-or-string shape) — keep those regressions covered.

## Definitely don't

- Commit `.env` or `data/` (both gitignored).
- Put secrets in `config.json`.
- Add a new model/provider call without routing the model id through `config.json`.
