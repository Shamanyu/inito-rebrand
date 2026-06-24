---
name: geo-add-source
description: Use when adding a new Track A web/SERP discovery source to the Inito GEO monitor (a new Apify actor that emits URLs/pages to classify — e.g. a forum, marketplace, or another search surface). Covers the actor wiring, record schema, dedupe/ownership, proxy, registration, and tests. Read geo-safe-change first.
---

# GEO — add a Track A discovery source

A Track A source is an Apify actor that yields candidate pages, normalized into the common
discovery-record schema, then crawled + classified into `web_observations.csv` like every other source.

## Steps

1. **Confirm the actor on Apify** — exact slug + input schema (they version). Note its output field
   names (URL, title, body/snippet).
2. **Add the slug to `config.json` `actors`** with a `_comment_<key>` documenting the input shape.
   Add any limit to `limits` (e.g. `<source>_max_items`). Never hardcode the slug in `pipeline.py`.
3. **Write `discover_<source>() -> List[dict]`** next to the other `discover_*` functions. Build
   `run_input` from config; call `run_actor(CFG["actors"]["<source>"], run_input, "<source>")`
   (fail-fast — no retries/sleeps). Emit the **exact discovery-record schema**:

   ```python
   {"url": url, "platform": "<source>", "query": q_or_label, "intent": intent,
    "topic_id": tid_or_"", "rank": rank_or_0, "title": title, "snippet": body[:4000]}
   ```
   - `platform` becomes the `source` column in the sheet. `rank` is the SERP position (1-based) or `0`
     for non-ranked sources; dedupe keeps the lowest non-zero rank.
   - If ownership is known at discovery time (e.g. ads-by-advertiser), set `"ownership"` on the record —
     `classify_web_record` prefers it over `ownership(url)`.
   - Use pseudo-URLs (`<thing>::<query>`) for answers with no real page — stored but skipped by
     enrichment (`enrich_content` only crawls `http(s)` URLs); judged on their snippet text.
4. **Proxy:** if needed, build `{"useApifyProxy": True, "apifyProxyCountry": CFG.get("proxy_country","US")}`.
   Do **not** name `apifyProxyGroups` unless the account has that group (naming `DATACENTER` without it
   hard-fails the actor). Reddit gets intermittent 429s regardless.
5. **Register it:** add `"<source>": discover_<source>` to `WEB_DISCOVERERS` **and** add the key to
   `WEB_SOURCES` so it's CLI-selectable. (There is no `_WEB_PLATFORMS` / metrics gate anymore — every
   discovered source just becomes a row in the snapshot.)
6. **Ownership:** if the source needs special routing (not by URL domain), add a helper like
   `ownership_for_ad` and set `"ownership"` on the record; otherwise the default `ownership(url)` applies.
7. **Tests** (`tests/test_pipeline.py`): monkeypatch `pipe.run_actor` to return a canned dataset and
   assert your parser yields correct records (platform, ownership, snippet, topic_id). Mirror
   `test_discover_ads_parses_and_tags_ownership` and the empty-config skip case. If the source can feed a
   pseudo-URL or special ownership, add a `classify_web_record` assertion too.
8. **Verify:** `pytest -q`, then a **`geo-dry-run`** with this source in `--sources` to confirm it flows
   through dedupe → enrich → `classify_web_record` → `write_web_sheet` with the full `WEB_COLUMNS` and no
   KeyErrors.

## Gotchas
- `refresh` passes the selected query subset only to `serp`/`news` (they take `queries`); other sources
  take no query arg — match that pattern when registering.
- Discovery runs in parallel under `_safe_discover`; a throw becomes an empty list + error log, never a
  crash. Keep your function side-effect-free on failure.
- Don't reintroduce a removed social/Bing source (see `geo-safe-change` invariant 9).
