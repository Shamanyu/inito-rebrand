#!/usr/bin/env python3
"""
Inito GEO monitor — Apify-backed stale-source pipeline (CSV-only outputs).

Two independent tracks, both driven from the CLI:

  Track A — Web/SERP stale-claim detection
    discover (Google SERP + News + Ads + Reddit) -> enrich (page text)
    -> classify (regex + Claude judge) -> persist (CSV) -> diff (metrics vs previous run)

  Track B — LLM brand visibility (live-web assistants only)
    discover (ChatGPT + Perplexity web actors, 3 samples per prompt from distinct US IPs)
    -> classify (Claude judge) -> persist (CSV) -> metrics (Wilson/mean CIs)

Every run writes a self-contained, descriptively-named folder under data/.

On-demand:
    python pipeline.py --refresh                 # Track A (interactive source/query select)
    python pipeline.py --llm                      # Track B (interactive surface/prompt select)
    python pipeline.py --llm --surfaces chatgpt --prompts 1,7 --num-runs 1   # scripted
    python pipeline.py --diff-only                # recompute metrics + diff, no crawling

Env (.env):  APIFY_TOKEN, ANTHROPIC_API_KEY
"""

import argparse, json, os, re, shutil, sys, time, datetime as dt, urllib.request
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

# Web/SERP platforms whose pages we crawl + classify for stale claims.
_WEB_PLATFORMS = {"web", "news", "ads", "reddit"}
# Track A discovery sources selectable from the CLI.
WEB_SOURCES = ["serp", "news", "ads", "reddit"]


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
    """Call an actor, return its dataset items. Fail-fast — no sleep between retries."""
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

def _to_bool(series: pd.Series) -> pd.Series:
    """Coerce a CSV-roundtripped column to real booleans (strings like 'True' -> True)."""
    return series.apply(lambda v: str(v).strip().lower() in ("true", "1"))

def _extract_urls(text: str) -> List[str]:
    return re.findall(r'https?://[^\s\)\]\"\'>,]+', text or "")

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
    Captures organic results, AI Overview, and ChatGPT/Perplexity SERP panels.
    """
    qcfg = queries or CFG["queries"]
    qmap = {c["q"]: c["intent"] for c in qcfg}
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
        # Google AI Overview (Gemini-powered) — passive capture, stored as a pseudo-URL
        ai = item.get("aiOverview") or item.get("aiOverviewText")
        if ai:
            out.append({"url": f"aioverview::{q}", "platform": "ai_overview", "query": q,
                        "intent": intent, "rank": 0, "title": "Google AI Overview",
                        "snippet": ai if isinstance(ai, str) else json.dumps(ai)[:4000]})
        # ChatGPT / Perplexity SERP panels (appear on some Google plans)
        for cgpt in (item.get("chatGptSearchResults") or []):
            text = cgpt.get("text") or cgpt.get("answer") or ""
            if text:
                out.append({"url": f"chatgptsearch::{q}", "platform": "chatgpt_search", "query": q,
                            "intent": intent, "rank": 0, "title": "ChatGPT Search Panel",
                            "snippet": text[:4000]})
        for px in (item.get("perplexitySearchResults") or []):
            text = px.get("text") or px.get("answer") or ""
            if text:
                out.append({"url": f"perplexitysearch::{q}", "platform": "perplexity_search", "query": q,
                            "intent": intent, "rank": 0, "title": "Perplexity Search Panel",
                            "snippet": text[:4000]})
    return out


def discover_news(queries: Optional[List[dict]] = None) -> List[dict]:
    """Google News via the same SERP actor with tbm=nws — press/syndicated stale content."""
    qcfg = queries or CFG["queries"]
    qmap = {c["q"]: c["intent"] for c in qcfg}
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
                            "rank": rank, "title": r.get("title", ""),
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
    Matches our own ads reliably ('inito') and competitors by their domain label
    (miracare.com -> 'miracare'); otherwise falls through to URL-based ownership.
    NOTE: advertiser display names can differ from domains (e.g. 'Mira' vs miracare.com) —
    add a curated competitor-brand list here if false negatives matter."""
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
    Driven by config.ads_start_urls (one advertiser/domain URL each). Catches stale copy in
    Inito's own ads (high priority) and competitor ads framing against Inito.
    """
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
    """Returns {normalized_url: text} for URLs fetched within TTL."""
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
    t = text.lower()
    flags = {}
    for claim, pats in CFG["claim_patterns"].items():
        flags[claim] = any(re.search(p, t) for p in pats)
    flags["current_signal"] = any(re.search(p, t) for p in CFG["current_signal_patterns"])
    m = re.findall(CFG["price_pattern"], text)
    flags["prices_seen"] = sorted(set(m))[:5]
    return flags

JUDGE_SYSTEM = """You classify a web page's claims about the fertility brand Inito.

Context: Inito's CURRENT product is the "InSight Wireless Reader" — a standalone Wi-Fi reader with a built-in optical sensor (Spectral Mapping), works on iOS AND Android, no phone-camera/clip needed. The OLD product clipped onto an iPhone and used the phone's camera/Lightning port to read the strip; it was iPhone-only.

Both the old and new products measure the same four hormones (estrogen/E3G, LH, PdG, FSH) on one strip, use a companion phone app, and use a dip-the-strip workflow. These shared attributes are NOT stale — never classify a page stale just because it mentions the four hormones, the app, accuracy, or dipping a strip.

A page is STALE only if it presents the OLD phone-dependent product as current: iPhone-only, attaches/clips to the phone, uses the phone camera, Lightning/charging-port dependent, no Android, or requires specific iPhone models. MIXED if it has both old and new content. CURRENT if it correctly reflects the wireless reader.

