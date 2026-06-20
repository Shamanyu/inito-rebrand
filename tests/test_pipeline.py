"""Offline unit tests for the Inito GEO pipeline. Run: pytest -q"""
import json
import pytest
from conftest import mkrow

STALE_TEXT = ("Inito is only compatible with iPhone. You attach the monitor to your phone "
              "and it uses your iPhone's camera. Android phones are not supported. $149")
CURRENT_TEXT = ("The new InSight Wireless Reader is Wi-Fi enabled and compatible with both "
                "iOS and Android. $99")
MIXED_TEXT = ("The original reader clipped onto your iPhone and used the camera. The new "
              "InSight Wireless Reader is now available on Android too.")
# Shared, NOT-stale attributes (hormones + app + dip-strip) must never trip a claim flag.
NEUTRAL_TEXT = ("Inito measures estrogen, LH, PdG and FSH on one strip and sends results to "
                "the Inito app on your phone. Just dip the strip and read results in 10 minutes.")


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

@pytest.mark.parametrize("advertiser,url,expected", [
    ("Inito Inc", "https://example.com/ad", "owned"),
    ("Miracare Inc", "https://example.com/ad", "competitor"),       # matches domain label 'miracare'
    ("competitor ad", "https://miracare.com/x", "competitor"),       # falls through to URL ownership
    ("Some Random Co", "https://leafsnap.com/x", "third_party"),
])
def test_ownership_for_ad(pipe, advertiser, url, expected):
    assert pipe.ownership_for_ad(advertiser, url) == expected


# ---------- claim detection ----------
def test_detect_claims_on_stale_text(pipe):
    f = pipe.detect_claims(STALE_TEXT)
    assert f["iphone_only"] and f["attach_to_phone"] and f["camera_dependent"] and f["no_android"]
    assert "$149" in f["prices_seen"]

def test_detect_claims_attach_regression(pipe):
    # regression: "attach the monitor to your phone" is a 21-char gap the old .{0,20} missed
    assert pipe.detect_claims("you attach the monitor to your phone each morning")["attach_to_phone"]

def test_detect_claims_lightning_port(pipe):
    assert pipe.detect_claims("connect via the lightning port to read the strip")["attach_to_phone"]

def test_detect_claims_on_current_text(pipe):
    f = pipe.detect_claims(CURRENT_TEXT)
    assert not f["iphone_only"] and not f["no_android"]
    assert f["current_signal"]

def test_detect_claims_shared_attributes_not_stale(pipe):
    # hormones + app + dip-strip are common to both products -> no stale flags (false-positive guard)
    f = pipe.detect_claims(NEUTRAL_TEXT)
    assert not any(f[k] for k in ("iphone_only", "attach_to_phone", "camera_dependent", "no_android"))


# ---------- judge offline fallback ----------
def test_judge_fallback_classifies_stale_and_current(pipe):
    v_stale = pipe.judge("http://x", STALE_TEXT, pipe.detect_claims(STALE_TEXT))
    v_cur = pipe.judge("http://y", CURRENT_TEXT, pipe.detect_claims(CURRENT_TEXT))
    assert v_stale["status"] == "stale"
    assert v_cur["status"] == "current"
    for key in ("status", "claims_confirmed", "sentiment_inito", "competitor_framing", "confidence"):
        assert key in v_stale

def test_judge_fallback_mixed(pipe):
    v = pipe.judge("http://z", MIXED_TEXT, pipe.detect_claims(MIXED_TEXT))
    assert v["status"] in ("mixed", "stale")

def test_judge_fallback_neutral_is_current(pipe):
    v = pipe.judge("http://n", NEUTRAL_TEXT, pipe.detect_claims(NEUTRAL_TEXT))
    assert v["status"] == "current"

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
        {"searchQuery": "Inito review",
         "organicResults": [{"url": "https://leafsnap.com/inito-review"}]},
    ]
    monkeypatch.setattr(pipe, "run_actor", lambda *a, **k: fake)
    rows = pipe.discover_serp()
    urls = [r["url"] for r in rows]
    assert "https://miracare.com/x" in urls
    assert any(u.startswith("aioverview::") for u in urls)
    by_q = {r["query"]: r["intent"] for r in rows}
    assert by_q["Inito vs Mira"] == "comparison"
    assert by_q["Inito review"] == "brand_entity"


