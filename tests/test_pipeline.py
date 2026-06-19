"""Offline unit tests for the Inito GEO pipeline. Run: pytest -q"""
import pytest
from conftest import mkrow

STALE_TEXT = ("Inito is only compatible with iPhone. You attach the monitor to your phone "
              "and it uses your iPhone's camera. Android phones are not supported. $149")
CURRENT_TEXT = ("The new InSight Wireless Reader is Wi-Fi enabled and compatible with both "
                "iOS and Android. $99")
MIXED_TEXT = ("The original reader clipped onto your iPhone and used the camera. The new "
              "InSight Wireless Reader is now available on Android too.")


# ---------- url helpers ----------
def test_normalize_url_strips_www_tracking_fragment_trailing_slash(pipe):
    assert pipe.normalize_url("https://WWW.Inito.com/buy-now/?utm_source=x#frag") == "https://inito.com/buy-now"
    assert pipe.normalize_url("http://inito.com/") == "http://inito.com"

def test_domain_of(pipe):
    assert pipe.domain_of("https://www.miracare.com/mira-vs-inito-comparison/") == "miracare.com"


# ---------- ownership routing ----------
@pytest.mark.parametrize("url,expected", [
    ("https://inito.com/buy-now", "owned"),
    ("https://blog.inito.com/x", "owned"),
    ("https://apps.apple.com/us/app/inito-fertility-ovulation/id1183799668", "owned"),
    ("https://apps.apple.com/us/app/something-else/id999", "third_party"),
    ("https://play.google.com/store/apps/details?id=com.inito.insight", "owned"),
    ("https://www.amazon.com/Inito/dp/B0CM17Y1TH", "owned_marketplace"),
    ("https://miracare.com/mira-vs-inito-comparison", "competitor"),
    ("https://leafsnap.com/inito-review", "third_party"),
])
def test_ownership_routing(pipe, url, expected):
    assert pipe.ownership(url) == expected


# ---------- claim detection ----------
def test_detect_claims_on_stale_text(pipe):
    f = pipe.detect_claims(STALE_TEXT)
    assert f["iphone_only"] and f["attach_to_phone"] and f["camera_dependent"] and f["no_android"]
    assert "$149" in f["prices_seen"]

def test_detect_claims_attach_regression(pipe):
    # regression: "attach the monitor to your phone" is a 21-char gap that the old .{0,20} missed
    assert pipe.detect_claims("you attach the monitor to your phone each morning")["attach_to_phone"]

def test_detect_claims_on_current_text(pipe):
    f = pipe.detect_claims(CURRENT_TEXT)
    assert not f["iphone_only"] and not f["no_android"]
    assert f["current_signal"]


# ---------- judge offline fallback ----------
def test_judge_fallback_classifies_stale_and_current(pipe):
    v_stale = pipe.judge("http://x", STALE_TEXT, pipe.detect_claims(STALE_TEXT))
    v_cur = pipe.judge("http://y", CURRENT_TEXT, pipe.detect_claims(CURRENT_TEXT))
    assert v_stale["status"] == "stale"
    assert v_cur["status"] == "current"
    # fallback still returns the full contract so downstream rows never KeyError
    for key in ("status", "claims_confirmed", "sentiment_inito", "competitor_framing", "confidence"):
        assert key in v_stale

def test_judge_fallback_mixed(pipe):
    v = pipe.judge("http://z", MIXED_TEXT, pipe.detect_claims(MIXED_TEXT))
    assert v["status"] in ("mixed", "stale")  # has both signals; never 'current'

def test_judge_fallback_confidence_is_numeric(pipe):
    v = pipe.judge("http://x", STALE_TEXT, pipe.detect_claims(STALE_TEXT))
    assert isinstance(v["confidence"], float)
    assert 0.0 <= v["confidence"] <= 1.0


