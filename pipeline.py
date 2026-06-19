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

def run_actor(actor_id: str, run_input: dict, label: str, retries: int = 3) -> list:
    """Call an actor, return its dataset items. Retries with exponential backoff on failure."""
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
            wait = 2 ** attempt
            log(f"  actor {actor_id} ({label}) attempt {attempt} failed: {e}. Retrying in {wait}s…")
            time.sleep(wait)
    return []


# ---------- stage 1: discover ----------
def discover_serp() -> list[dict]:
    """Google SERP via apify/google-search-scraper. Captures organic rank + AI Overview."""
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
        # AI Overview text stored verbatim as a pseudo-URL so it's tracked and judged per run
        ai = item.get("aiOverview") or item.get("aiOverviewText")
        if ai:
            out.append({"url": f"aioverview::{q}", "platform": "ai_overview", "query": q,
                        "intent": intent, "rank": 0, "title": "AI Overview",
                        "snippet": ai if isinstance(ai, str) else json.dumps(ai)[:4000]})
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
                 "type": "posts", "sort": "relevance", "proxy": {"useApifyProxy": True}}
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
def refresh(no_social: bool):
    t0 = time.time()
    log("STAGE 1 discover")
    recs = discover_serp() + discover_bing() + discover_news() + discover_reddit()
    if not no_social:
        recs += discover_social()

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


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="run the full pipeline")
    ap.add_argument("--no-social", action="store_true", help="skip IG/X/YouTube/TikTok (cheaper)")
    ap.add_argument("--diff-only", action="store_true", help="recompute metrics + diff only")
    a = ap.parse_args()
    if a.diff_only:
        diff_only()
    elif a.refresh:
        refresh(a.no_social)
    else:
        ap.print_help()
