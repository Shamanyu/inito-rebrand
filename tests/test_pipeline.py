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
    for key in ("status", "claims_confirmed", "sentiment_inito", "competitor_framing"):
        assert key in v_stale

def test_judge_fallback_mixed(pipe):
    v = pipe.judge("http://z", MIXED_TEXT, pipe.detect_claims(MIXED_TEXT))
    assert v["status"] in ("mixed", "stale")  # has both signals; never 'current'


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
    assert len(mdf) == 2                                            # two dated rows -> diffable series


def test_share_of_voice(pipe):
    rows = [
        mkrow(pipe, "https://inito.com/p", "owned", "current", {}, intent="category", rank=2),
        mkrow(pipe, "https://miracare.com/p", "competitor", "current", {}, intent="category", rank=1),
    ]
    pipe.RUN_DATE = "2026-06-19"; df = pipe.persist(rows); m, _ = pipe.compute_metrics(df)
    assert m["share_of_voice_category"] == 1.0  # owned domain present in top-10 for the category query