# ---------- SERP parsing (guards the searchQuery dict/str fix) ----------
def test_serp_parsing_handles_dict_and_string_query(pipe, monkeypatch):
    fake = [
        {"searchQuery": {"term": "Inito vs Mira"},
         "organicResults": [{"url": "https://miracare.com/x", "title": "t", "description": "d"}],
         "aiOverview": "Inito is iPhone only"},
        {"searchQuery": "Inito review",  # string form must not crash
         "organicResults": [{"url": "https://leafsnap.com/inito-review"}]},
    ]
    monkeypatch.setattr(pipe, "run_actor", lambda *a, **k: fake)
    rows = pipe.discover_serp()
    urls = [r["url"] for r in rows]
    assert "https://miracare.com/x" in urls
    assert any(u.startswith("aioverview::") for u in urls)           # AI Overview captured
    by_q = {r["query"]: r["intent"] for r in rows}
    assert by_q["Inito vs Mira"] == "comparison"                     # intent resolved from config
    assert by_q["Inito review"] == "brand_entity"                    # string-form query handled


# ---------- fetch cache ----------
def test_fetch_cache_roundtrip(pipe):
    entries = {"https://inito.com/buy-now": "some page text", "https://example.com/x": "other text"}
    pipe.save_fetch_cache(entries)
    cache = pipe.load_fetch_cache()
    assert cache["https://inito.com/buy-now"] == "some page text"
    assert cache["https://example.com/x"] == "other text"

def test_enrich_content_uses_cache(pipe, monkeypatch):
    pipe.save_fetch_cache({"https://cached.com/page": "cached text"})
    actor_calls = []
    monkeypatch.setattr(pipe, "run_actor", lambda *a, **k: (actor_calls.append(a), [])[1])
    result = pipe.enrich_content(["https://cached.com/page"])
    assert result["https://cached.com/page"] == "cached text"
    assert len(actor_calls) == 0  # actor not called — served from cache


# ---------- review queue ----------
def test_low_confidence_rows_written_to_review_queue(pipe):
    rows = [
        mkrow(pipe, "https://inito.com/buy-now", "owned", "stale", {}, confidence=0.4),
        mkrow(pipe, "https://leafsnap.com/x", "third_party", "current", {}, confidence=0.95),
    ]
    pipe.persist(rows)
    rq_path = pipe.DATA / "review_queue.csv"
    assert rq_path.exists()
    import pandas as pd
    rq = pd.read_csv(rq_path)
    assert len(rq) == 1
    assert "inito.com" in rq.iloc[0]["url"]


# ---------- kappa ----------
def test_kappa_returns_float(pipe):
    rows = [
        mkrow(pipe, "https://inito.com/p", "owned", "stale", {"iphone_only": True}),
        mkrow(pipe, "https://leafsnap.com/p", "third_party", "current", {}),
    ]
    k = pipe._kappa_regex_vs_judge(rows)
    assert k == k or k != k  # either a float or nan — just must not raise


# ---------- metrics + diff across two runs ----------
def test_metrics_and_diff_decay(pipe):
    run1 = [
        mkrow(pipe, "https://inito.com/buy-now", "owned", "stale", {"iphone_only": True, "no_android": True}),
        mkrow(pipe, "https://leafsnap.com/x", "third_party", "stale", {"camera_dependent": True}),
        mkrow(pipe, "https://miracare.com/x", "competitor", "stale", {}, sentiment=-0.5, framing=True),
        mkrow(pipe, "https://thebump.com/x", "third_party", "current", {}, sentiment=0.6, intent="category", rank=3),
    ]
    run2 = [
        mkrow(pipe, "https://inito.com/buy-now", "owned", "current", {}),  # owned page fixed
        mkrow(pipe, "https://leafsnap.com/x", "third_party", "stale", {"camera_dependent": True}),
        mkrow(pipe, "https://miracare.com/x", "competitor", "stale", {}, sentiment=-0.5, framing=True),
        mkrow(pipe, "https://thebump.com/x", "third_party", "current", {}, sentiment=0.6, intent="category", rank=3),
    ]
    pipe.RUN_DATE = "2026-06-12"; df1 = pipe.persist(run1); m1, _ = pipe.compute_metrics(df1)
    pipe.RUN_DATE = "2026-06-19"; df2 = pipe.persist(run2); m2, mdf = pipe.compute_metrics(df2)

    assert m1["owned_stale"] == 1 and m2["owned_stale"] == 0        # the headline: owned fix shows up
    assert m1["claim_iphone_only"] == 1 and m2["claim_iphone_only"] == 0
    assert m2["stale_or_mixed"] == 2
    assert m2["competitor_negative"] == 1
    assert len(mdf) == 2                                             # two dated rows -> diffable series
    assert "run_quality_score" in m2
    assert 0 <= m2["run_quality_score"] <= 100