KEY EDGE CASE: If a page quotes old specs in order to refute or correct them ("the old Inito clipped onto your iPhone but the new InSight is wireless and works on Android"), classify as MIXED, not STALE.

DISTINGUISH: "syncs results to your phone app" is NOT stale (true of both). "attaches to your phone / uses your phone's camera" IS stale.

EXAMPLES:
Page: "Inito is only compatible with iPhone. You attach the monitor to your phone and it uses your iPhone's camera."
→ status=stale, iphone_only=true, attach_to_phone=true, camera_dependent=true, confidence=0.95

Page: "Inito measures estrogen, LH, PdG and FSH and sends results to the Inito app on your phone."
→ status=current (no phone-dependence claimed; hormones+app are shared, not stale), confidence=0.85

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
            "status": {"type": "string", "enum": ["stale", "mixed", "current", "unknown"],
                       "description": "Overall staleness classification"},
            "current_product_named": {"type": "boolean",
                "description": "True if the page explicitly names or describes the InSight Wireless Reader"},
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
            "price_mentioned": {"type": ["string", "null"],
                "description": "First price string found, e.g. '$149', or null"},
            "sentiment_inito": {"type": "number", "minimum": -1, "maximum": 1,
                "description": "Sentiment toward Inito: -1 very negative, 0 neutral, 1 very positive"},
            "competitor_framing": {"type": "boolean",
                "description": "True if the page frames a competitor (e.g. Mira) as better than Inito"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1,
                "description": "Confidence in this classification. Low (<0.6) flags the row for human review."},
        },
    },
}

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
        cc = {k: regex_flags.get(k, False) for k in ("iphone_only", "attach_to_phone", "camera_dependent", "no_android")}
        any_stale = any(cc.values())
        has_current = regex_flags.get("current_signal", False)
        status = ("mixed" if (any_stale and has_current)
                  else "stale" if any_stale else "current")
        return {"status": status, "current_product_named": has_current,
                "claims_confirmed": cc, "price_mentioned": (regex_flags.get("prices_seen") or [None])[0],
                "sentiment_inito": 0.0, "competitor_framing": False, "confidence": 0.5, "_fallback": True}

def ownership(url: str) -> str:
    d = domain_of(url)
    if d in CFG["competitor_domains"]:
        return "competitor"
    if d in CFG["owned_domains"]:
        if d in ("apps.apple.com", "play.google.com"):
            # app/play stores are owned only when it's Inito's own app id
            return "owned" if any(a in url for a in CFG["owned_app_ids"]) else "third_party"
        return "owned"
    if d == "amazon.com" and "/dp/" in url:
        return "owned_marketplace"   # Inito's own ASINs; verify seller in practice
    return "third_party"


# ---------- stage 4: persist + diff (Track A) ----------
def persist(rows: List[dict], out_dir: Optional[Path] = None):
    out_dir = Path(out_dir) if out_dir else DATA
    df = pd.DataFrame(rows)
    df["run_date"] = RUN_DATE
    df.to_csv(out_dir / f"observations_{RUN_DATE}.csv", index=False)
    # latest snapshot (per-run) for Sheets import
    df.to_csv(out_dir / "latest_snapshot.csv", index=False)
    # rolling history at the DATA root (survives across runs, powers diff)
    hist = DATA / "observations_history.csv"
    if hist.exists():
        prev = pd.read_csv(hist)
        prev = prev[prev["run_date"].astype(str) != RUN_DATE]
        df_all = pd.concat([prev, df], ignore_index=True)
    else:
        df_all = df
    df_all.to_csv(hist, index=False)
    # low-confidence rows -> human review queue (per run)
    review = [r for r in rows if r.get("confidence", 1.0) < 0.6]
    if review:
        rq_new = pd.DataFrame(review)
        rq_new["flagged_date"] = RUN_DATE
        rq_new.to_csv(out_dir / "review_queue.csv", index=False)
        log(f"  {len(review)} low-confidence rows -> review_queue.csv")
    log(f"persisted {len(rows)} rows -> {out_dir.name}/observations_{RUN_DATE}.csv")
    return df_all


