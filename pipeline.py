#!/usr/bin/env python3
"""
Inito GEO monitor — Apify-backed brand-snapshot pipeline (CSV-only, no time series).

Every run is a SELF-CONTAINED SNAPSHOT of what sources currently say about the brand Inito.
Nothing accumulates across runs: each run writes its own folder under data/ with one or two
lean sheets and nothing else.

Two independent tracks, both driven from the CLI:

  Track A — Web/SERP  ->  web_observations.csv  (1 row per web source)
    discover (Google SERP + News + Ads + Reddit) -> enrich (page text)
    -> classify (Claude judge: what it says about Inito + competition, links, price)

  Track B — LLM brand visibility (live-web assistants only)  ->  llm_observations.csv
    discover (ChatGPT via Apify actor + Perplexity sonar API; `num_runs` samples per
    prompt×surface, US-pinned) -> classify (Claude judge)

On-demand:
    python pipeline.py --list-topics                              # show the topic catalog
    python pipeline.py --refresh                                  # Track A (interactive select)
    python pipeline.py --llm                                       # Track B (interactive select)
    python pipeline.py --llm --surfaces chatgpt --prompts 1,7 --num-runs 1 -y     # scripted
    python pipeline.py --llm --extra-prompts "Inito vs Oova::comparison" -y        # ad-hoc prompt

Env (.env):  APIFY_TOKEN, ANTHROPIC_API_KEY (+ optional PERPLEXITY_API_KEY for the Perplexity surface)
"""

import argparse, json, os, re, sys, time, datetime as dt, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, List, Dict, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit
from pathlib import Path

from apify_client import ApifyClient
from anthropic import Anthropic
import pandas as pd

ROOT = Path(__file__).parent
DATA = ROOT / "data"; DATA.mkdir(exist_ok=True)
CFG = json.loads((ROOT / "config.json").read_text())
RUN_DATE = dt.date.today().isoformat()

# Track A discovery sources selectable from the CLI.
WEB_SOURCES = ["serp", "news", "ads", "reddit"]
# Non-production inito subdomains that should never be publicly cited — flagged in the sheet.
NONPROD_PREFIXES = ("preprod", "staging", "stage", "dev", "test", "qa", "uat")

# Output column order (also the contract the writers/tests rely on).
WEB_COLUMNS = ["source", "url", "query", "intent", "topic_id", "ownership", "says_about_inito",
               "mentions_competition", "competition_summary", "competitors_named", "sentiment",
               "price", "links_on_source", "nonprod_url", "title"]
LLM_COLUMNS = ["surface", "run", "prompt", "intent", "topic_id", "mentioned", "rank", "recommended",
               "says_about_inito", "mentions_competition", "competition_summary", "competitors_named",
               "sentiment", "price", "sources_cited", "nonprod_url", "response_text",
               "status", "error_note"]


# ---- topic catalog: both tracks send the SAME `query` string verbatim ----
def web_topics() -> List[dict]:
    """Track A view of config.topics: [{q, intent, id}]."""
    return [{"q": t["query"], "intent": t["intent"], "id": t["id"]}
            for t in CFG["topics"] if t.get("query")]

def llm_topics() -> List[dict]:
    """Track B view of config.topics: [{prompt, intent, id}] — same string as web_topics()."""
    return [{"prompt": t["query"], "intent": t["intent"], "id": t["id"]}
            for t in CFG["topics"] if t.get("query")]


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
# Optional — only the Perplexity (sonar) surface needs it. Absent ⇒ that surface fails fast into
# error rows; the rest of the pipeline is unaffected.
PPLX_KEY = os.environ.get("PERPLEXITY_API_KEY", "")


# ---------- helpers ----------
def log(msg): print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)

def normalize_url(u: str) -> str:
    """Canonical URL: drop www, tracking params (utm_*, disc_code, workflow, os, …) and fragment.
    All query params are stripped — that canonicalises the noisy citation links ChatGPT emits."""
    try:
        s = urlsplit(u.strip())
        host = s.netloc.lower().removeprefix("www.")
        return urlunsplit((s.scheme or "https", host, s.path.rstrip("/"), "", ""))
    except Exception:
        return u.strip()

def domain_of(u: str) -> str:
    return urlsplit(u).netloc.lower().removeprefix("www.")

def _host_matches(host: str, domains) -> bool:
    """True if host equals a domain or is a subdomain of it (suffix match)."""
    return any(host == d or host.endswith("." + d) for d in domains)

def is_nonprod_owned(url: str) -> bool:
    """True for a non-production Inito host (preprod./staging./… .inito.com)."""
    d = domain_of(url)
    if not _host_matches(d, CFG["owned_domains"]):
        return False
    sub = d[: -len("inito.com")].rstrip(".") if d.endswith("inito.com") else ""
    return (sub.split(".")[0] if sub else "") in NONPROD_PREFIXES

def run_actor(actor_id: str, run_input: dict, label: str) -> list:
    """Call an actor, return its dataset items. Fail-fast — the caller turns any exception into a
    visible error row / `_safe_discover` skip, so there is no retry/backoff here (invariant)."""
    log(f"  actor {actor_id} ({label}) starting…")
    run = apify.actor(actor_id).call(run_input=run_input)
    items = list(apify.dataset(run["defaultDatasetId"]).iterate_items())
    log(f"  actor {actor_id} ({label}) -> {len(items)} items")
    return items