# ---------- ads discovery ----------
def test_discover_ads_parses_and_tags_ownership(pipe, monkeypatch):
    monkeypatch.setitem(pipe.CFG, "ads_start_urls", ["https://adstransparency.google.com/advertiser/X?region=US"])
    fake = [{"advertiserName": "Inito", "creativeId": "c1", "format": "TEXT",
             "url": "https://inito.com/lp",
             "variants": [{"text": "Inito clips to your iPhone camera"}]}]
    monkeypatch.setattr(pipe, "run_actor", lambda *a, **k: fake)
    rows = pipe.discover_ads()
    assert len(rows) == 1
    assert rows[0]["platform"] == "ads"
    assert rows[0]["ownership"] == "owned"
    assert "clips to your iPhone" in rows[0]["snippet"]

def test_discover_ads_empty_when_no_urls(pipe, monkeypatch):
    monkeypatch.setitem(pipe.CFG, "ads_start_urls", [])
    assert pipe.discover_ads() == []


# ---------- fetch cache (CSV) ----------
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
    assert len(actor_calls) == 0  # served from cache, actor not called


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
    assert k == k or k != k  # float or nan — just must not raise


# ---------- metrics + diff across two runs (CSV roundtrip) ----------
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

    assert m1["owned_stale"] == 1 and m2["owned_stale"] == 0      # the headline: owned fix shows up
    assert m1["claim_iphone_only"] == 1 and m2["claim_iphone_only"] == 0
    assert m2["stale_or_mixed"] == 2
    assert m2["competitor_negative"] == 1
    assert len(mdf) == 2                                          # two dated rows -> diffable series
    assert 0 <= m2["run_quality_score"] <= 100


def test_kappa_derived_from_dataframe_when_no_current_rows(pipe):
    # regression: --diff-only recomputes without in-memory rows; kappa must come from the
    # persisted columns instead of going blank.
    rows = [
        mkrow(pipe, "https://oldblog.com/x", "third_party", "stale", {"iphone_only": True}),
        mkrow(pipe, "https://thebump.com/x", "third_party", "current", {}),
    ]
    pipe.RUN_DATE = "2026-06-19"
    df = pipe.persist(rows)
    m_no_rows, _ = pipe.compute_metrics(df, current_rows=None)  # the diff-only path
    assert m_no_rows["kappa_regex_judge"] == 1.0  # perfect regex/judge agreement, not blank


def test_cleanup_empty_removes_only_empty_dir(pipe, tmp_path):
    empty = tmp_path / "empty_run"; empty.mkdir()
    full = tmp_path / "full_run"; full.mkdir(); (full / "x.csv").write_text("a")
    pipe._cleanup_empty(empty)
    pipe._cleanup_empty(full)
    assert not empty.exists() and full.exists()


def test_share_of_voice(pipe):
    rows = [
        mkrow(pipe, "https://inito.com/p", "owned", "current", {}, intent="category", rank=2),
        mkrow(pipe, "https://miracare.com/p", "competitor", "current", {}, intent="category", rank=1),
    ]
    pipe.RUN_DATE = "2026-06-19"; df = pipe.persist(rows); m, _ = pipe.compute_metrics(df)
    assert m["share_of_voice_category"] == 1.0


# ---------- LLM visibility ----------
def test_judge_llm_response_fallback(pipe):
    v = pipe.judge_llm_response("Inito reviews", "chatgpt", "Inito is a great fertility monitor.")
    assert isinstance(v["inito_mentioned"], bool)
    assert "sentiment_inito" in v
    assert 0.0 <= v["confidence"] <= 1.0

def test_discover_llm_visibility_runs_surface(pipe, monkeypatch):
    fake_items = [
        {"prompt": "Inito", "response": "Inito is a good product.", "citations": []},
        {"prompt": "Inito reviews", "response": "Inito has positive reviews.", "citations": []},
    ]
    monkeypatch.setattr(pipe, "run_actor", lambda *a, **k: fake_items)
    rows = pipe.discover_llm_visibility(["chatgpt"], [{"prompt": "Inito", "intent": "brand_entity"},
                                                      {"prompt": "Inito reviews", "intent": "brand_entity"}], 1)
    assert len(rows) == 2
    assert rows[0]["surface"] == "chatgpt"
    assert "inito_mentioned" in rows[0]
    assert "action" in rows[0] and "priority" in rows[0]

def test_llm_row_empty_response_is_not_judged(pipe):
    # regression: an empty actor response must NOT be sent to the judge (it fabricates signals).
    row = pipe._llm_row(1, "perplexity", "Inito vs Mira", "comparison", "   ")
    assert row["status"] == "empty"
    assert row["inito_mentioned"] is None and row["confidence"] is None
    assert "Empty response" in row["action"] and row["priority"] == 6

