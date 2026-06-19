#!/usr/bin/env python3
"""
Inito GEO monitor — Apify-backed stale-source pipeline.

Stages:  discover (Apify actors) -> enrich (page text) -> classify (regex + Claude judge)
         -> persist (dated snapshot) -> diff (metrics vs previous run)

On-demand:   python pipeline.py --refresh
             python pipeline.py --refresh --no-social      # SERP + Reddit only (cheaper)
             python pipeline.py --diff-only                 # recompute metrics from last snapshot

Env (.env):  APIFY_TOKEN, ANTHROPIC_API_KEY
"""

import argparse, json, os, re, sys, time, datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit, urlunsplit
from pathlib import Path

from apify_client import ApifyClient
from anthropic import Anthropic
import pandas as pd

ROOT = Path(__file__).parent
DATA = ROOT / "data"; DATA.mkdir(exist_ok=True)
CFG = json.loads((ROOT / "config.json").read_text())
RUN_DATE = dt.date.today().isoformat()


# ---------- startup validation ----------
def _require_env(*names):
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        sys.exit(
            f"Missing required environment variables: {', '.join(missing)}\n"
            f"Set them in .env and run:  export $(grep -v '^#' .env | xargs)"
        )

_require_env("APIFY_TOKEN", "ANTHROPIC_API_KEY")

apify = ApifyClient(os.environ["APIFY_TOKEN"])
claude = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


# ---------- helpers ----------
def log(msg): print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)

def normalize_url(u: str) -> str:
    try:
        s = urlsplit(u.strip())
        host = s.netloc.lower().removeprefix("www.")
        # drop tracking params + fragment; keep path + meaningful query (e.g. amazon /dp/)
        return urlunsplit((s.scheme or "https", host, s.path.rstrip("/"), "", ""))
    except Exception:
        return u.strip()

def domain_of(u: str) -> str:
    return urlsplit(u).netloc.lower().removeprefix("www.")

def run_actor(actor_id: str, run_input: dict, label: str, retries: int = 1) -> list:
    """Call an actor, return its dataset items. Fails fast — no sleep between retries."""
    for attempt in range(1, retries + 1):
        try:
            log(f"  actor {actor_id} ({label}) starting…")
            run = apify.actor(actor_id).call(run_input=run_input)
            items = list(apify.dataset(run["defaultDatasetId"]).iterate_items())
            log(f"  actor {actor_id} ({label}) -> {len(items)} items")
            return items
        except Exception as e:
            if attempt == retries:
                raise
            log(f"  actor {actor_id} ({label}) attempt {attempt} failed: {e}. Retrying…")
    return []


# ---------- stage 1: discover ----------
def discover_serp() -> list[dict]:
    """Google SERP via apify/google-search-scraper.
    Captures organic results, AI Overview, ChatGPT Search panel, and Perplexity panel.
    """
    qmap = {c["q"]: c["intent"] for c in CFG["queries"]}
    run_input = {
        "queries": "\n".join(qmap.keys()),
        "resultsPerPage": CFG["limits"]["serp_results_per_query"],
        "maxPagesPerQuery": CFG["limits"]["serp_pages_per_query"],
        "countryCode": CFG["market"]["countryCode"],
        "languageCode": CFG["market"]["languageCode"],
        "mobileResults": False,
    }
    out = []
    for item in run_actor(CFG["actors"]["serp"], run_input, "serp"):
        sq = item.get("searchQuery")
        q = (sq.get("term") if isinstance(sq, dict) else sq) or ""
        intent = qmap.get(q, "unknown")
        for rank, r in enumerate(item.get("organicResults", []), 1):
            url = r.get("url")
            if not url:
                continue
            out.append({"url": url, "platform": "web", "query": q, "intent": intent,
                        "rank": rank, "title": r.get("title", ""), "snippet": r.get("description", "")})
        # Google AI Overview (Gemini-powered)
        ai = item.get("aiOverview") or item.get("aiOverviewText")
        if ai:
            out.append({"url": f"aioverview::{q}", "platform": "ai_overview", "query": q,
                        "intent": intent, "rank": 0, "title": "Google AI Overview",
                        "snippet": ai if isinstance(ai, str) else json.dumps(ai)[:4000]})
        # ChatGPT Search panel (appears in Google SERP on paid plans)
        for cgpt in (item.get("chatGptSearchResults") or []):
            text = cgpt.get("text") or cgpt.get("answer") or ""
            if text:
                out.append({"url": f"chatgptsearch::{q}", "platform": "chatgpt_search", "query": q,
                            "intent": intent, "rank": 0, "title": "ChatGPT Search Panel",
                            "snippet": text[:4000]})
        # Perplexity panel (appears in Google SERP on paid plans)
        for px in (item.get("perplexitySearchResults") or []):
            text = px.get("text") or px.get("answer") or ""
            if text:
                out.append({"url": f"perplexitysearch::{q}", "platform": "perplexity_search", "query": q,
                            "intent": intent, "rank": 0, "title": "Perplexity Search Panel",
                            "snippet": text[:4000]})
    return out

def discover_bing() -> list[dict]:
    """Bing SERP — feeds Copilot, Perplexity, and DuckDuckGo AI answers."""
    qmap = {c["q"]: c["intent"] for c in CFG["queries"]}
    run_input = {
        "queries": "\n".join(qmap.keys()),
        "resultsPerPage": CFG["limits"].get("bing_results_per_query", 10),
        "countryCode": CFG["market"]["countryCode"],
    }
    out = []
    try:
        for item in run_actor(CFG["actors"]["bing"], run_input, "bing"):
            sq = item.get("searchQuery") or item.get("query") or ""
            q = (sq.get("term") if isinstance(sq, dict) else sq) or ""
            intent = qmap.get(q, "unknown")
            for rank, r in enumerate(item.get("organicResults", []) or item.get("results", []), 1):
                url = r.get("url")
                if url:
                    out.append({"url": url, "platform": "bing", "query": q, "intent": intent,
                                "rank": rank, "title": r.get("title", ""),
                                "snippet": r.get("description", "")})
    except Exception as e:
        log(f"  bing skipped: {e}")
    return out

def discover_perplexity_web() -> list[dict]:
    """Perplexity AI via zhorex/perplexity-ai-scraper — headless browser, true web interface.
    Returns one row per query with the full AI answer + cited sources.
    Uses brand_monitor mode so mention/position/competitor signals are pre-extracted by the actor.
    """
    queries = [c["q"] for c in CFG["queries"]]
    qmap = {c["q"]: c["intent"] for c in CFG["queries"]}
    run_input = {
        "queries": queries,
        "mode": "brand_monitor",
        "brandName": "Inito",
        "maxQueries": len(queries),
        "waitTimeout": CFG["limits"].get("perplexity_wait_timeout", 90),
        # No proxyConfiguration — actor uses its own built-in residential proxy pool.
        # Passing Apify datacenter proxies causes Perplexity to block the requests.
    }
    out = []
    try:
        items = run_actor(CFG["actors"]["perplexity_web"], run_input, "perplexity_web")
    except Exception as e:
        log(f"  perplexity_web FAILED: {e}")
        return []
    for it in items:
        q = it.get("query", "")
        answer = it.get("answer", "")
        sources = it.get("sources") or []
        # sources is a list of {position, title, url, domain, snippet}
        source_urls = [s.get("url", "") for s in sources if s.get("url")]
        out.append({
            "url": f"perplexity::{q}",
            "platform": "perplexity_web",
            "query": q,
            "intent": qmap.get(q, "unknown"),
            "rank": 0,
            "title": "Perplexity Answer",
            "snippet": answer[:4000],
            # actor-extracted brand signals (pre-computed, supplement our judge)
            "_perplexity_mentioned": it.get("mentioned"),
            "_perplexity_position": it.get("position"),
            "_perplexity_competitors": it.get("competitorsMentioned", []),
            "_perplexity_sources": json.dumps(source_urls[:15]),
            "_perplexity_related": json.dumps(it.get("relatedQuestions", [])[:5]),
        })
    return out


