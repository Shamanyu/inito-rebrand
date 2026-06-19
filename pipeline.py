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

def run_actor(actor_id: str, run_input: dict, label: str):
    """Call an actor, return its dataset items (list of dicts)."""
    log(f"  actor {actor_id} ({label}) starting…")
    run = apify.actor(actor_id).call(run_input=run_input)
    items = list(apify.dataset(run["defaultDatasetId"]).iterate_items())
    log(f"  actor {actor_id} ({label}) -> {len(items)} items")
    return items


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
        # AI Overview text (when present) is a high-signal LLM-style answer to log verbatim
        ai = item.get("aiOverview") or item.get("aiOverviewText")
        if ai:
            out.append({"url": f"aioverview::{q}", "platform": "ai_overview", "query": q,
                        "intent": intent, "rank": 0, "title": "AI Overview",
                        "snippet": ai if isinstance(ai, str) else json.dumps(ai)[:4000]})
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
    return out


# ---------- stage 2: enrich (full page text for web URLs) ----------
def enrich_content(urls: list[str]) -> dict[str, str]:
    """Website Content Crawler -> {url: text}. Only real http(s) pages; skips pseudo-urls."""
    real = [u for u in urls if u.startswith("http") and not u.startswith("aioverview::")]
    if not real:
        return {}
    run_input = {
        "startUrls": [{"url": u} for u in real],
        "crawlerType": "playwright:adaptive",
        "maxCrawlDepth": 0, "maxCrawlPages": len(real),
        "proxyConfiguration": {"useApifyProxy": True},
        "saveMarkdown": True,
    }
    text = {}
    for it in run_actor(CFG["actors"]["content"], run_input, "content"):
        u = it.get("url")
        if u:
            text[normalize_url(u)] = (it.get("text") or it.get("markdown") or "")[:20000]
    return text


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
A page is STALE if it presents the old product as current (iPhone-only, attaches to phone, camera-based, no Android, old price). MIXED if it has both old and new. CURRENT if it correctly reflects the wireless reader.
Return ONLY minified JSON, no prose, no markdown:
{"status":"stale|mixed|current","current_product_named":bool,"claims_confirmed":{"iphone_only":bool,"attach_to_phone":bool,"camera_dependent":bool,"no_android":bool},"price_mentioned":string|null,"sentiment_inito":number,"competitor_framing":bool}
sentiment_inito is -1..1 toward Inito. competitor_framing is true if the page pushes a rival (e.g. Mira) as better."""

def judge(url: str, text: str, regex_flags: dict) -> dict:
    excerpt = text[:6000] if text else ""
    user = (f"URL: {url}\nRegex hints: {json.dumps({k:v for k,v in regex_flags.items() if k!='prices_seen'})}\n"
            f"Prices seen: {regex_flags.get('prices_seen')}\n\nPAGE TEXT:\n{excerpt}")
    try:
        resp = claude.messages.create(
            model=CFG["limits"]["judge_model"], max_tokens=400,
            system=JUDGE_SYSTEM, messages=[{"role": "user", "content": user}])
        raw = resp.content[0].text
        m = re.search(r"\{.*\}", raw, re.DOTALL)  # grab the JSON object, ignore any fences/prose
        return json.loads(m.group(0) if m else raw)
    except Exception as e:
        log(f"  judge fallback for {url}: {e}")
        # deterministic fallback from regex if the judge call/parse fails
        cc = {k: regex_flags.get(k, False) for k in ("iphone_only", "attach_to_phone", "camera_dependent", "no_android")}
        any_stale = any(cc.values())
        status = "current" if (regex_flags.get("current_signal") and not any_stale) else ("mixed" if regex_flags.get("current_signal") else ("stale" if any_stale else "current"))
        return {"status": status, "current_product_named": regex_flags.get("current_signal", False),
                "claims_confirmed": cc, "price_mentioned": (regex_flags.get("prices_seen") or [None])[0],
                "sentiment_inito": 0.0, "competitor_framing": False, "_fallback": True}

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
    log(f"persisted {len(rows)} rows -> {snap.name}")
    return df

def compute_metrics(df_all: pd.DataFrame):
    cur = df_all[df_all["run_date"] == RUN_DATE]
    web = cur[cur["platform"].isin(["web", "reddit", "instagram", "x", "youtube"])]
    def claim_count(c): return int(web["claim_" + c].fillna(False).sum())
    stale = web[web["status"].isin(["stale", "mixed"])]
    metrics = {
        "run_date": RUN_DATE,
        "total_urls": int(web["url"].nunique()),
        "stale_or_mixed": int(len(stale)),
        "owned_stale": int(len(stale[stale["ownership"].isin(["owned", "owned_marketplace"])])),
        "competitor_negative": int(len(web[(web["ownership"] == "competitor") &
                                           ((web["sentiment_inito"] < 0) | (web["competitor_framing"] == True))])),
        "claim_iphone_only": claim_count("iphone_only"),
        "claim_attach_to_phone": claim_count("attach_to_phone"),
        "claim_camera_dependent": claim_count("camera_dependent"),
        "claim_no_android": claim_count("no_android"),
        "mean_sentiment": round(float(web["sentiment_inito"].fillna(0).mean()), 3),
        "share_of_voice_category": _sov(cur),
    }
    mpath = DATA / "metrics.csv"
    mdf = pd.read_csv(mpath) if mpath.exists() else pd.DataFrame()
    mdf = pd.concat([mdf[mdf.get("run_date", "") != RUN_DATE] if len(mdf) else mdf,
                     pd.DataFrame([metrics])], ignore_index=True)
    mdf.to_csv(mpath, index=False)
    return metrics, mdf

def _sov(cur: pd.DataFrame) -> float:
    """Share of voice on category queries: fraction where an owned domain appears in top 10."""
    cat = cur[(cur["intent"] == "category") & (cur["platform"] == "web") & (cur["rank"].between(1, 10))]
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
              "claim_no_android", "mean_sentiment", "share_of_voice_category"]:
        d = b[k] - a[k]
        arrow = "↓" if d < 0 else ("↑" if d > 0 else "·")
        log(f"  {k:28} {a[k]:>7} -> {b[k]:>7}  {arrow}{abs(d):.3g}")


# ---------- orchestration ----------
def refresh(no_social: bool):
    t0 = time.time()
    log("STAGE 1 discover")
    recs = discover_serp() + discover_reddit()
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
            "sentiment_inito": 0.0, "competitor_framing": False}
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
            "title": r.get("title", ""),
        })

    log("STAGE 4 persist + diff")
    df_all = persist(rows)
    metrics, mdf = compute_metrics(df_all)
    log(f"metrics: {json.dumps(metrics)}")
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
    ap.add_argument("--no-social", action="store_true", help="skip IG/X/YouTube (cheaper)")
    ap.add_argument("--diff-only", action="store_true", help="recompute metrics + diff only")
    a = ap.parse_args()
    if a.diff_only:
        diff_only()
    elif a.refresh:
        refresh(a.no_social)
    else:
        ap.print_help()
