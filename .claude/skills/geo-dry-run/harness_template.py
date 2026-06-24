"""Hermetic dry-run harness for the Inito GEO pipeline (TEMPLATE — copy to scratchpad, edit, run).

No network, no cost: stubs apify_client + anthropic before import, fakes run_actor + the Claude judge,
points DATA at a temp dir, and drives the real CLI end-to-end. Adjust FAKE DATA + SCENARIOS for your
change. Never commit this or write into the repo's data/.

    python3 harness_template.py [/path/to/repo]
"""
import sys, types, os, importlib.util, tempfile, pathlib

# ---- stub network deps BEFORE importing pipeline ----
for name in ("apify_client", "anthropic"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["apify_client"].ApifyClient = lambda *a, **k: None
sys.modules["anthropic"].Anthropic = lambda *a, **k: None
os.environ.setdefault("APIFY_TOKEN", "test")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("PERPLEXITY_API_KEY", "pplx-test")

# ---- locate pipeline.py: argv[1] | $GEO_REPO | walk up from CWD or this script ----
def _find_pipeline():
    cands = []
    if len(sys.argv) > 1: cands.append(pathlib.Path(sys.argv[1]))
    if os.environ.get("GEO_REPO"): cands.append(pathlib.Path(os.environ["GEO_REPO"]))
    starts = [pathlib.Path.cwd(), pathlib.Path(__file__).resolve().parent]
    for s in starts:
        cands += [s, *s.parents]
    for d in cands:
        if (d / "pipeline.py").exists():
            return d / "pipeline.py"
    raise SystemExit("pipeline.py not found — pass the repo path: python3 harness_template.py /path/to/repo")
spec = importlib.util.spec_from_file_location("pipeline", _find_pipeline())
pipe = importlib.util.module_from_spec(spec); spec.loader.exec_module(pipe)

TMP = pathlib.Path(tempfile.mkdtemp(prefix="geo_dryrun_"))
pipe.DATA = TMP
pipe.FETCH_CACHE_PATH = TMP / "fetch_cache.csv"
pipe.CFG["ads_start_urls"] = ["https://adstransparency.google.com/advertiser/INITO?region=US"]
print(f"DATA dir: {TMP}\n")

# ---- fake Claude judge: returns a real tool_use block; inspects BODY only (current tool schemas) ----
class _Block:
    type = "tool_use"
    def __init__(self, inp): self.input = inp
class _Resp:
    def __init__(self, inp): self.content = [_Block(inp)]
class _Msgs:
    def create(self, model, max_tokens, system, tools, tool_choice, messages):
        full = messages[0]["content"]
        body = (full.split("PAGE TEXT:")[-1] if "PAGE TEXT:" in full
                else full.split("RESPONSE:")[-1] if "RESPONSE:" in full else full).lower()
        old = any(s in body for s in ("uses your iphone's camera", "phone camera", "attach the monitor",
                                      "clip", "not available on android", "only works with iphone", "iphone only"))
        comps = [c for c in ("Mira", "Proov", "Kegg") if c.lower() in body]
        says = "Still describes the OLD phone-dependent product." if old else "Describes the current wireless reader."
        common = {"says_about_inito": says, "mentions_competition": bool(comps),
                  "competition_summary": ("vs " + ", ".join(comps)) if comps else None,
                  "competitors_named": comps, "sentiment_inito": -0.4 if (old or comps) else 0.2,
                  "price_mentioned": "$149" if "$149" in body else None}
        if tool_choice["name"] == "classify_page":
            return _Resp(common)
        return _Resp({**common, "inito_mentioned": "inito" in body,
                      "inito_rank": 1 if "inito" in body else None,
                      "inito_recommended": "worth it" in body or "great" in body,
                      "sources_cited": pipe._extract_urls(full)})
class _FakeClaude: messages = _Msgs()
pipe.claude = _FakeClaude()

# ---- fake Perplexity sonar API (direct, not an actor) ----
pipe.PPLX_KEY = "pplx-test"
pipe.perplexity_complete = lambda prompt, model: (
    "Inito clips to your iPhone and uses the phone camera. Mira is preferred." if "mira" in prompt.lower()
    else "Inito InSight Wireless Reader works on iOS and Android. See https://inito.com/buy",
    ["https://oldblog.com/inito"] if "mira" in prompt.lower() else ["https://inito.com/"])

# ---- fake Apify actors: edit datasets per your change ----
STALE = "Inito only works with iPhone. You attach the monitor to your phone and it uses your iPhone's camera. Not available on Android. $149"
CURRENT = "The Inito InSight Wireless Reader is Wi-Fi enabled and works on both iOS and Android. See https://inito.com/buy"
TEXT_BY_URL = {"https://inito.com": CURRENT, "https://inito.com/buy-now": CURRENT,
               "https://preprod.inito.com/en-us/faqs": CURRENT,  # nonprod owned host -> should be flagged
               "https://leafsnap.com/inito-review": STALE, "https://oldblog.com/inito": STALE,
               "https://miracare.com/best": "Mira is more accurate than Inito.",
               "https://reddit.com/r/x/1": "Does Inito work on Android? It's iPhone only and uses the camera."}

def fake_run_actor(actor_id, run_input, label):
    if label == "serp":
        return [{"searchQuery": {"term": "Inito fertility monitor"},
                 "organicResults": [{"url": "https://inito.com/", "title": "Inito", "description": CURRENT},
                                    {"url": "https://leafsnap.com/inito-review", "title": "R", "description": STALE}],
                 "aiOverview": "Inito is an at-home fertility monitor."},
                {"searchQuery": {"term": "best at-home fertility monitor"},
                 "organicResults": [{"url": "https://preprod.inito.com/en-us/faqs?os=android&utm_source=x", "title": "FAQ", "description": CURRENT},
                                    {"url": "https://miracare.com/best", "title": "Mira", "description": "Mira is more accurate than Inito."}]}]
    if label == "news":
        return [{"searchQuery": {"term": "Inito fertility monitor"},
                 "organicResults": [{"url": "https://news.com/inito", "title": "N", "description": CURRENT}]}]
    if label == "ads":
        return [{"advertiserName": "Inito", "creativeId": "c1", "url": "https://inito.com/ad-lp",
                 "variants": [{"text": "Inito clips onto your iPhone — only on iPhone."}]}]
    if label == "reddit":
        return [{"url": "https://reddit.com/r/x/1", "title": "Inito android?",
                 "body": TEXT_BY_URL["https://reddit.com/r/x/1"]}]
    if label == "content":
        return [{"url": pipe.normalize_url(su["url"]),
                 "text": TEXT_BY_URL.get(pipe.normalize_url(su["url"]), CURRENT)}
                for su in run_input.get("startUrls", [])]
    if label.startswith("chatgpt"):
        out = []
        for p in run_input.get("prompts", []):
            stale = "mira" in p.lower()
            out.append({"prompt": p,
                        "response": ("Inito only works on iPhone and uses the phone camera; Mira is preferred. See https://leafsnap.com/inito-review?utm_source=chatgpt.com"
                                     if stale else "Inito (InSight Wireless Reader) is great. Worth it? Yes. https://inito.com/?utm_source=chatgpt.com"),
                        "citations": [{"url": "https://preprod.inito.com/faqs?os=android" if stale else "https://inito.com/"}]})
        return out
    # NOTE: Perplexity is NOT an actor — stubbed above via pipe.perplexity_complete / pipe.PPLX_KEY.
    return []
pipe.run_actor = fake_run_actor

import pandas as pd
pd.set_option("display.max_columns", None); pd.set_option("display.width", 220)

# ============================ SCENARIOS (edit for your change) ============================
print("### Track A (web)")
pipe.main(["--refresh", "--sources", "serp,ads,reddit,news", "--queries", "all", "-y"])
print("\n### Track B (llm, 2 runs)")
pipe.main(["--llm", "--surfaces", "chatgpt,perplexity", "--prompts", "1,7", "--num-runs", "2", "-y"])

print("\n### OUTPUTS")
for d in sorted(p for p in TMP.iterdir() if p.is_dir()):
    print("📁", d.name, "->", ", ".join(sorted(f.name for f in d.iterdir())))
for f in sorted(TMP.glob("*__web__*/web_observations.csv")):
    print("\n----- web_observations.csv -----"); print(pd.read_csv(f).to_string())
for f in sorted(TMP.glob("*__llm__*/llm_observations.csv")):
    print("\n----- llm_observations.csv -----"); print(pd.read_csv(f).to_string())
print("\nDONE. (assert columns == WEB_COLUMNS / LLM_COLUMNS, nonprod flagged, sources canonical)")