def discover_google_ai_mode() -> list[dict]:
    """Google AI Mode (udm=50) via scrape.badger/google-ai-mode-scraper.
    This is the full-page Gemini-powered generative answer — stronger signal than AI Overview.
    Input: one query per line. Output: text_blocks + references per query.
    """
    queries = [c["q"] for c in CFG["queries"]]
    qmap = {c["q"]: c["intent"] for c in CFG["queries"]}
    run_input = {
        "queries": "\n".join(queries),
        "gl": CFG["market"]["countryCode"],
        "hl": CFG["market"]["languageCode"],
    }
    out = []
    try:
        items = run_actor(CFG["actors"]["google_ai_mode"], run_input, "google_ai_mode")
    except Exception as e:
        log(f"  google_ai_mode FAILED: {e}")
        return []
    for it in items:
        q = it.get("query", "")
        blocks = it.get("text_blocks") or []
        refs = it.get("references") or []
        # flatten text_blocks into a single answer string
        answer = " ".join(b.get("snippet", "") for b in blocks if b.get("snippet"))
        if not answer:
            continue  # Google didn't serve AI Mode for this query
        source_urls = [r.get("link", "") for r in refs if r.get("link")]
        out.append({
            "url": f"googleaimode::{q}",
            "platform": "google_ai_mode",
            "query": q,
            "intent": qmap.get(q, "unknown"),
            "rank": 0,
            "title": "Google AI Mode (Gemini)",
            "snippet": answer[:4000],
            "_googleai_sources": json.dumps(source_urls[:15]),
        })
    return out


def discover_news() -> list[dict]:
    """Google News via the same SERP actor with tbm=nws — press/syndicated stale content."""
    qmap = {c["q"]: c["intent"] for c in CFG["queries"]}
    run_input = {
        "queries": "\n".join(qmap.keys()),
        "resultsPerPage": CFG["limits"].get("news_max_per_query", 20),
        "maxPagesPerQuery": 1,
        "countryCode": CFG["market"]["countryCode"],
        "languageCode": CFG["market"]["languageCode"],
        "mobileResults": False,
        "tbm": "nws",  # switches google-search-scraper to News tab
    }
    out = []
    try:
        for item in run_actor(CFG["actors"]["serp"], run_input, "news"):
            sq = item.get("searchQuery")
            q = (sq.get("term") if isinstance(sq, dict) else sq) or ""
            intent = qmap.get(q, "unknown")
            for rank, r in enumerate(item.get("organicResults", []), 1):
                url = r.get("url")
                if url:
                    out.append({"url": url, "platform": "news", "query": q, "intent": intent,
                                "rank": rank, "title": r.get("title", ""),
                                "snippet": r.get("description", "")})
    except Exception as e:
        log(f"  news skipped: {e}")
    return out

def discover_reddit() -> list[dict]:
    # NOTE: confirm input keys against your chosen Reddit actor's page. trudax/reddit-scraper-lite
    # accepts `searches` + `maxItems`. The official apify/reddit-scraper uses `searchTerms`/`startUrls`.
    run_input = {"searches": CFG["reddit_searches"], "maxItems": CFG["limits"]["reddit_max_items"],
                 "type": "posts", "sort": "relevance",
                 "proxy": {"useApifyProxy": True, "apifyProxyCountry": "US"}}
    out = []
    for it in run_actor(CFG["actors"]["reddit"], run_input, "reddit"):
        url = it.get("url") or it.get("link")
        if not url:
            continue
        out.append({"url": url, "platform": "reddit", "query": "reddit", "intent": "community",
                    "rank": 0, "title": it.get("title", ""),
                    "snippet": (it.get("body") or it.get("text") or "")[:4000]})
    return out

def discover_social() -> list[dict]:
    out = []
    # Instagram (keyword/hashtag). Confirm input schema for your actor version.
    try:
        ig_in = {"search": CFG["social_keywords"][0], "searchType": "hashtag",
                 "resultsLimit": CFG["limits"]["social_max_items"]}
        for it in run_actor(CFG["actors"]["instagram"], ig_in, "instagram"):
            url = it.get("url")
            if url:
                out.append({"url": url, "platform": "instagram", "query": "ig", "intent": "social",
                            "rank": 0, "title": it.get("ownerUsername", ""),
                            "snippet": (it.get("caption") or "")[:4000]})
    except Exception as e:
        log(f"  instagram skipped: {e}")
    # X / Twitter. Confirm input schema.
    try:
        x_in = {"searchTerms": CFG["social_keywords"], "maxItems": CFG["limits"]["social_max_items"]}
        for it in run_actor(CFG["actors"]["twitter"], x_in, "twitter"):
            url = it.get("url") or it.get("twitterUrl")
            if url:
                out.append({"url": url, "platform": "x", "query": "x", "intent": "social",
                            "rank": 0, "title": it.get("author", {}).get("userName", "") if isinstance(it.get("author"), dict) else "",
                            "snippet": (it.get("text") or "")[:4000]})
    except Exception as e:
        log(f"  twitter skipped: {e}")
    # YouTube
    try:
        yt_in = {"searchQueries": CFG["youtube_searches"], "maxResults": CFG["limits"]["youtube_max_items"]}
        for it in run_actor(CFG["actors"]["youtube"], yt_in, "youtube"):
            url = it.get("url")
            if url:
                out.append({"url": url, "platform": "youtube", "query": "yt", "intent": "social",
                            "rank": 0, "title": it.get("title", ""),
                            "snippet": (it.get("text") or it.get("description") or "")[:4000]})
    except Exception as e:
        log(f"  youtube skipped: {e}")
    # TikTok — fertility content is high-volume and frequently outdated. Confirm input schema.
    try:
        tt_in = {"searchQueries": CFG["social_keywords"],
                 "maxItems": CFG["limits"].get("tiktok_max_items", 50)}
        for it in run_actor(CFG["actors"]["tiktok"], tt_in, "tiktok"):
            url = it.get("webVideoUrl") or it.get("url")
            if url:
                author = it.get("authorMeta", {})
                out.append({"url": url, "platform": "tiktok", "query": "tiktok", "intent": "social",
                            "rank": 0,
                            "title": author.get("name", "") if isinstance(author, dict) else "",
                            "snippet": (it.get("text") or it.get("description") or "")[:4000]})
    except Exception as e:
        log(f"  tiktok skipped: {e}")
    return out


# ---------- stage 2: enrich (full page text for web URLs) ----------
FETCH_CACHE_PATH = DATA / "fetch_cache.parquet"
FETCH_CACHE_TTL_DAYS = 7

def load_fetch_cache() -> dict[str, str]:
    """Returns {normalized_url: text} for URLs fetched within TTL."""
    if not FETCH_CACHE_PATH.exists():
        return {}
    df = pd.read_parquet(FETCH_CACHE_PATH)
    cutoff = (dt.date.today() - dt.timedelta(days=FETCH_CACHE_TTL_DAYS)).isoformat()
    fresh = df[df["fetch_date"] >= cutoff]
    return {row["url"]: row["text"] for _, row in fresh.iterrows()}