def _extract_urls(text: str) -> List[str]:
    return re.findall(r'https?://[^\s\)\]\"\'>,]+', text or "")

def extract_links(text: str, exclude_url: str = "") -> List[str]:
    """Outbound links mentioned on a page, canonicalised + deduped, excluding the page's own URL."""
    ex = normalize_url(exclude_url)
    out, seen = [], set()
    for u in _extract_urls(text):
        n = normalize_url(u)
        if not n or n == ex or n in seen:
            continue
        seen.add(n); out.append(n)
    return out[:20]

def _competitors_in(text: str) -> List[str]:
    t = (text or "").lower()
    return [b for b in CFG.get("competitor_brands", []) if b.lower() in t]

def perplexity_complete(prompt: str, model: str) -> Tuple[str, List[str]]:
    """Call Perplexity's sonar API directly (OpenAI-compatible) — always live web search + citations.
    Returns (answer_text, source_urls). Raises on transport/HTTP error (caller turns it into an
    error row, fail-fast)."""
    req = urllib.request.Request(
        "https://api.perplexity.ai/chat/completions",
        data=json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}]}).encode(),
        headers={"Authorization": f"Bearer {PPLX_KEY}", "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=90) as r:
        data = json.loads(r.read().decode())
    answer = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    sources = data.get("citations") or [s.get("url", "") for s in (data.get("search_results") or [])]
    return answer, [s for s in sources if s]


# ---------- stage 1: discover (Track A) ----------
def discover_serp(queries: Optional[List[dict]] = None) -> List[dict]:
    """Google SERP via apify/google-search-scraper.
    Captures organic results, AI Overview, and ChatGPT/Perplexity SERP panels."""
    qcfg = queries or web_topics()
    qmap = {c["q"]: c["intent"] for c in qcfg}
    idmap = {c["q"]: c.get("id", "") for c in qcfg}
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
        intent = qmap.get(q, "unknown"); tid = idmap.get(q, "")
        for rank, r in enumerate(item.get("organicResults", []), 1):
            url = r.get("url")
            if not url:
                continue
            out.append({"url": url, "platform": "web", "query": q, "intent": intent, "topic_id": tid,
                        "rank": rank, "title": r.get("title", ""), "snippet": r.get("description", "")})
        # Google AI Overview (Gemini-powered) — passive capture, stored as a pseudo-URL
        ai = item.get("aiOverview") or item.get("aiOverviewText")
        if ai:
            out.append({"url": f"aioverview::{q}", "platform": "ai_overview", "query": q,
                        "intent": intent, "topic_id": tid, "rank": 0, "title": "Google AI Overview",
                        "snippet": ai if isinstance(ai, str) else json.dumps(ai)[:4000]})
        # ChatGPT / Perplexity SERP panels (appear on some Google plans)
        for cgpt in (item.get("chatGptSearchResults") or []):
            text = cgpt.get("text") or cgpt.get("answer") or ""
            if text:
                out.append({"url": f"chatgptsearch::{q}", "platform": "chatgpt_search", "query": q,
                            "intent": intent, "topic_id": tid, "rank": 0, "title": "ChatGPT Search Panel",
                            "snippet": text[:4000]})
        for px in (item.get("perplexitySearchResults") or []):
            text = px.get("text") or px.get("answer") or ""
            if text:
                out.append({"url": f"perplexitysearch::{q}", "platform": "perplexity_search", "query": q,
                            "intent": intent, "topic_id": tid, "rank": 0, "title": "Perplexity Search Panel",
                            "snippet": text[:4000]})
    return out


def discover_news(queries: Optional[List[dict]] = None) -> List[dict]:
    """Google News via the same SERP actor with tbm=nws — press/syndicated content."""
    qcfg = queries or web_topics()
    qmap = {c["q"]: c["intent"] for c in qcfg}
    idmap = {c["q"]: c.get("id", "") for c in qcfg}
    run_input = {
        "queries": "\n".join(qmap.keys()),
        "resultsPerPage": CFG["limits"].get("news_max_per_query", 20),
        "maxPagesPerQuery": 1,
        "countryCode": CFG["market"]["countryCode"],
        "languageCode": CFG["market"]["languageCode"],
        "mobileResults": False,
        "tbm": "nws",
    }
    out = []
    for item in run_actor(CFG["actors"]["serp"], run_input, "news"):
        sq = item.get("searchQuery")
        q = (sq.get("term") if isinstance(sq, dict) else sq) or ""
        intent = qmap.get(q, "unknown")
        for rank, r in enumerate(item.get("organicResults", []), 1):
            url = r.get("url")
            if url:
                out.append({"url": url, "platform": "news", "query": q, "intent": intent,
                            "topic_id": idmap.get(q, ""), "rank": rank, "title": r.get("title", ""),
                            "snippet": r.get("description", "")})
    return out


def _ad_text(item: dict) -> str:
    """Best-effort extraction of an ad's copy from a google-ads-scraper item's variants."""
    parts = []
    for v in (item.get("variants") or []):
        if isinstance(v, dict):
            for k in ("text", "headline", "description", "body"):
                if v.get(k):
                    parts.append(str(v[k]))
    return " ".join(parts)


def ownership_for_ad(advertiser: str, url: str) -> str:
    """Ownership of an ad is by advertiser identity, not landing domain.
    Matches our own ads ('inito') and competitors by their domain label (miracare.com -> 'miracare');
    otherwise falls through to URL-based ownership."""
    adv = (advertiser or "").lower()
    if "inito" in adv:
        return "owned"
    for d in CFG["competitor_domains"]:
        token = d.split(".")[-2] if "." in d else d  # miracare.com -> miracare, ovul.ai -> ovul
        if token and token in adv:
            return "competitor"
    return ownership(url)


def discover_ads() -> List[dict]:
    """Google Ads Transparency Center via lexis-solutions/google-ads-scraper.
    Driven by config.ads_start_urls (one advertiser/domain URL each)."""
    start_urls = CFG.get("ads_start_urls", [])
    if not start_urls:
        log("  ads: no ads_start_urls configured — skipping")
        return []
    run_input = {
        "startUrls": [{"url": u} for u in start_urls],
        "maxItems": CFG["limits"].get("ads_max_items", 100),
        "proxyConfiguration": {"useApifyProxy": True, "apifyProxyCountry": CFG.get("proxy_country", "US")},
    }
    out = []
    for it in run_actor(CFG["actors"]["ads"], run_input, "ads"):
        adv = it.get("advertiserName") or it.get("advertiserId") or ""
        text = _ad_text(it)
        url = it.get("url") or it.get("previewUrl") or ""
        if not (url or text):
            continue
        out.append({"url": url or f"ad::{it.get('creativeId', adv)}", "platform": "ads",
                    "query": "ads", "intent": "ads", "rank": 0, "title": adv,
                    "snippet": text[:4000], "advertiser": adv,
                    "ownership": ownership_for_ad(adv, url)})
    return out


def discover_reddit() -> List[dict]:
    # trudax/reddit-scraper-lite: `searches` + `maxItems`. Residential proxy (datacenter -> 403).
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


WEB_DISCOVERERS: Dict[str, Callable] = {
    "serp": discover_serp,
    "news": discover_news,
    "ads": discover_ads,
    "reddit": discover_reddit,
}


# ---------- stage 2: enrich (full page text for web URLs) ----------
FETCH_CACHE_PATH = DATA / "fetch_cache.csv"
FETCH_CACHE_TTL_DAYS = 7

def load_fetch_cache() -> Dict[str, str]:
    """Returns {normalized_url: text} for URLs fetched within TTL. The cache is a cost saver, not
    analytics — it is the only file that survives between runs."""
    if not FETCH_CACHE_PATH.exists():
        return {}
    df = pd.read_csv(FETCH_CACHE_PATH).fillna({"text": ""})
    cutoff = (dt.date.today() - dt.timedelta(days=FETCH_CACHE_TTL_DAYS)).isoformat()
    fresh = df[df["fetch_date"].astype(str) >= cutoff]
    return {row["url"]: str(row["text"]) for _, row in fresh.iterrows()}

def save_fetch_cache(new_entries: Dict[str, str]):
    """Merge new {url: text} entries into the cache CSV, evicting stale records."""
    today = dt.date.today().isoformat()
    new_rows = pd.DataFrame([{"url": u, "text": t, "fetch_date": today}
                              for u, t in new_entries.items()])
    if FETCH_CACHE_PATH.exists():
        existing = pd.read_csv(FETCH_CACHE_PATH)
        existing = existing[~existing["url"].isin(new_entries.keys())]
        combined = pd.concat([existing, new_rows], ignore_index=True)
    else:
        combined = new_rows
    cutoff = (dt.date.today() - dt.timedelta(days=FETCH_CACHE_TTL_DAYS * 2)).isoformat()
    combined = combined[combined["fetch_date"].astype(str) >= cutoff]
    combined.to_csv(FETCH_CACHE_PATH, index=False)

def enrich_content(urls: List[str]) -> Dict[str, str]:
    """Website Content Crawler -> {url: text}. Only real http(s) pages; skips pseudo-urls.
    Checks the fetch cache first — only crawls URLs not seen within TTL days."""
    real = [u for u in urls if u.startswith("http")]
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
    """Cheap regex hints: which old-product claims appear + any prices. Feeds the judge and its
    offline fallback (it does NOT decide anything on its own)."""
    t = text.lower()
    flags = {}
    for claim, pats in CFG["claim_patterns"].items():
        flags[claim] = any(re.search(p, t) for p in pats)
    flags["current_signal"] = any(re.search(p, t) for p in CFG["current_signal_patterns"])
    m = re.findall(CFG["price_pattern"], text)
    flags["prices_seen"] = sorted(set(m))[:5]
    return flags

def _describes_old_product(flags: dict) -> bool:
    return any(flags.get(k) for k in ("iphone_only", "attach_to_phone", "camera_dependent", "no_android"))


JUDGE_SYSTEM = """You summarise what a web page says about the fertility-monitor brand Inito.

Context: Inito's CURRENT product is the "InSight Wireless Reader" — a standalone Wi-Fi reader with a built-in optical sensor (Spectral Mapping), works on iOS AND Android, no phone-camera/clip needed. The OLD product clipped onto an iPhone and used the phone's camera/Lightning port to read the strip; it was iPhone-only. Both products measure the same four hormones (estrogen/E3G, LH, PdG, FSH), use a companion app, and use a dip-the-strip workflow — those shared attributes are unremarkable.

Write `says_about_inito` as 1-2 plain sentences capturing what THIS page claims about Inito. If the page still presents the OLD phone-dependent product (iPhone-only, clips/attaches to phone, uses the phone camera, Lightning-port, no Android) as if it were current, say so explicitly in that sentence.

Also capture whether the page brings up competing products and, if so, what it says about them relative to Inito.

Use the classify_page tool."""

JUDGE_TOOL = {
    "name": "classify_page",
    "description": "Summarise what a web page says about Inito and its competitors.",
    "input_schema": {
        "type": "object",
        "required": ["says_about_inito", "mentions_competition", "competitors_named", "sentiment_inito"],
        "properties": {
            "says_about_inito": {"type": "string",
                "description": "1-2 sentences: what this page claims about Inito. Note explicitly if it describes the OLD phone-dependent product as current."},
            "mentions_competition": {"type": "boolean",
                "description": "True if the page names or discusses a competing product/brand"},
            "competition_summary": {"type": ["string", "null"],
                "description": "What the page says about competitors relative to Inito, or null"},
            "competitors_named": {"type": "array", "items": {"type": "string"},
                "description": "Competitor brand names mentioned (e.g. ['Mira','Proov'])"},
            "sentiment_inito": {"type": "number", "minimum": -1, "maximum": 1,
                "description": "Sentiment toward Inito: -1 very negative, 0 neutral, 1 very positive"},
            "price_mentioned": {"type": ["string", "null"],
                "description": "First Inito price string found, e.g. '$149', or null"},
        },
    },
}

def _judge_web_fallback(text: str, flags: dict) -> dict:
    comps = _competitors_in(text)
    if _describes_old_product(flags):
        says = "Describes the OLD phone-dependent Inito (iPhone-only / clips to phone / camera)."
    elif "inito" in (text or "").lower():
        says = "Mentions Inito."
    else:
        says = "No clear mention of Inito."
    return {"says_about_inito": says, "mentions_competition": bool(comps),
            "competition_summary": ("Names: " + ", ".join(comps)) if comps else "",
            "competitors_named": comps, "sentiment_inito": 0.0,
            "price_mentioned": (flags.get("prices_seen") or [None])[0], "_fallback": True}

def judge(url: str, text: str, regex_flags: dict) -> dict:
    excerpt = text[:8000] if text else ""
    user = (f"URL: {url}\nRegex hints: {json.dumps({k: v for k, v in regex_flags.items() if k != 'prices_seen'})}\n"
            f"Prices seen: {regex_flags.get('prices_seen')}\n\nPAGE TEXT:\n{excerpt}")
    try:
        resp = claude.messages.create(
            model=CFG["limits"]["judge_model"], max_tokens=400,
            system=JUDGE_SYSTEM, tools=[JUDGE_TOOL],
            tool_choice={"type": "tool", "name": "classify_page"},
            messages=[{"role": "user", "content": user}])
        tool_block = next((b for b in resp.content if b.type == "tool_use"), None)
        if tool_block:
            return tool_block.input
        raise ValueError("no tool_use block in response")
    except Exception as e:
        log(f"  judge fallback for {url}: {e}")
        return _judge_web_fallback(text, regex_flags)

def ownership(url: str) -> str:
    d = domain_of(url)
    if _host_matches(d, CFG["competitor_domains"]):
        return "competitor"
    if d in ("apps.apple.com", "play.google.com"):
        # app/play stores are owned only when it's Inito's own app id
        return "owned" if any(a in url for a in CFG["owned_app_ids"]) else "third_party"
    if _host_matches(d, CFG["owned_domains"]):   # suffix match -> blog./ng./preprod./staging. all owned
        return "owned"
    if d == "amazon.com" and "/dp/" in url:
        return "owned_marketplace"   # Inito's own ASINs; verify seller in practice
    return "third_party"


# ---------- stage 4: write the web snapshot sheet ----------
def write_web_sheet(rows: List[dict], out_dir: Path) -> None:
    df = pd.DataFrame(rows).reindex(columns=WEB_COLUMNS)
    df.to_csv(out_dir / "web_observations.csv", index=False)
    log(f"  wrote {len(df)} rows -> web_observations.csv")


# ---------- Track A orchestration ----------
def _safe_discover(fn: Callable, label: str) -> List[dict]:
    """Run a discovery function, logging any failure gracefully (never aborts the run)."""
    try:
        results = fn()
        log(f"  {label}: {len(results)} records")
        return results
    except Exception as e:
        log(f"  {label} FAILED (skipping): {type(e).__name__}: {e}")
        return []

def classify_web_record(r: dict, body: str) -> dict:
    """Build one web_observations row from a discovered record + its page text."""
    flags = detect_claims(body)
    v = judge(r["url"], body, flags) if body else _judge_web_fallback("", flags)
    price = v.get("price_mentioned") or (flags.get("prices_seen") or [None])[0]
    return {
        "source": r["platform"],
        "url": r["url"],
        "query": r.get("query", ""),
        "intent": r.get("intent", ""),
        "topic_id": r.get("topic_id", ""),
        "ownership": r.get("ownership") or ownership(r["url"]),
        "says_about_inito": v.get("says_about_inito", ""),
        "mentions_competition": bool(v.get("mentions_competition", False)),
        "competition_summary": v.get("competition_summary") or "",
        "competitors_named": "; ".join(v.get("competitors_named") or []),
        "sentiment": v.get("sentiment_inito", 0.0),
        "price": price,
        "links_on_source": ", ".join(extract_links(body, r["url"])),
        "nonprod_url": is_nonprod_owned(r["url"]),
        "title": r.get("title", ""),
    }

def refresh(sources: List[str], queries: List[dict], out_dir: Path):
    t0 = time.time()
    log(f"STAGE 1 discover  sources={sources}  queries={len(queries)}")
    fns = {}
    for s in sources:
        fns[s] = (lambda s=s: WEB_DISCOVERERS[s](queries)) if s in ("serp", "news") else WEB_DISCOVERERS[s]
    recs = []
    with ThreadPoolExecutor(max_workers=max(1, len(fns))) as ex:
        futures = {ex.submit(_safe_discover, fn, s): s for s, fn in fns.items()}
        for fut in as_completed(futures):
            recs.extend(fut.result())

    # keep best record per normalized url (lowest non-zero rank); preserve precomputed ownership
    best = {}
    for r in recs:
        u = normalize_url(r["url"])
        r["url"] = u
        keep = best.get(u)
        if keep is None or (r["rank"] and r["rank"] < (keep["rank"] or 999)):
            best[u] = r
    records = list(best.values())
    log(f"  {len(records)} unique URLs after dedupe")
    if not records:
        log("no records discovered — nothing to persist")
        _cleanup_empty(out_dir)
        return

    log("STAGE 2 enrich")
    text = enrich_content([r["url"] for r in records])

    log("STAGE 3 classify + write")
    rows = [classify_web_record(r, text.get(r["url"], "") or r.get("snippet", "")) for r in records]
    write_web_sheet(rows, out_dir)
    nonprod = [r["url"] for r in rows if r["nonprod_url"]]
    if nonprod:
        log(f"  ⚠ {len(nonprod)} non-production Inito URL(s) found (should not be public): {nonprod[:5]}")
    log(f"done in {time.time()-t0:.0f}s -> {out_dir}")


# ---------- Track B: LLM brand visibility (live-web assistants) ----------
LLM_JUDGE_SYSTEM = """You summarise what a live-web LLM assistant's response says about the fertility-monitor brand Inito.

Context: Inito's CURRENT product is the "InSight Wireless Reader" — standalone, Wi-Fi, works on iOS AND Android, no phone camera or clip. The OLD product clipped onto an iPhone and used the phone's camera; it was iPhone-only. The four hormones (E3G, LH, PdG, FSH), the companion app and the dip-strip workflow are common to both and unremarkable.

Write `says_about_inito` as 1-2 plain sentences capturing what the response claims about Inito — and note explicitly if it describes the OLD phone-dependent product as if current. Capture whether competitors come up and what is said about them relative to Inito.

Use the analyze_llm_response tool."""

LLM_JUDGE_TOOL = {
    "name": "analyze_llm_response",
    "description": "Summarise what an LLM response says about Inito and its competitors.",
    "input_schema": {
        "type": "object",
        "required": ["inito_mentioned", "inito_recommended", "says_about_inito",
                     "mentions_competition", "competitors_named", "sentiment_inito", "sources_cited"],
        "properties": {
            "inito_mentioned": {"type": "boolean",
                "description": "True if Inito is mentioned by name in the response"},
            "inito_rank": {"type": ["integer", "null"],
                "description": "1-based position where Inito first appears among recommended products, null if not mentioned"},
            "inito_recommended": {"type": "boolean",
                "description": "True if the response recommends or positively endorses Inito"},
            "says_about_inito": {"type": "string",
                "description": "1-2 sentences: what the response claims about Inito. Note if it describes the OLD phone-dependent product."},
            "mentions_competition": {"type": "boolean",
                "description": "True if the response names or discusses a competing product/brand"},
            "competition_summary": {"type": ["string", "null"],
                "description": "What the response says about competitors relative to Inito, or null"},
            "competitors_named": {"type": "array", "items": {"type": "string"},
                "description": "Competitor brand names mentioned (e.g. ['Mira','Kegg','Clearblue'])"},
            "sentiment_inito": {"type": "number", "minimum": -1, "maximum": 1,
                "description": "Sentiment toward Inito: -1 very negative, 0 neutral, 1 very positive"},
            "price_mentioned": {"type": ["string", "null"],
                "description": "First Inito price string in the response, e.g. '$149', or null"},
            "sources_cited": {"type": "array", "items": {"type": "string"},
                "description": "All URLs/domains cited as sources in the response"},
        },
    },
}

def judge_llm_response(prompt: str, surface: str, response_text: str) -> dict:
    """Summarise a live-web assistant response for what it says about Inito + competition."""
    user = f"PROMPT ASKED: {prompt}\nSURFACE: {surface}\n\nRESPONSE:\n{response_text[:8000]}"
    try:
        resp = claude.messages.create(
            model=CFG["limits"]["judge_model"], max_tokens=500,
            system=LLM_JUDGE_SYSTEM, tools=[LLM_JUDGE_TOOL],
            tool_choice={"type": "tool", "name": "analyze_llm_response"},
            messages=[{"role": "user", "content": user}])
        tool_block = next((b for b in resp.content if b.type == "tool_use"), None)
        if tool_block:
            return tool_block.input
        raise ValueError("no tool_use block")
    except Exception as e:
        log(f"  llm_judge fallback for {surface}/{prompt[:40]}: {e}")
        comps = _competitors_in(response_text)
        old = _describes_old_product(detect_claims(response_text))
        return {
            "inito_mentioned": "inito" in response_text.lower(), "inito_rank": None,
            "inito_recommended": False,
            "says_about_inito": ("Describes the OLD phone-dependent Inito." if old
                                 else "Mentions Inito." if "inito" in response_text.lower()
                                 else "No clear mention of Inito."),
            "mentions_competition": bool(comps),
            "competition_summary": ("Names: " + ", ".join(comps)) if comps else "",
            "competitors_named": comps, "sentiment_inito": 0.0, "price_mentioned": None,
            "sources_cited": _extract_urls(response_text)[:10], "_fallback": True,
        }


def _blank_llm_row(run_idx, surface, prompt, intent, topic_id, status, note) -> dict:
    """A row for a response we never judge (empty / actor error) — visible in the sheet."""
    return {"surface": surface, "run": run_idx, "prompt": prompt, "intent": intent,
            "topic_id": topic_id, "mentioned": None, "rank": None, "recommended": None,
            "says_about_inito": "", "mentions_competition": None, "competition_summary": "",
            "competitors_named": "", "sentiment": None, "price": None, "sources_cited": "",
            "nonprod_url": False, "response_text": "", "status": status, "error_note": note}


def _llm_row(run_idx, surface, prompt, intent, response_text, extra_sources=None, topic_id="") -> dict:
    """Judge one assistant response and build a visibility row. An empty/blank response is NOT
    judged — it means the actor returned nothing, and judging it fabricates signals."""
    if not (response_text or "").strip():
        return _blank_llm_row(run_idx, surface, prompt, intent, topic_id, "empty",
                              "actor returned no answer text (navigation/anti-bot failure)")
    v = judge_llm_response(prompt, surface, response_text)
    sources, seen = [], set()
    for s in (v.get("sources_cited") or []) + (extra_sources or []) + _extract_urls(response_text):
        if not (s and str(s).startswith("http")):
            continue
        n = normalize_url(s)
        if n not in seen:
            seen.add(n); sources.append(n)
    sources = sources[:15]
    return {
        "surface": surface, "run": run_idx, "prompt": prompt, "intent": intent, "topic_id": topic_id,
        "mentioned": bool(v.get("inito_mentioned", False)),
        "rank": v.get("inito_rank"),
        "recommended": bool(v.get("inito_recommended", False)),
        "says_about_inito": v.get("says_about_inito", ""),
        "mentions_competition": bool(v.get("mentions_competition", False)),
        "competition_summary": v.get("competition_summary") or "",
        "competitors_named": "; ".join(v.get("competitors_named") or []),
        "sentiment": v.get("sentiment_inito", 0.0),
        "price": v.get("price_mentioned"),
        "sources_cited": ", ".join(sources),
        "nonprod_url": any(is_nonprod_owned(s) for s in sources),
        "response_text": response_text,
        "status": "ok", "error_note": "",
    }


def _error_rows(run_idx, surface, prompts_cfg, err: str) -> List[dict]:
    """One visible error row per prompt so a surface failure shows up in the sheet."""
    note = str(err)[:300]
    return [_blank_llm_row(run_idx, surface, p["prompt"], p["intent"], p.get("id", ""), "error", note)
            for p in prompts_cfg]


def _run_chatgpt(run_idx: int, prompts_cfg: List[dict]) -> List[dict]:
    """tri_angle/gpt-search — live ChatGPT search. country pins US; this run draws a fresh US session."""
    intent_map = {p["prompt"]: p["intent"] for p in prompts_cfg}
    id_map = {p["prompt"]: p.get("id", "") for p in prompts_cfg}
    run_input = {"prompts": [p["prompt"] for p in prompts_cfg],
                 "country": CFG.get("proxy_country", "US")}
    try:
        items = run_actor(CFG["actors"]["chatgpt"], run_input, f"chatgpt/run{run_idx}")
    except Exception as e:
        return _error_rows(run_idx, "chatgpt", prompts_cfg, e)
    rows = []
    for it in items:
        prompt = it.get("prompt") or ""
        if not prompt:
            continue
        response = it.get("response") or ""
        cites = [c.get("url", "") for c in (it.get("citations") or []) if c.get("url")]
        rows.append(_llm_row(run_idx, "chatgpt", prompt, intent_map.get(prompt, "unknown"),
                             response, extra_sources=cites, topic_id=id_map.get(prompt, "")))
    return rows


def _run_perplexity(run_idx: int, prompts_cfg: List[dict]) -> List[dict]:
    """Perplexity via the sonar API (direct, no Apify). Live web search + citations. No proxy/IP
    control here; the samples capture model variance."""
    if not PPLX_KEY:
        return _error_rows(run_idx, "perplexity", prompts_cfg,
                           "PERPLEXITY_API_KEY not set — add it to .env to enable the Perplexity surface")
    model = CFG["limits"].get("perplexity_model", "sonar")
    rows = []
    for p in prompts_cfg:
        try:
            answer, sources = perplexity_complete(p["prompt"], model)
            rows.append(_llm_row(run_idx, "perplexity", p["prompt"], p["intent"],
                                 answer, extra_sources=sources, topic_id=p.get("id", "")))
        except Exception as e:
            rows.append(_error_rows(run_idx, "perplexity", [p], e)[0])  # fail-fast, per-prompt
    return rows


SURFACE_RUNNERS: Dict[str, Callable] = {
    "chatgpt": _run_chatgpt,
    "perplexity": _run_perplexity,
}


def discover_llm_visibility(surfaces: List[str], prompts_cfg: List[dict], num_runs: int) -> List[dict]:
    """Run each surface × run in parallel; each call samples all prompts independently.
    No cross-run resume — every run is a fresh, self-contained snapshot."""
    jobs = [(s, r) for s in surfaces for r in range(1, num_runs + 1)]

    def _one(surface, run_idx):
        runner = SURFACE_RUNNERS.get(surface)
        if not runner:
            return _error_rows(run_idx, surface, prompts_cfg, f"unknown surface '{surface}'")
        log(f"  LLM visibility: {surface} run {run_idx}/{num_runs} ({len(prompts_cfg)} prompts)")
        return runner(run_idx, prompts_cfg)

    rows = []
    with ThreadPoolExecutor(max_workers=min(max(1, len(jobs)), 10)) as ex:
        futures = {ex.submit(_one, s, r): (s, r) for s, r in jobs}
        for fut in as_completed(futures):
            s, r = futures[fut]
            try:
                rows.extend(fut.result())
            except Exception as e:
                log(f"  {s}/run{r} unexpected thread error: {e}")
                rows.extend(_error_rows(r, s, prompts_cfg, e))
    log(f"  LLM visibility: {len(rows)} observations ({num_runs} run(s) × {len(surfaces)} surface(s))")
    return rows


def write_llm_sheet(rows: List[dict], out_dir: Path) -> None:
    df = pd.DataFrame(rows).reindex(columns=LLM_COLUMNS)
    df = df.sort_values(["surface", "prompt", "run"], kind="stable")
    df.to_csv(out_dir / "llm_observations.csv", index=False)
    log(f"  wrote {len(df)} rows -> llm_observations.csv")


def run_llm_visibility(surfaces: List[str], prompts_cfg: List[dict], num_runs: int, out_dir: Path):
    t0 = time.time()
    log(f"LLM VISIBILITY  surfaces={surfaces}  prompts={len(prompts_cfg)}  runs={num_runs}")
    rows = discover_llm_visibility(surfaces, prompts_cfg, num_runs)
    if not rows:
        log("no LLM visibility rows — nothing to do")
        _cleanup_empty(out_dir)
        return
    write_llm_sheet(rows, out_dir)
    nonprod = sorted({s for r in rows if r.get("nonprod_url") for s in r["sources_cited"].split(", ") if s})
    if nonprod:
        log(f"  ⚠ non-production Inito URL(s) cited (should not be public): {nonprod[:5]}")
    log(f"LLM visibility done in {time.time()-t0:.0f}s -> {out_dir}")


# ---------- run folders + interactive selection ----------
def _slug(text: str, n: int = 24) -> str:
    return re.sub(r"[^0-9a-zA-Z]+", "-", (text or "").strip()).strip("-").lower()[:n]

def run_dir_name(ts: str, track: str, parts: List[str], n_items: int,
                 num_runs: Optional[int] = None, note: str = "") -> str:
    """Descriptive, filesystem-safe run-folder name (pure — testable)."""
    bits = [ts, track, "+".join(parts)[:40], f"{n_items}items"]
    if num_runs:
        bits.append(f"{num_runs}runs")
    if note:
        bits.append(_slug(note))
    return "__".join(bits)

def make_run_dir(track: str, parts: List[str], items: list,
                 num_runs: Optional[int] = None, note: str = "") -> Path:
    ts = dt.datetime.now().strftime("%Y-%m-%dT%H%M%S")
    d = DATA / run_dir_name(ts, track, parts, len(items), num_runs, note)
    d.mkdir(parents=True, exist_ok=True)
    return d

def _cleanup_empty(out_dir: Path) -> None:
    """Remove a run folder that ended up empty."""
    try:
        d = Path(out_dir)
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    except Exception:
        pass


def parse_extra_prompts(spec: Optional[str]) -> List[dict]:
    """Parse ad-hoc one-off prompts (not in config) into {'prompt', 'intent'} dicts.
    Format: ';'-separated entries, each optionally 'text::intent' (intent defaults to 'adhoc')."""
    if not spec or not spec.strip():
        return []
    out, seen = [], set()
    for entry in spec.split(";"):
        e = entry.strip()
        if not e:
            continue
        text, _, intent = e.partition("::")
        text, intent = text.strip(), intent.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append({"prompt": text, "intent": intent or "adhoc"})
    return out


def resolve_selection(items: list, spec: Optional[str], label_fn: Callable) -> list:
    """Filter `items` by a spec string: None/'all'/'*' -> all; else comma list of 1-based
    indices and/or (substring) name matches. Raises ValueError on an unmatched token."""
    if spec is None or spec.strip().lower() in ("", "all", "*"):
        return list(items)
    chosen = []
    for tok in spec.split(","):
        t = tok.strip()
        if not t:
            continue
        if t.isdigit():
            i = int(t) - 1
            if not (0 <= i < len(items)):
                raise ValueError(f"selection index out of range: {t}")
            chosen.append(items[i])
        else:
            matches = [it for it in items if t.lower() in label_fn(it).lower()]
            if not matches:
                raise ValueError(f"no match for selection: {t!r}")
            chosen.extend(matches)
    seen, out = set(), []
    for it in chosen:
        key = label_fn(it)
        if key not in seen:
            seen.add(key); out.append(it)
    return out


def prompt_select(items: list, label_fn: Callable, title: str, spec: Optional[str],
                  assume_all: bool) -> list:
    """Resolve a selection from an explicit spec, else interactively (multiple choice), else all."""
    if spec is not None:
        return resolve_selection(items, spec, label_fn)
    if assume_all or not sys.stdin.isatty():
        return list(items)
    print(f"\n{title}")
    for i, it in enumerate(items, 1):
        print(f"  {i:>2}. {label_fn(it)}")
    raw = input("Select (comma-separated numbers/names, blank = all): ").strip()
    return resolve_selection(items, raw or None, label_fn)


def list_topics() -> None:
    """Print the editable topic catalog (config.topics). One query per topic, used by BOTH tracks."""
    print(f"\n{len(CFG['topics'])} topics (one query per topic, sent to both tracks — edit config.json freely):\n")
    for i, t in enumerate(CFG["topics"], 1):
        print(f"  {i:>2}. [{t['id']:<16}] ({t['intent']:<14}) {t.get('query','')!r}")


# ---------- CLI ----------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Inito GEO monitor (snapshot, CSV-only)")
    ap.add_argument("--refresh", action="store_true", help="Track A: web/SERP/news/ads/reddit")
    ap.add_argument("--llm", action="store_true", help="Track B: live-web LLM assistants")
    ap.add_argument("--list-topics", action="store_true", help="print the topic catalog and exit")
    ap.add_argument("--sources", help="Track A sources (e.g. 'serp,reddit' or 'all')")
    ap.add_argument("--queries", help="Track A queries (indices/names, or 'all')")
    ap.add_argument("--surfaces", help="Track B surfaces (e.g. 'chatgpt' or 'all')")
    ap.add_argument("--prompts", help="Track B prompts (indices/names, or 'all')")
    ap.add_argument("--num-runs", type=int, help="samples per (prompt × surface); default config")
    ap.add_argument("--extra-prompts", help="Track B ad-hoc one-off prompts NOT in config; "
                    "';'-separated, each optionally 'text::intent' (default intent 'adhoc').")
    ap.add_argument("--note", default="", help="short note added to the run-folder name")
    ap.add_argument("-y", "--yes", action="store_true", help="non-interactive: use specs/all, no prompts")
    a = ap.parse_args(argv)

    if a.list_topics:
        list_topics(); return
    if a.llm:
        surfaces = prompt_select(CFG["llm_surfaces"], lambda s: s, "Surfaces:", a.surfaces, a.yes)
        prompts = prompt_select(llm_topics(), lambda p: p["prompt"], "Prompts:", a.prompts, a.yes)
        chosen = {p["prompt"] for p in prompts}
        extra = [p for p in parse_extra_prompts(a.extra_prompts) if p["prompt"] not in chosen]
        if extra:
            log(f"  + {len(extra)} ad-hoc prompt(s) (one-off, not saved to config): "
                + ", ".join(p["prompt"] for p in extra))
            prompts = prompts + extra
        num_runs = a.num_runs or CFG.get("llm_num_runs", 5)
        if not surfaces or not prompts:
            sys.exit("nothing selected")
        out_dir = make_run_dir("llm", surfaces, prompts, num_runs, a.note)
        run_llm_visibility(surfaces, prompts, num_runs, out_dir)
        return
    if a.refresh:
        sources = prompt_select(WEB_SOURCES, lambda s: s, "Sources:", a.sources, a.yes)
        queries = prompt_select(web_topics(), lambda q: q["q"], "Queries:", a.queries, a.yes)
        if not sources or not queries:
            sys.exit("nothing selected")
        out_dir = make_run_dir("web", sources, queries, note=a.note)
        refresh(sources, queries, out_dir)
        return
    ap.print_help()


if __name__ == "__main__":
    main()