def _coerce_web(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in ("claim_iphone_only", "claim_attach_to_phone", "claim_camera_dependent",
              "claim_no_android", "competitor_framing", "current_product_named"):
        if c in df.columns:
            df[c] = _to_bool(df[c])
    for c in ("rank", "sentiment_inito", "confidence"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _kappa_regex_vs_judge(rows: List[dict]) -> float:
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


def _run_quality_score(metrics: dict, mdf: pd.DataFrame):
    """0-100 composite run quality score: coverage, judge confidence, kappa, stale trend."""
    def _safe(v, default=0.0):
        return default if (v != v) else float(v)
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
        scores["progress"] = 12.5
    total = round(sum(scores.values()), 1)
    return total, {k: round(v, 1) for k, v in scores.items()}


def compute_metrics(df_all: pd.DataFrame, current_rows=None, out_dir: Optional[Path] = None):
    df_all = _coerce_web(df_all)
    cur = df_all[df_all["run_date"].astype(str) == RUN_DATE]
    web = cur[cur["platform"].isin(_WEB_PLATFORMS)]
    def claim_count(c): return int(web["claim_" + c].fillna(False).sum())
    stale = web[web["status"].isin(["stale", "mixed"])]

    # kappa needs per-row regex-vs-judge labels; prefer the in-memory rows, else derive from the
    # persisted columns so --diff-only doesn't blank it out.
    kappa     = _kappa_regex_vs_judge(current_rows if current_rows is not None else web.to_dict("records"))
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
        "mean_sentiment":         round(float(web["sentiment_inito"].fillna(0).mean()), 3) if len(web) else 0.0,
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
    if out_dir and Path(out_dir) != DATA:
        mdf.to_csv(Path(out_dir) / "metrics.csv", index=False)
    log(f"run quality score: {quality_score}/100  breakdown: {quality_breakdown}")
    return metrics, mdf

def _sov(cur: pd.DataFrame) -> float:
    """Share of voice on category queries: fraction where an owned domain appears in top 10."""
    cat = cur[(cur["intent"] == "category") &
              (cur["platform"] == "web") &
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
            if av != av or bv != bv:
                continue
            d = bv - av
            arrow = "↓" if d < 0 else ("↑" if d > 0 else "·")
            log(f"  {k:28} {av:>7.3g} -> {bv:>7.3g}  {arrow}{abs(d):.3g}")
        except (TypeError, ValueError):
            continue


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

def refresh(sources: List[str], queries: List[dict], out_dir: Path):
    t0 = time.time()
    log(f"STAGE 1 discover  sources={sources}  queries={len(queries)}")
    # discovery sources run in parallel; serp/news take the selected query subset
    fns = {}
    for s in sources:
        if s in ("serp", "news"):
            fns[s] = (lambda s=s: WEB_DISCOVERERS[s](queries))
        else:
            fns[s] = WEB_DISCOVERERS[s]
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

    log("STAGE 3 classify")
    rows = []
    for r in records:
        body = text.get(r["url"], "") or r.get("snippet", "")
        flags = detect_claims(body)
        verdict = judge(r["url"], body, flags) if body else {
            "status": "unknown", "current_product_named": False, "claims_confirmed": {},
            "price_mentioned": None, "sentiment_inito": 0.0, "competitor_framing": False, "confidence": 0.0}
        cc = verdict.get("claims_confirmed", {})
        rows.append({
            "url": r["url"], "domain": domain_of(r["url"]), "platform": r["platform"],
            "query": r["query"], "intent": r["intent"], "rank": r["rank"],
            "advertiser": r.get("advertiser", ""),
            "ownership": r.get("ownership") or ownership(r["url"]),
            "status": verdict.get("status"),
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
    df_all = persist(rows, out_dir)
    metrics, mdf = compute_metrics(df_all, current_rows=rows, out_dir=out_dir)
    export_serp_csv(out_dir)
    safe_metrics = {k: (None if v != v else v) for k, v in metrics.items()}
    log(f"metrics: {json.dumps(safe_metrics, indent=2)}")
    print_diff(mdf)
    _finalize_run(out_dir, track="web",
                  selection={"sources": sources, "queries": [q["q"] for q in queries]},
                  cumulative=["observations_history.csv", "metrics.csv"])
    log(f"done in {time.time()-t0:.0f}s -> {out_dir}")


def diff_only():
    hist = DATA / "observations_history.csv"
    if not hist.exists():
        sys.exit("no history yet — run --refresh first")
    _, mdf = compute_metrics(pd.read_csv(hist))
    print_diff(mdf)


# ---------- Track B: LLM brand visibility (live-web assistants) ----------
LLM_JUDGE_SYSTEM = """You analyze a live-web LLM assistant's response about the fertility monitor brand Inito.
Extract structured signals about brand visibility, accuracy, and framing.

Context: Inito's CURRENT product is the "InSight Wireless Reader" — standalone, Wi-Fi, works on iOS AND Android, no phone camera or clip needed. The OLD product clipped onto an iPhone and used the phone's camera; it was iPhone-only. The four hormones (E3G, LH, PdG, FSH), the companion app and the dip-strip workflow are common to both and are NOT stale signals — only phone-dependence (iPhone-only, clip, camera, Lightning port, no Android) is stale.

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
            "inito_mentioned": {"type": "boolean",
                "description": "True if Inito is mentioned by name in the response"},
            "inito_rank": {"type": ["integer", "null"],
                "description": "1-based position where Inito first appears among recommended products, null if not mentioned"},
            "inito_recommended": {"type": "boolean",
                "description": "True if the response recommends or positively endorses Inito"},
            "stale_product_described": {"type": "boolean",
                "description": "True if the response describes Inito as iPhone-only, camera-based, or requiring a phone clip — the OLD product"},
            "stale_excerpt": {"type": ["string", "null"],
                "description": "The exact sentence/phrase containing the stale claim, verbatim, or null"},
            "sources_cited": {"type": "array", "items": {"type": "string"},
                "description": "All URLs/domains cited as sources in the response"},
            "sentiment_inito": {"type": "number", "minimum": -1, "maximum": 1,
                "description": "Sentiment toward Inito: -1 very negative, 0 neutral, 1 very positive"},
            "competitors_named": {"type": "array", "items": {"type": "string"},
                "description": "Competitor brand names mentioned (e.g. ['Mira','Kegg','Clearblue'])"},
            "competitor_preferred": {"type": ["string", "null"],
                "description": "The competitor the response most clearly prefers over Inito, or null"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1,
                "description": "Confidence in this analysis"},
        },
    },
}

def judge_llm_response(prompt: str, surface: str, response_text: str) -> dict:
    """Classify a live-web assistant response for Inito brand visibility signals."""
    user = f"PROMPT ASKED: {prompt}\nSURFACE: {surface}\n\nRESPONSE:\n{response_text[:6000]}"
    try:
        resp = claude.messages.create(
            model=CFG["limits"]["judge_model"], max_tokens=400,
            system=LLM_JUDGE_SYSTEM, tools=[LLM_JUDGE_TOOL],
            tool_choice={"type": "tool", "name": "analyze_llm_response"},
            messages=[{"role": "user", "content": user}])
        tool_block = next((b for b in resp.content if b.type == "tool_use"), None)
        if tool_block:
            return tool_block.input
        raise ValueError("no tool_use block")
    except Exception as e:
        log(f"  llm_judge fallback for {surface}/{prompt[:40]}: {e}")
        return {
            "inito_mentioned": "inito" in response_text.lower(), "inito_rank": None,
            "inito_recommended": False, "stale_product_described": False,
            "stale_excerpt": None, "sources_cited": _extract_urls(response_text)[:10],
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


def _empty_row(run_idx, surface, prompt, intent, note: str) -> dict:
    """A response with no usable text (actor nav/anti-bot failure) — never judged, flagged in the sheet."""
    row = {"run_date": RUN_DATE, "run_index": run_idx, "surface": surface, "prompt": prompt,
           "intent": intent, "response_text": "", "inito_mentioned": None, "inito_rank": None,
           "inito_recommended": None, "stale_product_described": None, "stale_excerpt": None,
           "sources_cited": "[]", "sentiment_inito": None, "competitors_named": "[]",
           "competitor_preferred": None, "confidence": None, "status": "empty", "error_note": note}
    row["action"] = f"Empty response — {note} (no judgement; fix the actor/surface)"
    row["priority"] = 6
    return row


def _llm_row(run_idx, surface, prompt, intent, response_text, extra_sources=None,
             priors=None) -> dict:
    """Judge one assistant response and build a visibility row (with action + priority).
    An empty/blank response is NOT judged — it means the actor returned nothing (e.g. nav timeout),
    and judging it fabricates signals."""
    if not (response_text or "").strip():
        return _empty_row(run_idx, surface, prompt, intent,
                          "actor returned no answer text (navigation/anti-bot failure)")
    verdict = judge_llm_response(prompt, surface, response_text)
    priors = priors or {}
    judge_sources = verdict.get("sources_cited") or []
    inline = _extract_urls(response_text)
    all_sources = list(dict.fromkeys(judge_sources + (extra_sources or []) + inline))[:15]
    row = {
        "run_date": RUN_DATE, "run_index": run_idx, "surface": surface,
        "prompt": prompt, "intent": intent, "response_text": response_text[:4000],
        "inito_mentioned": verdict.get("inito_mentioned", priors.get("mentioned") or False),
        "inito_rank": verdict.get("inito_rank", priors.get("position")),
        "inito_recommended": verdict.get("inito_recommended", False),
        "stale_product_described": verdict.get("stale_product_described", False),
        "stale_excerpt": verdict.get("stale_excerpt"),
        "sources_cited": json.dumps(all_sources),
        "sentiment_inito": verdict.get("sentiment_inito", 0.0),
        "competitors_named": json.dumps(verdict.get("competitors_named", priors.get("competitors", []))),
        "competitor_preferred": verdict.get("competitor_preferred"),
        "confidence": verdict.get("confidence", 0.3),
        "status": "ok", "error_note": "",
    }
    act, prio = derive_action(row)
    row["action"], row["priority"] = act, prio
    return row


def _error_rows(run_idx, surface, prompts_cfg, err: str) -> List[dict]:
    """One visible error row per prompt so a surface failure shows up in the sheet."""
    note = str(err)[:300]
    out = []
    for p in prompts_cfg:
        row = {"run_date": RUN_DATE, "run_index": run_idx, "surface": surface,
               "prompt": p["prompt"], "intent": p["intent"], "response_text": None,
               "inito_mentioned": None, "inito_rank": None, "inito_recommended": None,
               "stale_product_described": None, "stale_excerpt": None, "sources_cited": "[]",
               "sentiment_inito": None, "competitors_named": "[]", "competitor_preferred": None,
               "confidence": None, "status": "error", "error_note": note}
        row["action"] = f"Actor failed — {note} (fail-fast; retried next run)"
        row["priority"] = 6
        out.append(row)
    return out


def _run_chatgpt(run_idx: int, prompts_cfg: List[dict]) -> List[dict]:
    """tri_angle/gpt-search — live ChatGPT search. country pins US; this run draws a fresh US session."""
    intent_map = {p["prompt"]: p["intent"] for p in prompts_cfg}
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
                             response, extra_sources=cites))
    return rows


def _run_perplexity(run_idx: int, prompts_cfg: List[dict]) -> List[dict]:
    """Perplexity via the sonar API (direct, no Apify). Live web search + citations. Web-interface
    scrapers (zhorex etc.) are anti-bot-walled, so the API — which the product itself runs on — is the
    reliable equivalent. No proxy/IP control here; the 3 samples capture model variance, not IP variance."""
    if not PPLX_KEY:
        return _error_rows(run_idx, "perplexity", prompts_cfg,
                           "PERPLEXITY_API_KEY not set — add it to .env to enable the Perplexity surface")
    model = CFG["limits"].get("perplexity_model", "sonar")
    rows = []
    for p in prompts_cfg:
        try:
            answer, sources = perplexity_complete(p["prompt"], model)
            rows.append(_llm_row(run_idx, "perplexity", p["prompt"], p["intent"],
                                 answer, extra_sources=sources))
        except Exception as e:
            rows.append(_error_rows(run_idx, "perplexity", [p], e)[0])  # fail-fast, per-prompt
    return rows


SURFACE_RUNNERS: Dict[str, Callable] = {
    "chatgpt": _run_chatgpt,
    "perplexity": _run_perplexity,
}


def _json_urls(val) -> List[str]:
    try:
        return [s for s in json.loads(val or "[]") if isinstance(s, str) and s.startswith("http")]
    except Exception:
        return []


def derive_action(row: dict):
    """One prioritized, source-targeted action for a visibility row. Returns (action, priority).

    Attribution is **quote-grounded**: a stale claim is blamed on a source only if that source was
    verified to actually contain stale content (`verified_stale_sources`, set by
    verify_stale_attribution). We never tell the team to "fix our own page" just because the brand
    site appears in the citation list — that was the misattribution bug."""
    if row.get("status") == "error":
        return "Actor failed — see error_note (fail-fast)", 6
    cited = _json_urls(row.get("sources_cited"))
    verified = _json_urls(row.get("verified_stale_sources"))
    competitor_cited = [s for s in cited if ownership(s) == "competitor"]
    high_intent = row.get("intent") in ("comparison", "purchase", "brand_entity")

    if row.get("stale_product_described"):
        v_owned = [s for s in verified if ownership(s) in ("owned", "owned_marketplace")]
        v_comp = [s for s in verified if ownership(s) == "competitor"]
        v_third = [s for s in verified if ownership(s) == "third_party"]
        if v_owned:
            return f"Fix our own page now (verified stale): {v_owned[0]}", 1
        if v_third:
            return f"Outreach to publisher to correct (verified stale source): {v_third[0]}", 2
        if v_comp:
            return f"Competitor source carries the stale claim — counter-content / correction: {v_comp[0]}", 2
        # stale in the answer but NOT found in any cited source → likely model training data, not a page we can fix
        return "Stale claim in the answer but not in any cited source — likely model training data; submit provider feedback", 3
    if not row.get("inito_mentioned"):
        return ("Not visible — create/optimize content ranking for this prompt's terms",
                3 if high_intent else 4)
    cp = row.get("competitor_preferred")
    if cp and not row.get("inito_recommended"):
        src = f" (cited: {competitor_cited[0]})" if competitor_cited else ""
        return f"Competitor '{cp}' preferred{src} — build comparison content to outrank", 3
    if row.get("inito_recommended"):
        return "Positive — monitor to ensure this holds", 5
    return "Neutral mention — strengthen positioning content", 4


def _text_is_stale(txt: str) -> bool:
    f = detect_claims(txt or "")
    return any(f.get(k) for k in ("iphone_only", "attach_to_phone", "camera_dependent", "no_android"))


def verify_stale_attribution(rows: List[dict], fetch_missing: bool = True) -> None:
    """Quote-ground the stale attribution: for each stale row, mark which of its cited sources
    *actually contain* stale content — so we never blame a clean page (the misattribution bug).

    A cited source is a verified stale source if it's (1) already judged stale/mixed in the Track A web
    history, (2) already in the page-text cache and trips the claim regex, or (3) fetched now and trips
    it. Steps 1–2 are FREE (no parsing). `fetch_missing=False` (re-eval mode) skips step 3 entirely, so
    re-evaluating stored data spends zero parsing tokens — uncached sources just stay unverified.
    Sets `row['verified_stale_sources']` and re-derives the action."""
    for r in rows:
        r.setdefault("verified_stale_sources", "[]")
    stale_rows = [r for r in rows if r.get("status") == "ok" and r.get("stale_product_described")]
    if not stale_rows:
        return

    # (1) free: sources already judged stale/mixed in Track A history
    verified_norm = set()
    hist = DATA / "observations_history.csv"
    if hist.exists():
        try:
            df = pd.read_csv(hist)
            verified_norm = {normalize_url(u) for u in
                             df[df["status"].astype(str).isin(["stale", "mixed"])]["url"].astype(str)}
        except Exception:
            pass

    cited_norm = {normalize_url(s) for r in stale_rows for s in _json_urls(r.get("sources_cited"))}

    # (2) free: sources already in the page-text cache that trip the claim regex
    try:
        cache = load_fetch_cache()
    except Exception:
        cache = {}
    for nu in cited_norm - verified_norm:
        if nu in cache and _text_is_stale(cache[nu]):
            verified_norm.add(nu)

    # (3) optional spend: fetch the rest (skipped in re-eval mode)
    if fetch_missing:
        to_fetch = [nu for nu in (cited_norm - verified_norm) if nu.startswith("http")]
        to_fetch = to_fetch[:CFG["limits"].get("verify_max_fetch", 40)]
        if to_fetch:
            log(f"  verify attribution: fetching {len(to_fetch)} cited source(s) to confirm stale claims")
            for nu, txt in enrich_content(to_fetch).items():
                if _text_is_stale(txt):
                    verified_norm.add(nu)

    for r in stale_rows:
        r["verified_stale_sources"] = json.dumps(
            [s for s in _json_urls(r.get("sources_cited")) if normalize_url(s) in verified_norm])

    # re-derive actions now that attribution is verified
    for r in rows:
        if r.get("status") == "ok":
            r["action"], r["priority"] = derive_action(r)


def _coerce_llm(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in ("inito_mentioned", "inito_recommended", "stale_product_described"):
        if c in df.columns:
            df[c] = _to_bool(df[c])
    for c in ("sentiment_inito", "inito_rank"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _completed_combos() -> set:
    """Resume at (surface, run_index, prompt) granularity — so a partial run (one prompt) doesn't
    mark the whole (surface, run) done and skip the rest. Counts real data only, not error rows."""
    hist = DATA / "llm_visibility_history.csv"
    if not hist.exists():
        return set()
    try:
        prev = pd.read_csv(hist)
        today = prev[prev["run_date"].astype(str) == RUN_DATE]
        if "inito_mentioned" in today.columns:
            today = today[today["inito_mentioned"].notna()]
        return {(str(r["surface"]), int(r["run_index"]), str(r["prompt"])) for _, r in today.iterrows()}
    except Exception as exc:
        log(f"  resume check failed (will re-run all): {exc}")
        return set()


def discover_llm_visibility(surfaces: List[str], prompts_cfg: List[dict], num_runs: int) -> List[dict]:
    """Run each surface × prompt × run in parallel. Each (surface, run) is one runner call; runs are
    separate so they sample independently. Resume skips per-(surface, run, prompt) already done today."""
    completed = _completed_combos()
    if completed:
        log(f"  resume: {len(completed)} (surface, run, prompt) already done today — skipping those")
    # per (surface, run): only the prompts not yet completed
    jobs = []
    for s in surfaces:
        for r in range(1, num_runs + 1):
            todo = [p for p in prompts_cfg if (s, r, p["prompt"]) not in completed]
            if todo:
                jobs.append((s, r, todo))

    def _one(surface, run_idx, todo):
        runner = SURFACE_RUNNERS.get(surface)
        if not runner:
            return _error_rows(run_idx, surface, todo, f"unknown surface '{surface}'")
        log(f"  LLM visibility: {surface} run {run_idx}/{num_runs} ({len(todo)} prompts)")
        return runner(run_idx, todo)

    rows = []
    if not jobs:
        return rows
    with ThreadPoolExecutor(max_workers=min(len(jobs), 10)) as ex:
        futures = {ex.submit(_one, s, r, todo): (s, r, todo) for s, r, todo in jobs}
        for fut in as_completed(futures):
            s, r, todo = futures[fut]
            try:
                rows.extend(fut.result())
            except Exception as e:
                log(f"  {s}/run{r} unexpected thread error: {e}")
                rows.extend(_error_rows(r, s, todo, e))
    log(f"  LLM visibility: {len(rows)} observations ({num_runs} run(s) × {len(surfaces)} surface(s))")
    return rows


def _sources_to_plain(sources_json: str) -> str:
    """JSON array of source URLs -> comma-separated plain text (Sheets auto-links)."""
    try:
        urls = [s for s in json.loads(sources_json or "[]") if s and s.startswith("http")]
        return ", ".join(urls)
    except Exception:
        return ""


def export_llm_csv(rows: List[dict], out_dir: Path) -> None:
    """Sheets-friendly CSV: clickable sources, action, priority, error notes, clean column names."""
    out = []
    for r in rows:
        row_out = dict(r)
        row_out["sources_cited"] = _sources_to_plain(r.get("sources_cited", "[]"))
        out.append(row_out)
    df = pd.DataFrame(out)
    col_map = {
        "run_date": "date", "run_index": "run_#", "inito_mentioned": "mentioned",
        "inito_rank": "rank_in_response", "inito_recommended": "recommended",
        "stale_product_described": "stale_claim", "stale_excerpt": "stale_quote",
        "sources_cited": "sources (clickable URLs)", "sentiment_inito": "sentiment (-1 to +1)",
        "competitors_named": "competitors_mentioned", "verified_stale_sources": "verified_stale_source",
    }
    df = df.rename(columns=col_map)
    ordered = ["date", "surface", "prompt", "intent", "priority", "action",
               "mentioned", "rank_in_response", "recommended", "sentiment (-1 to +1)",
               "stale_claim", "stale_quote", "verified_stale_source",
               "competitors_mentioned", "competitor_preferred", "sources (clickable URLs)",
               "response_text", "run_#", "status", "error_note", "confidence"]
    ordered = [c for c in ordered if c in df.columns]
    df = df[ordered + [c for c in df.columns if c not in ordered]]
    if "priority" in df.columns:
        df = df.sort_values("priority", kind="stable")
    if "response_text" in df.columns:
        df["response_text"] = df["response_text"].fillna("").astype(str).str[:800]
    df.to_csv(out_dir / "llm_visibility_latest.csv", index=False)
    log(f"  exported {len(df)} rows -> llm_visibility_latest.csv")


def export_serp_csv(out_dir: Path) -> None:
    """AI Overviews + top-5 organic from this run's snapshot -> serp_latest.csv."""
    snap = out_dir / "latest_snapshot.csv"
    if not snap.exists():
        return
    df = pd.read_csv(snap)
    ai = df[df["url"].astype(str).str.startswith("aioverview::")].copy()
    ai["query"] = ai["url"].astype(str).str.removeprefix("aioverview::")
    ai_cols = [c for c in ["query", "intent", "snippet", "status", "sentiment_inito"] if c in ai.columns]
    ai_out = ai[ai_cols].rename(columns={"snippet": "ai_overview_text", "status": "inito_status",
                                          "sentiment_inito": "sentiment_score"})
    ai_out.insert(0, "source", "Google AI Overview")
    org = df[df["platform"].isin(["web", "news"]) & df["url"].notna()].copy()
    org = org[pd.to_numeric(org["rank"], errors="coerce").between(1, 5)]
    org_cols = [c for c in ["rank", "url", "title", "query", "intent", "platform",
                            "ownership", "status", "sentiment_inito"] if c in org.columns]
    org_out = org[org_cols].copy()
    org_out.insert(0, "source", "Google Organic")
    pd.concat([ai_out, org_out], ignore_index=True).to_csv(out_dir / "serp_latest.csv", index=False)
    log(f"  exported {len(ai_out)} AI overviews + {len(org_out)} top-5 organic -> serp_latest.csv")


def persist_llm(rows: List[dict], out_dir: Optional[Path] = None) -> pd.DataFrame:
    out_dir = Path(out_dir) if out_dir else DATA
    df = pd.DataFrame(rows)
    df.to_csv(out_dir / f"llm_visibility_{RUN_DATE}.csv", index=False)
    hist = DATA / "llm_visibility_history.csv"
    if hist.exists():
        prev = pd.read_csv(hist)
        prev = prev[prev["run_date"].astype(str) != RUN_DATE]
        df_all = pd.concat([prev, df], ignore_index=True)
    else:
        df_all = df
    df_all.to_csv(hist, index=False)
    export_llm_csv(rows, out_dir)
    log(f"persisted {len(rows)} LLM visibility rows -> {out_dir.name}/llm_visibility_{RUN_DATE}.csv")
    return df_all


def compute_llm_metrics(df_all: pd.DataFrame, out_dir: Optional[Path] = None) -> dict:
    """Point estimates + 95% CIs pooled across runs. Per-prompt/surface drill-down + aggregates."""
    df_all = _coerce_llm(df_all)
    cur = df_all[df_all["run_date"].astype(str) == RUN_DATE]
    if cur.empty:
        return {}
    num_runs = int(cur["run_index"].nunique()) if "run_index" in cur.columns else 1
    log(f"  computing LLM metrics over {len(cur)} observations ({num_runs} run(s))")

    def _prop(series, prefix):
        k = int(series.fillna(False).sum()); n = int(series.notna().sum())
        p, lo, hi = _wilson_ci(k, n)
        return {prefix: p, f"{prefix}_lo": lo, f"{prefix}_hi": hi, f"{prefix}_n": n}

    def _cont(series, prefix):
        mu, lo, hi = _mean_ci(series.dropna().tolist())
        return {prefix: mu, f"{prefix}_lo": lo, f"{prefix}_hi": hi}

    prompt_rows = []
    for (surface, prompt), grp in cur.groupby(["surface", "prompt"]):
        row = {"run_date": RUN_DATE, "surface": surface, "prompt": prompt,
               "intent": grp["intent"].iloc[0], "n_obs": len(grp)}
        row.update(_prop(grp["inito_mentioned"], "mention"))
        row.update(_prop(grp["inito_recommended"], "recommended"))
        row.update(_prop(grp["stale_product_described"], "stale"))
        row.update(_cont(grp["sentiment_inito"], "sentiment"))
        ranks = grp["inito_rank"].dropna().tolist()
        if ranks:
            mu, lo, hi = _mean_ci(ranks)
            row.update({"rank_mean": mu, "rank_lo": lo, "rank_hi": hi})
        prompt_rows.append(row)
    out_dir = Path(out_dir) if out_dir else DATA
    pd.DataFrame(prompt_rows).to_csv(out_dir / "llm_visibility_stats.csv", index=False)

    metrics = {"run_date": RUN_DATE, "llm_total_observations": int(len(cur)), "llm_num_runs": num_runs}
    for surface, grp in cur.groupby("surface"):
        safe = re.sub(r"[^0-9a-zA-Z]+", "_", surface)
        for fn, col in ((_prop, "inito_mentioned"), (_prop, "inito_recommended"),
                        (_prop, "stale_product_described"), (_cont, "sentiment_inito")):
            pref = {"inito_mentioned": "mention", "inito_recommended": "recommended",
                    "stale_product_described": "stale", "sentiment_inito": "sentiment"}[col]
            metrics.update({f"llm_{safe}_{k}": v for k, v in fn(grp[col], pref).items()})
    metrics.update({f"llm_{k}": v for k, v in _prop(cur["inito_mentioned"], "mention").items()})
    metrics.update({f"llm_{k}": v for k, v in _prop(cur["inito_recommended"], "recommended").items()})
    metrics.update({f"llm_{k}": v for k, v in _prop(cur["stale_product_described"], "stale").items()})
    metrics.update({f"llm_{k}": v for k, v in _cont(cur["sentiment_inito"], "sentiment").items()})

    mpath = DATA / "llm_metrics.csv"
    mdf = pd.read_csv(mpath) if mpath.exists() else pd.DataFrame()
    mdf = pd.concat([mdf[mdf.get("run_date", "") != RUN_DATE] if len(mdf) else mdf,
                     pd.DataFrame([metrics])], ignore_index=True)
    mdf.to_csv(mpath, index=False)
    if out_dir != DATA:
        mdf.to_csv(out_dir / "llm_metrics.csv", index=False)
    safe_m = {k: (None if isinstance(v, float) and v != v else v) for k, v in metrics.items()}
    log(f"LLM visibility metrics: {json.dumps(safe_m, indent=2)}")
    return metrics


def run_llm_visibility(surfaces: List[str], prompts_cfg: List[dict], num_runs: int, out_dir: Path):
    t0 = time.time()
    log(f"LLM VISIBILITY  surfaces={surfaces}  prompts={len(prompts_cfg)}  runs={num_runs}")
    rows = discover_llm_visibility(surfaces, prompts_cfg, num_runs)
    if not rows:
        log("no LLM visibility rows — nothing to do (all combos may already be done today)")
        _cleanup_empty(out_dir)
        return
    log("LLM VISIBILITY verify stale attribution")
    verify_stale_attribution(rows)
    log("LLM VISIBILITY persist")
    df_all = persist_llm(rows, out_dir)
    log("LLM VISIBILITY metrics")
    compute_llm_metrics(df_all, out_dir)
    _finalize_run(out_dir, track="llm",
                  selection={"surfaces": surfaces, "prompts": [p["prompt"] for p in prompts_cfg],
                             "num_runs": num_runs},
                  cumulative=["llm_visibility_history.csv", "llm_metrics.csv"])
    log(f"LLM visibility done in {time.time()-t0:.0f}s -> {out_dir}")


def _bool_or_none(v):
    if v is None:
        return None
    s = str(v).strip().lower()
    return True if s in ("true", "1") else False if s in ("false", "0") else None if s in ("", "nan", "none") else bool(v)


def reeval_llm(out_dir: Path):
    """Evaluation engine over ALREADY-CAPTURED raw LLM responses — re-runs attribution + action +
    metrics on today's stored rows, with NO ChatGPT/Apify re-query and NO crawling (cache + history
    only). This is the parse/evaluate separation: re-judge logic changes cheaply on stored data."""
    t0 = time.time()
    hist = DATA / "llm_visibility_history.csv"
    if not hist.exists():
        sys.exit("no LLM history to re-evaluate — run --llm first")
    df = pd.read_csv(hist)
    df = df.where(pd.notna(df), None)
    rows = df[df["run_date"].astype(str) == RUN_DATE].to_dict("records")
    if not rows:
        sys.exit(f"no stored LLM rows for {RUN_DATE} to re-evaluate")
    for r in rows:                       # restore types lost to CSV roundtrip
        for b in ("inito_mentioned", "inito_recommended", "stale_product_described"):
            r[b] = _bool_or_none(r.get(b))
        r["status"] = r.get("status") or "ok"
        r["sources_cited"] = r.get("sources_cited") or "[]"
    log(f"RE-EVAL: {len(rows)} stored observations (no re-query; cache + history only)")
    verify_stale_attribution(rows, fetch_missing=False)
    df_all = persist_llm(rows, out_dir)
    compute_llm_metrics(df_all, out_dir)
    _finalize_run(out_dir, track="llm-reeval",
                  selection={"note": "re-evaluated stored responses, no re-query",
                             "rows": len(rows)},
                  cumulative=["llm_visibility_history.csv", "llm_metrics.csv"])
    log(f"RE-EVAL done in {time.time()-t0:.0f}s -> {out_dir}")


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
    """Remove a run folder that ended up empty (e.g. resume skipped everything)."""
    try:
        d = Path(out_dir)
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    except Exception:
        pass

def _finalize_run(out_dir: Path, track: str, selection: dict, cumulative: List[str]) -> None:
    """Make the run folder self-contained: copy cumulative files in + write run_info.csv."""
    for name in cumulative:
        src = DATA / name
        if src.exists() and Path(out_dir) != DATA:
            shutil.copy(src, out_dir / name)
    info = [{"key": "track", "value": track}, {"key": "run_date", "value": RUN_DATE},
            {"key": "folder", "value": out_dir.name}]
    for k, v in selection.items():
        info.append({"key": k, "value": v if isinstance(v, (int, str)) else json.dumps(v)})
    pd.DataFrame(info).to_csv(out_dir / "run_info.csv", index=False)


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


# ---------- CLI ----------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Inito GEO monitor")
    ap.add_argument("--refresh", action="store_true", help="Track A: web/SERP/news/ads/reddit")
    ap.add_argument("--llm", action="store_true", help="Track B: live-web LLM assistants")
    ap.add_argument("--diff-only", action="store_true", help="recompute metrics + diff, no crawling")
    ap.add_argument("--reeval", action="store_true",
                    help="Track B: re-run the evaluation engine on today's stored responses (no re-query, no crawl)")
    ap.add_argument("--sources", help="Track A sources (e.g. 'serp,reddit' or 'all')")
    ap.add_argument("--queries", help="Track A queries (indices/names, or 'all')")
    ap.add_argument("--surfaces", help="Track B surfaces (e.g. 'chatgpt' or 'all')")
    ap.add_argument("--prompts", help="Track B prompts (indices/names, or 'all')")
    ap.add_argument("--num-runs", type=int, help="samples per (prompt × surface); default config")
    ap.add_argument("--note", default="", help="short note added to the run-folder name")
    ap.add_argument("-y", "--yes", action="store_true", help="non-interactive: use specs/all, no prompts")
    a = ap.parse_args(argv)

    if a.diff_only:
        diff_only(); return
    if a.reeval:
        out_dir = make_run_dir("llm-reeval", ["chatgpt+perplexity"], [0], note=a.note or "reeval")
        reeval_llm(out_dir); return
    if a.llm:
        surfaces = prompt_select(CFG["llm_surfaces"], lambda s: s,
                                 "Surfaces:", a.surfaces, a.yes)
        prompts = prompt_select(CFG["llm_visibility_prompts"], lambda p: p["prompt"],
                                "Prompts:", a.prompts, a.yes)
        num_runs = a.num_runs or CFG.get("llm_num_runs", 3)
        if not surfaces or not prompts:
            sys.exit("nothing selected")
        out_dir = make_run_dir("llm", surfaces, prompts, num_runs, a.note)
        run_llm_visibility(surfaces, prompts, num_runs, out_dir)
        return
    if a.refresh:
        sources = prompt_select(WEB_SOURCES, lambda s: s, "Sources:", a.sources, a.yes)
        queries = prompt_select(CFG["queries"], lambda q: q["q"], "Queries:", a.queries, a.yes)
        if not sources or not queries:
            sys.exit("nothing selected")
        out_dir = make_run_dir("web", sources, queries, note=a.note)
        refresh(sources, queries, out_dir)
        return
    ap.print_help()


if __name__ == "__main__":
    main()