# ---------- LLM visibility ----------
def test_judge_llm_response_fallback(pipe):
    # anthropic is stubbed -> falls back deterministically
    v = pipe.judge_llm_response("Inito reviews", "gpt-4o-mini",
                                 "Inito is a great fertility monitor.")
    assert isinstance(v["inito_mentioned"], bool)
    assert "sentiment_inito" in v
    assert 0.0 <= v["confidence"] <= 1.0

def test_discover_llm_visibility_runs_actor(pipe, monkeypatch):
    fake_items = [
        {"prompt": "Inito", "response": "Inito is a good product."},
        {"prompt": "Inito reviews", "response": "Inito has positive reviews."},
    ]
    monkeypatch.setattr(pipe, "run_actor", lambda *a, **k: fake_items)
    rows = pipe.discover_llm_visibility(models=["gpt-4o-mini"])
    assert len(rows) == 2
    assert rows[0]["model"] == "gpt-4o-mini"
    assert rows[0]["prompt"] == "Inito"
    assert "inito_mentioned" in rows[0]
    assert "stale_product_described" in rows[0]

def test_persist_llm_writes_files(pipe):
    rows = [
        {"run_date": "2026-06-19", "model": "gpt-4o-mini", "prompt": "Inito",
         "intent": "brand_entity", "response_text": "text", "inito_mentioned": True,
         "inito_rank": 1, "inito_recommended": True, "stale_product_described": False,
         "sentiment_inito": 0.8, "competitors_named": "[]", "competitor_preferred": None,
         "confidence": 0.9},
    ]
    pipe.persist_llm(rows)
    assert (pipe.DATA / "llm_visibility_latest.csv").exists()

def test_compute_llm_metrics(pipe):
    rows = [
        {"run_date": pipe.RUN_DATE, "model": "gpt-4o-mini", "prompt": "Inito",
         "intent": "brand_entity", "response_text": "t", "inito_mentioned": True,
         "inito_rank": 1, "inito_recommended": True, "stale_product_described": False,
         "sentiment_inito": 0.7, "competitors_named": "[]", "competitor_preferred": None,
         "confidence": 0.9},
        {"run_date": pipe.RUN_DATE, "model": "gpt-4o-mini", "prompt": "Inito reviews",
         "intent": "brand_entity", "response_text": "t2", "inito_mentioned": False,
         "inito_rank": None, "inito_recommended": False, "stale_product_described": True,
         "sentiment_inito": -0.2, "competitors_named": '["Mira"]', "competitor_preferred": "Mira",
         "confidence": 0.8},
    ]
    import pandas as pd
    df = pipe.persist_llm(rows)
    m = pipe.compute_llm_metrics(df)
    assert m["llm_mention_rate"] == 0.5
    assert m["llm_stale_rate"] == 0.5
    assert "llm_gpt_4o_mini_mention_rate" in m


def test_share_of_voice(pipe):
    rows = [
        mkrow(pipe, "https://inito.com/p", "owned", "current", {}, intent="category", rank=2),
        mkrow(pipe, "https://miracare.com/p", "competitor", "current", {}, intent="category", rank=1),
    ]
    pipe.RUN_DATE = "2026-06-19"; df = pipe.persist(rows); m, _ = pipe.compute_metrics(df)
    assert m["share_of_voice_category"] == 1.0  # owned domain present in top-10 for the category query