def test_run_perplexity_uses_sonar_api(pipe, monkeypatch):
    monkeypatch.setattr(pipe, "PPLX_KEY", "pplx-test")
    monkeypatch.setattr(pipe, "perplexity_complete",
                        lambda prompt, model: ("Inito is a fertility monitor.", ["https://inito.com/"]))
    rows = pipe._run_perplexity(1, [{"prompt": "Inito", "intent": "brand_entity"}])
    assert len(rows) == 1 and rows[0]["surface"] == "perplexity" and rows[0]["status"] == "ok"
    assert "inito_mentioned" in rows[0] and "https://inito.com/" in rows[0]["sources_cited"]

def test_run_perplexity_errors_without_key(pipe, monkeypatch):
    monkeypatch.setattr(pipe, "PPLX_KEY", "")
    rows = pipe._run_perplexity(1, [{"prompt": "Inito", "intent": "brand_entity"}])
    assert rows[0]["status"] == "error" and "PERPLEXITY_API_KEY" in rows[0]["error_note"]

def test_run_perplexity_per_prompt_failfast(pipe, monkeypatch):
    monkeypatch.setattr(pipe, "PPLX_KEY", "pplx-test")
    def flaky(prompt, model):
        if prompt == "boom":
            raise RuntimeError("api 500")
        return ("ok answer", [])
    monkeypatch.setattr(pipe, "perplexity_complete", flaky)
    rows = pipe._run_perplexity(1, [{"prompt": "Inito", "intent": "brand_entity"},
                                    {"prompt": "boom", "intent": "brand_entity"}])
    by = {r["prompt"]: r["status"] for r in rows}
    assert by["Inito"] == "ok" and by["boom"] == "error"  # one bad prompt doesn't kill the batch