def save_fetch_cache(new_entries: dict[str, str]):
    """Merge new {url: text} entries into the cache parquet, evicting stale records."""
    today = dt.date.today().isoformat()
    new_rows = pd.DataFrame([{"url": u, "text": t, "fetch_date": today}
                              for u, t in new_entries.items()])
    if FETCH_CACHE_PATH.exists():
        existing = pd.read_parquet(FETCH_CACHE_PATH)
        existing = existing[~existing["url"].isin(new_entries.keys())]
        combined = pd.concat([existing, new_rows], ignore_index=True)
    else:
        combined = new_rows
    cutoff = (dt.date.today() - dt.timedelta(days=FETCH_CACHE_TTL_DAYS * 2)).isoformat()
    combined = combined[combined["fetch_date"] >= cutoff]
    combined.to_parquet(FETCH_CACHE_PATH, index=False)

def enrich_content(urls: list[str]) -> dict[str, str]:
    """Website Content Crawler -> {url: text}. Only real http(s) pages; skips pseudo-urls.
    Checks fetch cache first — only crawls URLs not seen within TTL days."""
    real = [u for u in urls if u.startswith("http") and not u.startswith("aioverview::")]
    if not real:
        return {}
    cache = load_fetch_cache()
    cached = {u: cache[u] for u in real if u in cache}
    to_fetch = [u for u in real if u not in cache]
    log(f"  fetch cache: {len(cached)} hits, {len(to_fetch)} to crawl")
    if not to_fetch:
        return cached
    run_input = {
        "startUrls": [{"url": u} for u in to_fetch],
        "crawlerType": "playwright:adaptive",
        "maxCrawlDepth": 0, "maxCrawlPages": len(to_fetch),
        "proxyConfiguration": {"useApifyProxy": True},
        "saveMarkdown": True,
    }
    fetched = {}
    for it in run_actor(CFG["actors"]["content"], run_input, "content"):
        u = it.get("url")
        if u:
            fetched[normalize_url(u)] = (it.get("text") or it.get("markdown") or "")[:20000]
    save_fetch_cache(fetched)
    return {**cached, **fetched}


# ---------- stage 3: classify ----------
def detect_claims(text: str) -> dict:
    t = text.lower()
    flags = {}
    for claim, pats in CFG["claim_patterns"].items():
        flags[claim] = any(re.search(p, t) for p in pats)
    flags["current_signal"] = any(re.search(p, t) for p in CFG["current_signal_patterns"])
    m = re.findall(CFG["price_pattern"], text)
    flags["prices_seen"] = sorted(set(m))[:5]
    return flags

JUDGE_SYSTEM = """You classify a web page's claims about the fertility brand Inito.

Context: Inito's CURRENT product is the "InSight Wireless Reader" — Wi-Fi, works on iOS AND Android, no phone-camera/clip needed. The OLD product clipped onto an iPhone and used the phone camera; it was iPhone-only.

A page is STALE if it presents the old product as current (iPhone-only, attaches to phone, camera-based, no Android, old price). MIXED if it has both old and new content. CURRENT if it correctly reflects the wireless reader.

KEY EDGE CASE: If a page quotes old specs in order to refute or correct them ("the old Inito clipped onto your iPhone but the new InSight has changed that"), classify as MIXED, not STALE.

EXAMPLES:
Page: "Inito is only compatible with iPhone. You attach the monitor to your phone and it uses your iPhone's camera."
→ status=stale, iphone_only=true, attach_to_phone=true, camera_dependent=true, confidence=0.95

Page: "The original reader clipped onto your iPhone. The new InSight Wireless Reader is now available on Android too."
→ status=mixed, attach_to_phone=true, current_product_named=true, confidence=0.9

Page: "The InSight Wireless Reader is Wi-Fi enabled and works on both iOS and Android."
→ status=current, current_product_named=true, all claims false, confidence=0.97

Page: "Mira is more accurate and doesn't require clipping anything to your phone like Inito does."
→ status=stale, competitor_framing=true, sentiment_inito=-0.7, attach_to_phone=true, confidence=0.85

Use the classify_page tool to return your verdict."""

JUDGE_TOOL = {
    "name": "classify_page",
    "description": "Classify a web page's claims about the Inito fertility monitor.",
    "input_schema": {
        "type": "object",
        "required": ["status", "current_product_named", "claims_confirmed",
                     "price_mentioned", "sentiment_inito", "competitor_framing", "confidence"],
        "properties": {
            "status": {
                "type": "string",
                "enum": ["stale", "mixed", "current", "unknown"],
                "description": "Overall staleness classification",
            },
            "current_product_named": {
                "type": "boolean",
                "description": "True if the page explicitly names or describes the InSight Wireless Reader",
            },
            "claims_confirmed": {
                "type": "object",
                "required": ["iphone_only", "attach_to_phone", "camera_dependent", "no_android"],
                "properties": {
                    "iphone_only":      {"type": "boolean"},
                    "attach_to_phone":  {"type": "boolean"},
                    "camera_dependent": {"type": "boolean"},
                    "no_android":       {"type": "boolean"},
                },
            },
            "price_mentioned": {
                "type": ["string", "null"],
                "description": "First price string found, e.g. '$149', or null",
            },
            "sentiment_inito": {
                "type": "number", "minimum": -1, "maximum": 1,
                "description": "Sentiment toward Inito: -1 very negative, 0 neutral, 1 very positive",
            },
            "competitor_framing": {
                "type": "boolean",
                "description": "True if the page frames a competitor (e.g. Mira) as better than Inito",
            },
            "confidence": {
                "type": "number", "minimum": 0, "maximum": 1,
                "description": "Confidence in this classification. Low (<0.6) flags the row for human review.",
            },
        },
    },
}

def judge(url: str, text: str, regex_flags: dict) -> dict:
    excerpt = text[:8000] if text else ""
    user = (f"URL: {url}\nRegex hints: {json.dumps({k:v for k,v in regex_flags.items() if k!='prices_seen'})}\n"
            f"Prices seen: {regex_flags.get('prices_seen')}\n\nPAGE TEXT:\n{excerpt}")
    try:
        resp = claude.messages.create(
            model=CFG["limits"]["judge_model"], max_tokens=400,
            system=JUDGE_SYSTEM,
            tools=[JUDGE_TOOL],
            tool_choice={"type": "tool", "name": "classify_page"},
            messages=[{"role": "user", "content": user}])
        tool_block = next((b for b in resp.content if b.type == "tool_use"), None)
        if tool_block:
            return tool_block.input
        raise ValueError("no tool_use block in response")
    except Exception as e:
        log(f"  judge fallback for {url}: {e}")
        # deterministic fallback from regex if the judge call/parse fails
        cc = {k: regex_flags.get(k, False) for k in ("iphone_only", "attach_to_phone", "camera_dependent", "no_android")}
        any_stale = any(cc.values())
        has_current = regex_flags.get("current_signal", False)
        status = ("mixed" if (any_stale and has_current)
                  else "stale" if any_stale
                  else "current" if has_current else "current")
        return {"status": status, "current_product_named": has_current,
                "claims_confirmed": cc, "price_mentioned": (regex_flags.get("prices_seen") or [None])[0],
                "sentiment_inito": 0.0, "competitor_framing": False, "confidence": 0.5, "_fallback": True}

