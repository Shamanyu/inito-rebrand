---
name: geo-add-source
description: Use when adding a new Track A web/SERP discovery source to the Inito GEO monitor (a new Apify actor that emits URLs/pages to classify for stale claims — e.g. a forum, marketplace, or another search surface). Covers the actor wiring, record schema, dedupe/ownership, proxy, registration, and tests. Read geo-safe-change first.
---

# GEO — add a Track A discovery source

A Track A source is an Apify actor that yields candidate pages, normalized into the common
discovery-record schema, then crawled + classified like every other source.

## Steps

1. **Confirm the actor on Apify** — exact slug + input schema (they version). Note its output field
   names (URL, title, body/snippet).
2. **Add the slug to `config.json` `actors`** with a `_comment_<key>` documenting the input shape.
   Add any limit to `limits` (e.g. `<source>_max_items`). Never hardcode the slug in `pipeline.py`.
3. **Write `discover_<source>() -> List[dict]`** next to the other `discover_*` functions. Build
   `run_input` from config; call `run_actor(CFG["actors"]["<source>"], run_input, "<source>")`
   (fail-fast — don't add retries/sleeps). Emit the **exact record schema**:

   ```python
   {"url": url, "platform": "<source>", "query": q_or_label, "intent": intent,
    "rank": rank_or_0, "title": title, "snippet": body[:4000]}
   ```
   - `rank` is the SERP position (1-based) or `0` for non-ranked sources; dedupe keeps the lowest
     non-zero rank.
   - If ownership is known at discovery time (e.g. ads-by-advertiser), set `"ownership"` on the
     record and the classify loop will prefer it over `ownership(url)`.
   - Use pseudo-URLs (`<thing>::<query>`) for answers with no real page — they're stored but skipped
     by enrichment (`enrich_content` only crawls `http(s)` URLs).
4. **Proxy:** if the actor needs it, build `{"useApifyProxy": True, "apifyProxyCountry": CFG.get("proxy_country","US")}`.
   Do **not** name `apifyProxyGroups` unless you know the account has that group — naming `DATACENTER` on a
   plan without it hard-fails the actor (a real first-run failure). Reddit gets intermittent 429s regardless.
5. **Register it:** add `"<source>": discover_<source>` to `WEB_DISCOVERERS` **and** add the key to
   `WEB_SOURCES` so it's CLI-selectable. If its rows should count toward stale metrics, add the
   `platform` value to `_WEB_PLATFORMS`.
6. **Ownership:** if the source needs special routing (not by URL domain), add a helper like
   `ownership_for_ad` and call it inside `discover_<source>`; otherwise the default `ownership(url)`
   applies in `refresh`.
7. **Tests** (`tests/test_pipeline.py`): monkeypatch `pipe.run_actor` to return a canned dataset and
   assert your parser yields correct records (platform, ownership, snippet). Mirror
   `test_discover_ads_parses_and_tags_ownership` and the empty-config skip case.
8. **Verify:** `pytest -q`, then a **`geo-dry-run`** with this source in `--sources` to confirm it
   flows through dedupe → enrich → classify → persist → metrics without KeyErrors.

## Gotchas
- `refresh` passes the selected query subset only to `serp`/`news` (they take `queries`); other
  sources take no query arg — match that pattern when registering.
- Discovery runs in parallel under `_safe_discover`; a throw becomes an empty list + error log, never
  a crash. Keep your function side-effect-free on failure.
- Don't reintroduce a removed social/Bing source (see `geo-safe-change` invariant 8).