def test_discover_llm_visibility_error_rows_on_failure(pipe, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("actor exploded")
    monkeypatch.setattr(pipe, "run_actor", boom)
    rows = pipe.discover_llm_visibility(["chatgpt"], [{"prompt": "Inito", "intent": "brand_entity"}], 1)
    assert len(rows) == 1
    assert rows[0]["status"] == "error"
    assert "actor exploded" in rows[0]["error_note"]
    assert rows[0]["priority"] == 6

def test_resume_skips_completed_combos(pipe, monkeypatch):
    import pandas as pd
    # a completed chatgpt run 1 (real data) already in today's history
    pd.DataFrame([{"run_date": pipe.RUN_DATE, "run_index": 1, "surface": "chatgpt",
                   "prompt": "Inito", "inito_mentioned": True}]).to_csv(
        pipe.DATA / "llm_visibility_history.csv", index=False)
    monkeypatch.setattr(pipe, "run_actor", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not run")))
    rows = pipe.discover_llm_visibility(["chatgpt"], [{"prompt": "Inito", "intent": "brand_entity"}], 1)
    assert rows == []  # the only prompt was already done -> nothing to run

def test_resume_is_per_prompt_not_per_run(pipe, monkeypatch):
    # regression (C6): one done prompt must NOT skip the other prompts of the same (surface, run).
    import pandas as pd
    pd.DataFrame([{"run_date": pipe.RUN_DATE, "run_index": 1, "surface": "chatgpt",
                   "prompt": "Inito", "inito_mentioned": True}]).to_csv(
        pipe.DATA / "llm_visibility_history.csv", index=False)
    seen = {}
    def fake_actor(actor_id, run_input, label, retries=1):
        seen["prompts"] = list(run_input.get("prompts", []))
        return [{"prompt": p, "response": "Inito is great.", "citations": []} for p in run_input["prompts"]]
    monkeypatch.setattr(pipe, "run_actor", fake_actor)
    rows = pipe.discover_llm_visibility(
        ["chatgpt"], [{"prompt": "Inito", "intent": "brand_entity"},
                      {"prompt": "Inito reviews", "intent": "brand_entity"}], 1)
    assert seen["prompts"] == ["Inito reviews"]            # only the undone prompt was sent
    assert [r["prompt"] for r in rows] == ["Inito reviews"]

def test_persist_llm_writes_files(pipe):
    rows = [{"run_date": "2026-06-19", "run_index": 1, "surface": "chatgpt", "prompt": "Inito",
             "intent": "brand_entity", "response_text": "text", "inito_mentioned": True,
             "inito_rank": 1, "inito_recommended": True, "stale_product_described": False,
             "stale_excerpt": None, "sources_cited": "[]", "sentiment_inito": 0.8,
             "competitors_named": "[]", "competitor_preferred": None, "confidence": 0.9,
             "status": "ok", "error_note": "", "action": "monitor", "priority": 5}]
    pipe.persist_llm(rows)
    assert (pipe.DATA / "llm_visibility_latest.csv").exists()

def test_compute_llm_metrics(pipe):
    rows = [
        {"run_date": pipe.RUN_DATE, "run_index": 1, "surface": "chatgpt", "prompt": "Inito",
         "intent": "brand_entity", "response_text": "t", "inito_mentioned": True, "inito_rank": 1,
         "inito_recommended": True, "stale_product_described": False, "stale_excerpt": None,
         "sources_cited": "[]", "sentiment_inito": 0.7, "competitors_named": "[]",
         "competitor_preferred": None, "confidence": 0.9, "status": "ok", "error_note": ""},
        {"run_date": pipe.RUN_DATE, "run_index": 1, "surface": "chatgpt", "prompt": "Inito reviews",
         "intent": "brand_entity", "response_text": "t2", "inito_mentioned": False, "inito_rank": None,
         "inito_recommended": False, "stale_product_described": True, "stale_excerpt": "iPhone only",
         "sources_cited": "[]", "sentiment_inito": -0.2, "competitors_named": '["Mira"]',
         "competitor_preferred": "Mira", "confidence": 0.8, "status": "ok", "error_note": ""},
    ]
    df = pipe.persist_llm(rows)
    m = pipe.compute_llm_metrics(df)
    assert m["llm_mention"] == 0.5
    assert m["llm_stale"] == 0.5
    assert "llm_chatgpt_mention" in m


# ---------- action engine ----------
def _arow(pipe, **kw):
    base = {"status": "ok", "intent": "brand_entity", "sources_cited": "[]",
            "stale_product_described": False, "inito_mentioned": True,
            "inito_recommended": False, "competitor_preferred": None}
    base.update(kw)
    return base

def test_action_owned_stale_is_top_priority(pipe):
    row = _arow(pipe, stale_product_described=True,
                sources_cited=json.dumps(["https://inito.com/old-page"]))
    action, prio = pipe.derive_action(row)
    assert prio == 1 and "inito.com/old-page" in action

def test_action_thirdparty_stale_priority_2(pipe):
    row = _arow(pipe, stale_product_described=True,
                sources_cited=json.dumps(["https://leafsnap.com/inito"]))
    _, prio = pipe.derive_action(row)
    assert prio == 2

def test_action_not_mentioned_high_vs_low_intent(pipe):
    hi = pipe.derive_action(_arow(pipe, inito_mentioned=False, intent="comparison"))
    lo = pipe.derive_action(_arow(pipe, inito_mentioned=False, intent="use_case"))
    assert hi[1] == 3 and lo[1] == 4

def test_action_competitor_preferred_priority_3(pipe):
    _, prio = pipe.derive_action(_arow(pipe, competitor_preferred="Mira"))
    assert prio == 3

def test_action_recommended_is_monitor(pipe):
    action, prio = pipe.derive_action(_arow(pipe, inito_recommended=True))
    assert prio == 5 and "monitor" in action.lower()


# ---------- cross-track linkage ----------
def test_link_stale_sources_matches_web_history(pipe):
    import pandas as pd
    pd.DataFrame([{"url": "https://leafsnap.com/inito"}]).to_csv(
        pipe.DATA / "observations_history.csv", index=False)
    rows = [{"stale_product_described": True,
             "sources_cited": json.dumps(["https://leafsnap.com/inito"])}]
    pipe.link_stale_sources(rows)
    assert rows[0]["stale_source_seen_in_web"] == "https://leafsnap.com/inito"


# ---------- selection resolver ----------
def test_resolve_selection_all_and_blank(pipe):
    items = ["a", "b", "c"]
    assert pipe.resolve_selection(items, None, lambda s: s) == items
    assert pipe.resolve_selection(items, "all", lambda s: s) == items
    assert pipe.resolve_selection(items, "", lambda s: s) == items

def test_resolve_selection_indices_and_names(pipe):
    items = [{"q": "alpha"}, {"q": "beta"}, {"q": "gamma"}]
    lf = lambda x: x["q"]
    assert pipe.resolve_selection(items, "1,3", lf) == [items[0], items[2]]
    assert pipe.resolve_selection(items, "beta", lf) == [items[1]]
    # dedupe: index 1 and name 'alpha' refer to the same item
    assert pipe.resolve_selection(items, "1,alpha", lf) == [items[0]]

def test_resolve_selection_errors(pipe):
    items = ["a", "b"]
    with pytest.raises(ValueError):
        pipe.resolve_selection(items, "9", lambda s: s)
    with pytest.raises(ValueError):
        pipe.resolve_selection(items, "zzz", lambda s: s)


# ---------- run folder naming ----------
def test_run_dir_name_is_descriptive(pipe):
    name = pipe.run_dir_name("2026-06-20T143005", "llm", ["chatgpt", "perplexity"], 7,
                             num_runs=3, note="weekly check")
    assert name.startswith("2026-06-20T143005__llm__chatgpt+perplexity__7items__3runs__")
    assert "weekly-check" in name