def ownership(url: str) -> str:
    d = domain_of(url)
    if d in CFG["competitor_domains"]:
        return "competitor"
    if d in CFG["owned_domains"] and not any(a in url for a in CFG["owned_app_ids"]):
        # app/play stores are owned only when it's Inito's own app id
        if d in ("apps.apple.com", "play.google.com"):
            return "third_party"
        return "owned"
    if d in CFG["owned_domains"] and any(a in url for a in CFG["owned_app_ids"]):
        return "owned"
    if d == "amazon.com" and "/dp/" in url:
        return "owned_marketplace"   # Inito's own ASINs; verify seller in practice
    return "third_party"


# ---------- stage 4: persist + diff ----------
def persist(rows: list[dict]):
    df = pd.DataFrame(rows)
    df["run_date"] = RUN_DATE
    snap = DATA / f"observations_{RUN_DATE}.parquet"
    df.to_parquet(snap, index=False)
    # rolling history for diffing
    hist = DATA / "observations_history.parquet"
    if hist.exists():
        prev = pd.read_parquet(hist)
        prev = prev[prev["run_date"] != RUN_DATE]
        df = pd.concat([prev, df], ignore_index=True)
    df.to_parquet(hist, index=False)
    # latest snapshot as CSV for Sheets import
    pd.DataFrame(rows).to_csv(DATA / "latest_snapshot.csv", index=False)
    # low-confidence rows -> human review queue
    review = [r for r in rows if r.get("confidence", 1.0) < 0.6]
    if review:
        rq_path = DATA / "review_queue.csv"
        rq_new = pd.DataFrame(review)
        rq_new["flagged_date"] = RUN_DATE
        if rq_path.exists():
            existing = pd.read_csv(rq_path)
            rq_new = pd.concat([existing, rq_new], ignore_index=True).drop_duplicates(
                subset=["url", "flagged_date"])
        rq_new.to_csv(rq_path, index=False)
        log(f"  {len(review)} low-confidence rows -> review_queue.csv")
    log(f"persisted {len(rows)} rows -> {snap.name}")
    return df


def _kappa_regex_vs_judge(rows: list[dict]) -> float:
    """Cohen's Kappa: agreement between regex heuristic and LLM judge across all rows."""
    try:
        from sklearn.metrics import cohen_kappa_score
    except ImportError:
        return float("nan")
    regex_labels, judge_labels = [], []
    for r in rows:
        any_stale = any([r.get("claim_iphone_only"), r.get("claim_attach_to_phone"),
                         r.get("claim_camera_dependent"), r.get("claim_no_android")])
        has_current = r.get("current_product_named", False)
        regex_status = ("mixed" if (any_stale and has_current)
                        else "stale" if any_stale else "current")
        regex_labels.append(regex_status)
        judge_labels.append(r.get("status", "current"))
    try:
        return round(float(cohen_kappa_score(regex_labels, judge_labels,
                                             labels=["stale", "mixed", "current"])), 3)
    except Exception:
        return float("nan")


def _run_quality_score(metrics: dict, mdf: pd.DataFrame) -> tuple[float, dict]:
    """0-100 composite run quality score: coverage, judge confidence, kappa, stale trend."""
    def _safe(v, default=0.0):
        return default if (v != v) else float(v)  # nan guard

    scores = {}
    scores["coverage"]   = min(metrics["total_urls"] / 500, 1.0) * 25
    scores["confidence"] = _safe(metrics.get("mean_judge_confidence"), 0.7) * 25
    scores["kappa"]      = max(_safe(metrics.get("kappa_regex_judge"), 0.5), 0.0) * 25

    if len(mdf) >= 4:
        rolling_avg = mdf.tail(4)["stale_or_mixed"].mean()
        current = metrics["stale_or_mixed"]
        progress = max(0.0, (rolling_avg - current) / max(rolling_avg, 1.0))
        scores["progress"] = min(progress * 10, 1.0) * 25
    else:
        scores["progress"] = 12.5  # neutral on baseline runs

    total = round(sum(scores.values()), 1)
    return total, {k: round(v, 1) for k, v in scores.items()}


_SOCIAL_PLATFORMS = {"web", "bing", "news", "reddit", "instagram", "x", "youtube", "tiktok"}

def compute_metrics(df_all: pd.DataFrame, current_rows=None):
    cur = df_all[df_all["run_date"] == RUN_DATE]
    web = cur[cur["platform"].isin(_SOCIAL_PLATFORMS)]
    def claim_count(c): return int(web["claim_" + c].fillna(False).sum())
    stale = web[web["status"].isin(["stale", "mixed"])]

    kappa     = _kappa_regex_vs_judge(current_rows) if current_rows else float("nan")
    has_conf  = "confidence" in web.columns
    mean_conf = round(float(web["confidence"].fillna(0.5).mean()), 3) if has_conf else float("nan")
    pct_low   = round(float((web["confidence"].fillna(0.5) < 0.6).mean()), 3) if has_conf else float("nan")

    metrics = {
        "run_date":               RUN_DATE,
        "total_urls":             int(web["url"].nunique()),
        "stale_or_mixed":         int(len(stale)),
        "owned_stale":            int(len(stale[stale["ownership"].isin(["owned", "owned_marketplace"])])),
        "competitor_negative":    int(len(web[(web["ownership"] == "competitor") &
                                              ((web["sentiment_inito"] < 0) | (web["competitor_framing"] == True))])),
        "claim_iphone_only":      claim_count("iphone_only"),
        "claim_attach_to_phone":  claim_count("attach_to_phone"),
        "claim_camera_dependent": claim_count("camera_dependent"),
        "claim_no_android":       claim_count("no_android"),
        "mean_sentiment":         round(float(web["sentiment_inito"].fillna(0).mean()), 3),
        "share_of_voice_category": _sov(cur),
        "kappa_regex_judge":      kappa,
        "mean_judge_confidence":  mean_conf,
        "pct_low_confidence":     pct_low,
    }

    mpath = DATA / "metrics.csv"
    mdf = pd.read_csv(mpath) if mpath.exists() else pd.DataFrame()
    mdf = pd.concat([mdf[mdf.get("run_date", "") != RUN_DATE] if len(mdf) else mdf,
                     pd.DataFrame([metrics])], ignore_index=True)

    quality_score, quality_breakdown = _run_quality_score(metrics, mdf)
    metrics["run_quality_score"] = quality_score
    mdf.loc[mdf.index[-1], "run_quality_score"] = quality_score
    mdf.to_csv(mpath, index=False)
    log(f"run quality score: {quality_score}/100  breakdown: {quality_breakdown}")
    return metrics, mdf

def _sov(cur: pd.DataFrame) -> float:
    """Share of voice on category queries: fraction where an owned domain appears in top 10."""
    cat = cur[(cur["intent"] == "category") &
              (cur["platform"].isin(["web", "bing"])) &
              (cur["rank"].between(1, 10))]
    if cat.empty:
        return 0.0
    by_q = cat.groupby("query")["ownership"].apply(lambda s: (s.isin(["owned", "owned_marketplace"])).any())
    return round(float(by_q.mean()), 3)

def print_diff(mdf: pd.DataFrame):
    if len(mdf) < 2:
        log("baseline run — no prior to diff against.")
        return
    a, b = mdf.iloc[-2], mdf.iloc[-1]
    log(f"--- DIFF {a['run_date']} -> {b['run_date']} ---")
    for k in ["stale_or_mixed", "owned_stale", "competitor_negative",
              "claim_iphone_only", "claim_attach_to_phone", "claim_camera_dependent",
              "claim_no_android", "mean_sentiment", "share_of_voice_category",
              "kappa_regex_judge", "mean_judge_confidence", "pct_low_confidence",
              "run_quality_score"]:
        try:
            av = float(a[k]) if k in a.index else float("nan")
            bv = float(b[k]) if k in b.index else float("nan")
            if av != av or bv != bv:  # either is nan
                continue
            d = bv - av
            arrow = "↓" if d < 0 else ("↑" if d > 0 else "·")
            log(f"  {k:28} {av:>7.3g} -> {bv:>7.3g}  {arrow}{abs(d):.3g}")
        except (TypeError, ValueError):
            continue


# ---------- orchestration ----------
def _safe_discover(fn, label: str) -> list[dict]:
    """Run a discovery function and return results, logging any failure gracefully."""
    try:
        results = fn()
        log(f"  {label}: {len(results)} records")
        return results
    except Exception as e:
        log(f"  {label} FAILED (skipping): {type(e).__name__}: {e}")
        return []

def refresh(no_social: bool):
    t0 = time.time()
    log("STAGE 1 discover")
    recs = (
        _safe_discover(discover_serp,              "serp")
        + _safe_discover(discover_bing,            "bing")
        + _safe_discover(discover_news,            "news")
        + _safe_discover(discover_reddit,          "reddit")
        + _safe_discover(discover_google_ai_mode,  "google_ai_mode")
    )
    if not no_social:
        recs += _safe_discover(discover_social, "social")

    # keep best record per normalized url (lowest rank / longest snippet)
    best = {}
    for r in recs:
        u = normalize_url(r["url"])
        r["url"] = u
        keep = best.get(u)
        if keep is None or (r["rank"] and r["rank"] < (keep["rank"] or 999)):
            best[u] = r
    records = list(best.values())
    log(f"  {len(records)} unique URLs after dedupe")

    log("STAGE 2 enrich")
    text = enrich_content([r["url"] for r in records])

    log("STAGE 3 classify")
    rows = []
    for r in records:
        body = text.get(r["url"], "") or r.get("snippet", "")
        flags = detect_claims(body)
        verdict = judge(r["url"], body, flags) if body else {
            "status": "unknown", "current_product_named": False,
            "claims_confirmed": {}, "price_mentioned": None,
            "sentiment_inito": 0.0, "competitor_framing": False, "confidence": 0.0}
        cc = verdict.get("claims_confirmed", {})
        rows.append({
            "url": r["url"], "domain": domain_of(r["url"]), "platform": r["platform"],
            "query": r["query"], "intent": r["intent"], "rank": r["rank"],
            "ownership": ownership(r["url"]), "status": verdict.get("status"),
            "current_product_named": verdict.get("current_product_named"),
            "claim_iphone_only": cc.get("iphone_only", flags.get("iphone_only")),
            "claim_attach_to_phone": cc.get("attach_to_phone", flags.get("attach_to_phone")),
            "claim_camera_dependent": cc.get("camera_dependent", flags.get("camera_dependent")),
            "claim_no_android": cc.get("no_android", flags.get("no_android")),
            "price_mentioned": verdict.get("price_mentioned"),
            "sentiment_inito": verdict.get("sentiment_inito", 0.0),
            "competitor_framing": verdict.get("competitor_framing", False),
            "confidence": verdict.get("confidence", 0.5),
            "title": r.get("title", ""),
        })

    log("STAGE 4 persist + diff")
    df_all = persist(rows)
    metrics, mdf = compute_metrics(df_all, current_rows=rows)
    safe_metrics = {k: (None if v != v else v) for k, v in metrics.items()}
    log(f"metrics: {json.dumps(safe_metrics, indent=2)}")
    print_diff(mdf)
    log(f"done in {time.time()-t0:.0f}s")


def diff_only():
    hist = DATA / "observations_history.parquet"
    if not hist.exists():
        sys.exit("no history yet — run --refresh first")
    _, mdf = compute_metrics(pd.read_parquet(hist))
    print_diff(mdf)


# ---------- LLM brand visibility ----------

LLM_JUDGE_SYSTEM = """You analyze an LLM-generated response about the fertility monitor brand Inito.
Extract structured signals about brand visibility, accuracy, and framing.

Context: Inito's CURRENT product is the "InSight Wireless Reader" — Wi-Fi enabled, works on iOS AND Android, no phone camera or clip needed.
The OLD product clipped onto an iPhone and used the phone camera; it was iPhone-only.

Use the analyze_llm_response tool to return your structured findings."""

LLM_JUDGE_TOOL = {
    "name": "analyze_llm_response",
    "description": "Extract brand visibility signals from an LLM-generated response about Inito.",
    "input_schema": {
        "type": "object",
        "required": ["inito_mentioned", "inito_recommended", "stale_product_described",
                     "stale_excerpt", "sources_cited", "sentiment_inito",
                     "competitors_named", "competitor_preferred", "confidence"],
        "properties": {
            "inito_mentioned": {
                "type": "boolean",
                "description": "True if Inito is mentioned by name in the response",
            },
            "inito_rank": {
                "type": ["integer", "null"],
                "description": "1-based position where Inito first appears among recommended products, null if not mentioned",
            },
            "inito_recommended": {
                "type": "boolean",
                "description": "True if the response recommends or positively endorses Inito",
            },
            "stale_product_described": {
                "type": "boolean",
                "description": "True if the response describes Inito as iPhone-only, camera-based, or requiring phone clip — the OLD product",
            },
            "stale_excerpt": {
                "type": ["string", "null"],
                "description": "The exact sentence or phrase from the response that contains the stale claim, or null if none. Quote verbatim.",
            },
            "sources_cited": {
                "type": "array",
                "items": {"type": "string"},
                "description": "All URLs or domain names cited as sources in the response (extract from markdown links, footnotes, inline citations). E.g. ['https://fertilitys.com/inito-review', 'https://leafsnap.com/inito']",
            },
            "sentiment_inito": {
                "type": "number", "minimum": -1, "maximum": 1,
                "description": "Sentiment toward Inito: -1 very negative, 0 neutral, 1 very positive",
            },
            "competitors_named": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of competitor brand names mentioned (e.g. ['Mira', 'Kegg', 'Clearblue'])",
            },
            "competitor_preferred": {
                "type": ["string", "null"],
                "description": "The competitor the response most clearly prefers over Inito, or null",
            },
            "confidence": {
                "type": "number", "minimum": 0, "maximum": 1,
                "description": "Confidence in this analysis",
            },
        },
    },
}


def judge_llm_response(prompt: str, model: str, response_text: str) -> dict:
    """Classify an LLM-generated response for Inito brand visibility signals."""
    user = f"PROMPT ASKED: {prompt}\nLLM MODEL: {model}\n\nLLM RESPONSE:\n{response_text[:6000]}"
    try:
        resp = claude.messages.create(
            model=CFG["limits"]["judge_model"], max_tokens=400,
            system=LLM_JUDGE_SYSTEM,
            tools=[LLM_JUDGE_TOOL],
            tool_choice={"type": "tool", "name": "analyze_llm_response"},
            messages=[{"role": "user", "content": user}])
        tool_block = next((b for b in resp.content if b.type == "tool_use"), None)
        if tool_block:
            return tool_block.input
        raise ValueError("no tool_use block")
    except Exception as e:
        log(f"  llm_judge fallback for {model}/{prompt[:40]}: {e}")
        mentioned = "inito" in response_text.lower()
        # extract URLs from response text as fallback source list
        urls = re.findall(r'https?://[^\s\)\]\"\'>,]+', response_text)
        return {
            "inito_mentioned": mentioned, "inito_rank": None,
            "inito_recommended": False, "stale_product_described": False,
            "stale_excerpt": None, "sources_cited": urls[:10],
            "sentiment_inito": 0.0, "competitors_named": [], "competitor_preferred": None,
            "confidence": 0.3, "_fallback": True,
        }


def _wilson_ci(k: int, n: int, z: float = 1.96):
    """Wilson score 95% CI for a proportion. Returns (point_est, lo, hi)."""
    if n == 0:
        return 0.0, 0.0, 0.0
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * (p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5 / denom
    return round(p, 3), round(max(0.0, center - half), 3), round(min(1.0, center + half), 3)


def _mean_ci(values: list, z: float = 1.96):
    """Mean ± z·SE for a continuous variable. Returns (mean, lo, hi)."""
    n = len(values)
    if n == 0:
        return 0.0, 0.0, 0.0
    mu = sum(values) / n
    if n == 1:
        return round(mu, 3), round(mu, 3), round(mu, 3)
    var = sum((x - mu) ** 2 for x in values) / (n - 1)
    se = (var / n) ** 0.5
    return round(mu, 3), round(mu - z * se, 3), round(mu + z * se, 3)


def discover_llm_visibility(models=None, num_runs=None) -> list[dict]:
    """Run all llm_visibility_prompts through bulk-llm-runner (ChatGPT + Claude via API).
    Perplexity is handled by discover_perplexity_web() instead (true web interface).
    Google AI Mode / Gemini is handled by discover_google_ai_mode().

    Models run in parallel (ThreadPoolExecutor). Resume logic skips (model, run_index)
    combos already persisted for today. Failures log one error row per prompt.
    """
    prompts_cfg = CFG.get("llm_visibility_prompts", [])
    if not prompts_cfg:
        log("  no llm_visibility_prompts in config.json — skipping LLM visibility run")
        return []

    # perplexity/sonar-pro stays here: its API always does live web search, making it
    # equivalent to the Perplexity web UI. Web-interface scrapers are Cloudflare-blocked.
    models = models or CFG.get("llm_models", ["openai/gpt-5"])
    num_runs = num_runs or CFG.get("llm_num_runs", 1)
    system_prompt = CFG.get("llm_system_prompt", "")
    proxy_group = CFG.get("llm_proxy_group", "DATACENTER")
    proxy_country = CFG.get("proxy_country", "US")
    actor_slug = CFG["actors"]["llm_runner"]
    prompt_strings = [p["prompt"] for p in prompts_cfg]
    intent_map = {p["prompt"]: p["intent"] for p in prompts_cfg}

    # Resume: load already-completed (model, run_index) from today's history
    completed = set()
    hist_path = DATA / "llm_visibility_history.parquet"
    if hist_path.exists():
        try:
            prev = pd.read_parquet(hist_path)
            today = prev[prev["run_date"] == RUN_DATE]
            # only count rows that have real data (not prior error rows)
            real = today[today.get("status", pd.Series(dtype=str)) != "error"] if "status" in today.columns else today
            real = today[today["inito_mentioned"].notna()] if "inito_mentioned" in today.columns else today
            for _, r in real.iterrows():
                completed.add((str(r["model"]), int(r.get("run_index", 1))))
            if completed:
                log(f"  resume: {len(completed)} (model, run_index) combos already done today — skipping")
        except Exception as exc:
            log(f"  resume check failed (will re-run all): {exc}")

    def _run_one(model, run_idx):
        if (model, run_idx) in completed:
            log(f"  LLM visibility: {model} run {run_idx} already done — skipping")
            return []

        log(f"  LLM visibility: {model} run {run_idx}/{num_runs} ({len(prompt_strings)} prompts)")
        run_input = {
            "prompts": prompt_strings,
            "model": model,
            "proxyConfiguration": {
                "useApifyProxy": True,
                "apifyProxyGroups": [proxy_group],
                "apifyProxyCountry": proxy_country,
            },
        }
        if system_prompt:
            run_input["systemPrompt"] = system_prompt

        try:
            items = run_actor(actor_slug, run_input, f"llm/{model}/run{run_idx}")
        except Exception as e:
            log(f"  llm/{model}/run{run_idx} FAILED: {e}")
            # one error row per prompt so failures are visible in the sheet
            return [{
                "run_date": RUN_DATE, "run_index": run_idx, "model": model,
                "prompt": p, "intent": intent_map.get(p, "unknown"),
                "status": "error", "error": str(e)[:300],
                "response_text": None, "inito_mentioned": None,
                "inito_rank": None, "inito_recommended": None,
                "stale_product_described": None, "stale_excerpt": None,
                "sources_cited": "[]", "sentiment_inito": None,
                "competitors_named": "[]", "competitor_preferred": None,
                "confidence": None,
            } for p in prompt_strings]

        result = []
        for item in items:
            prompt_text = item.get("prompt") or item.get("input") or ""
            response_text = item.get("response") or item.get("output") or item.get("answer") or ""
            if not prompt_text:
                continue
            verdict = judge_llm_response(prompt_text, model, response_text)
            inline_urls = re.findall(r'https?://[^\s\)\]\"\'>,]+', response_text)
            judge_sources = verdict.get("sources_cited") or []
            all_sources = list(dict.fromkeys(judge_sources + inline_urls))[:15]
            result.append({
                "run_date":                RUN_DATE,
                "run_index":               run_idx,
                "model":                   model,
                "prompt":                  prompt_text,
                "intent":                  intent_map.get(prompt_text, "unknown"),
                "response_text":           response_text[:4000],
                "inito_mentioned":         verdict.get("inito_mentioned", False),
                "inito_rank":              verdict.get("inito_rank"),
                "inito_recommended":       verdict.get("inito_recommended", False),
                "stale_product_described": verdict.get("stale_product_described", False),
                "stale_excerpt":           verdict.get("stale_excerpt"),
                "sources_cited":           json.dumps(all_sources),
                "sentiment_inito":         verdict.get("sentiment_inito", 0.0),
                "competitors_named":       json.dumps(verdict.get("competitors_named", [])),
                "competitor_preferred":    verdict.get("competitor_preferred"),
                "confidence":              verdict.get("confidence", 0.3),
            })
        return result

    # run all model × run_index jobs in parallel
    jobs = [(m, r) for m in models for r in range(1, num_runs + 1)]
    rows = []
    with ThreadPoolExecutor(max_workers=min(len(jobs), 10)) as ex:
        futures = {ex.submit(_run_one, m, r): (m, r) for m, r in jobs}
        for fut in as_completed(futures):
            m, r = futures[fut]
            try:
                rows.extend(fut.result())
            except Exception as e:
                log(f"  llm/{m}/run{r} unexpected thread error: {e}")

    log(f"  LLM visibility: {len(rows)} total observations ({num_runs} run(s) × {len(models)} model(s))")
    return rows


def _derive_action(row: dict) -> str:
    """Single most-important action for this (model, prompt) observation."""
    if row.get("status") == "error":
        return f"Actor failed — retry or check {row.get('model','?')} actor config"
    if not row.get("inito_mentioned"):
        return "Not visible — Inito absent from this response; create/update content to enter training data"
    if row.get("stale_product_described"):
        stale_src = row.get("stale_sources") or ""
        src_hint = f" (stale source: {stale_src.split(',')[0]})" if stale_src else ""
        return f"Stale claim detected{src_hint} — submit correction/feedback to {row.get('model','?')} provider"
    cp = row.get("competitor_preferred") or ""
    if cp and not row.get("inito_recommended"):
        return f"Competitor gap — {cp} preferred; build comparison content targeting this prompt"
    if row.get("inito_recommended"):
        return "Positive — monitor to ensure this holds; no action needed"
    return "Neutral mention — strengthen positioning content for this prompt"


def _sources_to_plain(sources_json: str) -> str:
    """Convert JSON array of source URLs to comma-separated plain text (Sheets auto-links)."""
    try:
        items = json.loads(sources_json or "[]")
        # filter to real URLs only; drop bare domain labels
        urls = [s for s in items if s and s.startswith("http")]
        return ", ".join(urls)
    except Exception:
        return ""


def export_llm_csv(rows: list[dict]) -> None:
    """Write a Sheets-friendly CSV with clickable sources, action column, and clean column names."""
    out = []
    for r in rows:
        sources_plain = _sources_to_plain(r.get("sources_cited", "[]"))
        # stale sources = subset of sources that likely carry stale content (heuristic: any URL
        # that appears in the stale_excerpt context — we can't know for sure, so list all sources
        # when stale is True so the analyst can check them)
        stale_sources = sources_plain if r.get("stale_product_described") else ""
        row_out = r.copy()
        row_out["stale_sources_to_fix"] = stale_sources
        row_out["action"] = _derive_action({**r, "stale_sources": stale_sources})
        row_out["sources_cited"] = sources_plain
        out.append(row_out)

    df = pd.DataFrame(out)
    # Rename for clarity
    col_map = {
        "run_date": "date",
        "run_index": "run_#",
        "inito_mentioned": "mentioned",
        "inito_rank": "rank_in_response",
        "inito_recommended": "recommended",
        "stale_product_described": "stale_claim",
        "stale_excerpt": "stale_quote",
        "sources_cited": "sources (clickable URLs)",
        "sentiment_inito": "sentiment (-1 to +1)",
        "competitors_named": "competitors_mentioned",
        "competitor_preferred": "competitor_preferred",
    }
    df = df.rename(columns=col_map)

    # Column order for the sheet
    ordered = [
        "date", "model", "prompt", "intent",
        "mentioned", "rank_in_response", "recommended", "sentiment (-1 to +1)",
        "stale_claim", "stale_quote", "stale_sources_to_fix",
        "competitors_mentioned", "competitor_preferred",
        "sources (clickable URLs)",
        "response_text",
        "action",
        "run_#", "status", "confidence",
    ]
    ordered = [c for c in ordered if c in df.columns]
    extra = [c for c in df.columns if c not in ordered]
    df = df[ordered + extra]

    # Truncate response_text for sheet readability
    if "response_text" in df.columns:
        df["response_text"] = df["response_text"].fillna("").str[:800]

    df.to_csv(DATA / "llm_visibility_latest.csv", index=False)
    log(f"  exported {len(df)} rows -> llm_visibility_latest.csv")


def export_serp_csv() -> None:
    """Write SERP AI Overviews + top organic results to a separate CSV for the sheet."""
    snap = DATA / "latest_snapshot.csv"
    if not snap.exists():
        log("  no latest_snapshot.csv — skipping SERP export")
        return
    df = pd.read_csv(snap)

    # AI Overviews
    ai = df[df["url"].str.startswith("aioverview::", na=False)].copy()
    ai["query"] = ai["url"].str.removeprefix("aioverview::")
    ai_cols = [c for c in ["query", "intent", "snippet", "status", "sentiment"] if c in ai.columns]
    ai_out = ai[ai_cols].copy()
    ai_out = ai_out.rename(columns={"snippet": "ai_overview_text", "status": "inito_status",
                                    "sentiment": "sentiment_score"})
    ai_out.insert(0, "source", "Google AI Overview")

    # Top organic (rank ≤ 5), excluding aioverview rows
    org = df[df["platform"].isin(["web", "news"]) & df["url"].notna()].copy()
    org = org[org["rank"].notna() & (org["rank"] <= 5)]
    org_cols = [c for c in ["rank", "url", "title", "query", "intent", "platform",
                             "ownership", "status", "sentiment"] if c in org.columns]
    org_out = org[org_cols].copy()
    org_out.insert(0, "source", "Google Organic")

    combined = pd.concat([ai_out, org_out], ignore_index=True)
    combined.to_csv(DATA / "serp_latest.csv", index=False)
    log(f"  exported {len(ai_out)} AI overviews + {len(org_out)} top-5 organic -> serp_latest.csv")


def persist_llm(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    snap = DATA / f"llm_visibility_{RUN_DATE}.parquet"
    df.to_parquet(snap, index=False)
    hist = DATA / "llm_visibility_history.parquet"
    if hist.exists():
        prev = pd.read_parquet(hist)
        prev = prev[prev["run_date"] != RUN_DATE]
        df = pd.concat([prev, df], ignore_index=True)
    df.to_parquet(hist, index=False)
    export_llm_csv(rows)          # human-readable sheet export (replaces raw CSV)
    log(f"persisted {len(rows)} LLM visibility rows -> {snap.name}")
    return df


def compute_llm_metrics(df_all: pd.DataFrame) -> dict:
    """Compute point estimates + 95% Wilson/mean CIs pooled across all run_index values.

    Binary metrics (mention, recommend, stale): Wilson score CI — handles small n and edge
    proportions (0, 1) correctly. Continuous (sentiment): mean ± 1.96·SE.

    Per-prompt stats are written to llm_visibility_stats.csv for drill-down.
    Aggregate per-model and overall stats go to llm_metrics.csv (the time series).
    """
    cur = df_all[df_all["run_date"] == RUN_DATE]
    if cur.empty:
        return {}

    num_runs = int(cur["run_index"].nunique()) if "run_index" in cur.columns else 1
    log(f"  computing LLM metrics over {len(cur)} observations ({num_runs} run(s))")

    def _prop_stats(series, prefix):
        k = int(series.sum()); n = len(series)
        p, lo, hi = _wilson_ci(k, n)
        return {f"{prefix}": p, f"{prefix}_lo": lo, f"{prefix}_hi": hi, f"{prefix}_n": n}

    def _cont_stats(series, prefix):
        vals = series.dropna().tolist()
        mu, lo, hi = _mean_ci(vals)
        return {f"{prefix}": mu, f"{prefix}_lo": lo, f"{prefix}_hi": hi}

    # per-prompt × model stats (most granular — for the drill-down CSV)
    prompt_rows = []
    for (model, prompt), grp in cur.groupby(["model", "prompt"]):
        row = {"run_date": RUN_DATE, "model": model, "prompt": prompt,
               "intent": grp["intent"].iloc[0], "n_obs": len(grp)}
        row.update(_prop_stats(grp["inito_mentioned"],         "mention"))
        row.update(_prop_stats(grp["inito_recommended"],       "recommended"))
        row.update(_prop_stats(grp["stale_product_described"], "stale"))
        row.update(_cont_stats(grp["sentiment_inito"],         "sentiment"))
        ranks = grp["inito_rank"].dropna().tolist()
        if ranks:
            mu, lo, hi = _mean_ci(ranks)
            row.update({"rank_mean": mu, "rank_lo": lo, "rank_hi": hi})
        prompt_rows.append(row)
    pd.DataFrame(prompt_rows).to_csv(DATA / "llm_visibility_stats.csv", index=False)

    # per-model aggregate (pooled across all prompts × runs)
    metrics = {"run_date": RUN_DATE, "llm_total_observations": int(len(cur)),
               "llm_num_runs": num_runs}
    for model, grp in cur.groupby("model"):
        safe = model.replace("-", "_").replace(".", "_").replace("/", "_")
        metrics.update({f"llm_{safe}_{k}": v for k, v in
                        _prop_stats(grp["inito_mentioned"],         "mention").items()})
        metrics.update({f"llm_{safe}_{k}": v for k, v in
                        _prop_stats(grp["inito_recommended"],       "recommended").items()})
        metrics.update({f"llm_{safe}_{k}": v for k, v in
                        _prop_stats(grp["stale_product_described"], "stale").items()})
        metrics.update({f"llm_{safe}_{k}": v for k, v in
                        _cont_stats(grp["sentiment_inito"],         "sentiment").items()})

    # overall aggregate (all models + runs pooled)
    metrics.update({f"llm_{k}": v for k, v in
                    _prop_stats(cur["inito_mentioned"],         "mention").items()})
    metrics.update({f"llm_{k}": v for k, v in
                    _prop_stats(cur["inito_recommended"],       "recommended").items()})
    metrics.update({f"llm_{k}": v for k, v in
                    _prop_stats(cur["stale_product_described"], "stale").items()})
    metrics.update({f"llm_{k}": v for k, v in
                    _cont_stats(cur["sentiment_inito"],         "sentiment").items()})

    mpath = DATA / "llm_metrics.csv"
    mdf = pd.read_csv(mpath) if mpath.exists() else pd.DataFrame()
    mdf = pd.concat([mdf[mdf.get("run_date", "") != RUN_DATE] if len(mdf) else mdf,
                     pd.DataFrame([metrics])], ignore_index=True)
    mdf.to_csv(mpath, index=False)

    safe_m = {k: (None if isinstance(v, float) and v != v else v) for k, v in metrics.items()}
    log(f"LLM visibility metrics: {json.dumps(safe_m, indent=2)}")
    return metrics


def _perplexity_web_to_llm_rows(items: list[dict]) -> list[dict]:
    """Convert discover_perplexity_web() records into llm_visibility row format."""
    rows = []
    for it in items:
        q = it.get("query", "")
        answer = it.get("snippet", "")
        if not answer:
            continue
        verdict = judge_llm_response(q, "perplexity/web", answer)
        # merge actor-pre-extracted sources with judge sources
        actor_sources = json.loads(it.get("_perplexity_sources", "[]"))
        judge_sources = verdict.get("sources_cited") or []
        inline_urls = re.findall(r'https?://[^\s\)\]\"\'>,]+', answer)
        all_sources = list(dict.fromkeys(judge_sources + actor_sources + inline_urls))[:15]
        rows.append({
            "run_date": RUN_DATE, "run_index": 1, "model": "perplexity/web",
            "prompt": q, "intent": it.get("intent", "unknown"),
            "response_text": answer[:4000],
            "inito_mentioned": verdict.get("inito_mentioned",
                                it.get("_perplexity_mentioned") or False),
            "inito_rank": verdict.get("inito_rank"),
            "inito_recommended": verdict.get("inito_recommended", False),
            "stale_product_described": verdict.get("stale_product_described", False),
            "stale_excerpt": verdict.get("stale_excerpt"),
            "sources_cited": json.dumps(all_sources),
            "sentiment_inito": verdict.get("sentiment_inito", 0.0),
            "competitors_named": json.dumps(verdict.get("competitors_named",
                                    it.get("_perplexity_competitors", []))),
            "competitor_preferred": verdict.get("competitor_preferred"),
            "confidence": verdict.get("confidence", 0.3),
        })
    return rows


def _google_ai_mode_to_llm_rows(items: list[dict]) -> list[dict]:
    """Convert discover_google_ai_mode() records into llm_visibility row format."""
    rows = []
    for it in items:
        q = it.get("query", "")
        answer = it.get("snippet", "")
        if not answer:
            continue
        verdict = judge_llm_response(q, "google/ai-mode", answer)
        actor_sources = json.loads(it.get("_googleai_sources", "[]"))
        judge_sources = verdict.get("sources_cited") or []
        inline_urls = re.findall(r'https?://[^\s\)\]\"\'>,]+', answer)
        all_sources = list(dict.fromkeys(judge_sources + actor_sources + inline_urls))[:15]
        rows.append({
            "run_date": RUN_DATE, "run_index": 1, "model": "google/ai-mode",
            "prompt": q, "intent": it.get("intent", "unknown"),
            "response_text": answer[:4000],
            "inito_mentioned": verdict.get("inito_mentioned", False),
            "inito_rank": verdict.get("inito_rank"),
            "inito_recommended": verdict.get("inito_recommended", False),
            "stale_product_described": verdict.get("stale_product_described", False),
            "stale_excerpt": verdict.get("stale_excerpt"),
            "sources_cited": json.dumps(all_sources),
            "sentiment_inito": verdict.get("sentiment_inito", 0.0),
            "competitors_named": json.dumps(verdict.get("competitors_named", [])),
            "competitor_preferred": verdict.get("competitor_preferred"),
            "confidence": verdict.get("confidence", 0.3),
        })
    return rows


def run_llm_visibility(models=None):
    """Standalone: discover + persist + compute LLM visibility metrics.

    Sources:
      - fayoussef/bulk-llm-runner  → ChatGPT (gpt-5), Gemini, Perplexity sonar-pro (live search),
                                     Claude. All 4 via API; Perplexity's API always searches live.
      - scrape.badger/google-ai-mode-scraper → Google AI Mode (Gemini-powered, best-effort;
                                     503s are transient, actor is wrapped in try/except)
    Note: Perplexity web-interface scraper (zhorex) is blocked by Cloudflare; API is equivalent.
    """
    t0 = time.time()
    rows = []

    log("LLM VISIBILITY stage 1a: bulk-runner (ChatGPT + Gemini + Perplexity + Claude)")
    bulk_rows = discover_llm_visibility(models=models)
    rows.extend(bulk_rows)

    # Google AI Mode removed from LLM visibility: actor has hardcoded 10-attempt
    # exponential backoff on 502s (actor-internal, cannot be disabled externally).
    # It remains in refresh() for SERP-context crawls only.

    if not rows:
        log("no LLM visibility rows — check actor slugs and config")
        return
    log(f"  total rows collected: {len(rows)}")
    log("LLM VISIBILITY stage 2: persist")
    df_all = persist_llm(rows)
    log("LLM VISIBILITY stage 3: metrics")
    compute_llm_metrics(df_all)
    log("LLM VISIBILITY stage 4: SERP export")
    export_serp_csv()
    log(f"LLM visibility done in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="run the full pipeline")
    ap.add_argument("--no-social", action="store_true", help="skip IG/X/YouTube/TikTok (cheaper)")
    ap.add_argument("--diff-only", action="store_true", help="recompute metrics + diff only")
    ap.add_argument("--llm", action="store_true",
                    help="run LLM brand visibility (bulk-llm-runner across configured models)")
    ap.add_argument("--llm-models", nargs="+", metavar="MODEL",
                    help="override models for --llm (e.g. gpt-4o-mini gemini-2.0-flash sonar)")
    a = ap.parse_args()
    if a.diff_only:
        diff_only()
    elif a.llm:
        run_llm_visibility(models=a.llm_models or None)
    elif a.refresh:
        refresh(a.no_social)
        if a.llm:
            run_llm_visibility(models=a.llm_models or None)
    else:
        ap.print_help()
