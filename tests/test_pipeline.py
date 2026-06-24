"""Offline unit tests for the Inito GEO snapshot pipeline. Run: pytest -q"""
import glob
import pytest

STALE_TEXT = ("Inito is only compatible with iPhone. You attach the monitor to your phone "
              "and it uses your iPhone's camera. Android phones are not supported. $149")
CURRENT_TEXT = ("The new InSight Wireless Reader is Wi-Fi enabled and compatible with both "
                "iOS and Android. $99")
# Shared, unremarkable attributes (hormones + app + dip-strip) must never trip an old-product flag.
NEUTRAL_TEXT = ("Inito measures estrogen, LH, PdG and FSH on one strip and sends results to "
                "the Inito app on your phone. Just dip the strip and read results in 10 minutes.")
COMPETE_TEXT = "Mira is more accurate than Inito and Proov is cheaper. Inito clips to your iPhone."


# ---------- url helpers ----------
def test_normalize_url_strips_www_tracking_fragment_trailing_slash(pipe):
    assert pipe.normalize_url("https://WWW.Inito.com/buy-now/?utm_source=x#frag") == "https://inito.com/buy-now"
    assert pipe.normalize_url("http://inito.com/") == "http://inito.com"

def test_normalize_url_strips_chatgpt_citation_params(pipe):
    # row 8: the cited link carried disc_code / os / workflow / utm_source noise -> canonicalise it
    noisy = "https://www.inito.com/en-us/faqs?disc_code=CORY15&os=android&workflow=ng&utm_source=chatgpt.com"
    assert pipe.normalize_url(noisy) == "https://inito.com/en-us/faqs"

def test_domain_of(pipe):
    assert pipe.domain_of("https://www.miracare.com/mira-vs-inito-comparison/") == "miracare.com"


# ---------- ownership routing (suffix match incl. preprod) ----------
@pytest.mark.parametrize("url,expected", [
    ("https://inito.com/buy-now", "owned"),
    ("https://blog.inito.com/x", "owned"),
    ("https://ng.inito.com/x", "owned"),
    ("https://preprod.inito.com/en-us/faqs", "owned"),   # row 10: was mis-tagged third_party
    ("https://staging.inito.com/x", "owned"),
    ("https://apps.apple.com/us/app/inito-fertility-ovulation/id1183799668", "owned"),
    ("https://apps.apple.com/us/app/something-else/id999", "third_party"),
    ("https://play.google.com/store/apps/details?id=com.inito.insight", "owned"),
    ("https://www.amazon.com/Inito/dp/B0CM17Y1TH", "owned_marketplace"),
    ("https://miracare.com/mira-vs-inito-comparison", "competitor"),
    ("https://shop.miracare.com/x", "competitor"),        # subdomain matches by suffix
    ("https://leafsnap.com/inito-review", "third_party"),
])
def test_ownership_routing(pipe, url, expected):
    assert pipe.ownership(url) == expected

@pytest.mark.parametrize("advertiser,url,expected", [
    ("Inito Inc", "https://example.com/ad", "owned"),
    ("Miracare Inc", "https://example.com/ad", "competitor"),
    ("competitor ad", "https://miracare.com/x", "competitor"),
    ("Some Random Co", "https://leafsnap.com/x", "third_party"),
])
def test_ownership_for_ad(pipe, advertiser, url, expected):
    assert pipe.ownership_for_ad(advertiser, url) == expected

@pytest.mark.parametrize("url,expected", [
    ("https://preprod.inito.com/en-us/faqs", True),
    ("https://staging.inito.com/x", True),
    ("https://dev.inito.com/x", True),
    ("https://blog.inito.com/x", False),
    ("https://inito.com/x", False),
    ("https://miracare.com/x", False),
])
def test_is_nonprod_owned(pipe, url, expected):
    assert pipe.is_nonprod_owned(url) is expected


# ---------- links + competitor detection ----------
def test_extract_links_dedupes_normalises_and_excludes_self(pipe):
    text = ("See https://miracare.com/x?utm_source=a and https://proovtest.com/y "
            "and https://miracare.com/x again, plus https://inito.com/self.")
    links = pipe.extract_links(text, exclude_url="https://inito.com/self")
    assert "https://miracare.com/x" in links
    assert "https://proovtest.com/y" in links
    assert "https://inito.com/self" not in links          # the page's own URL is excluded
    assert links.count("https://miracare.com/x") == 1      # deduped after normalisation

def test_competitors_in(pipe):
    assert set(pipe._competitors_in(COMPETE_TEXT)) >= {"Mira", "Proov"}
    assert pipe._competitors_in(NEUTRAL_TEXT) == []


# ---------- claim detection (price + old-product hints, not-stale guard) ----------
def test_detect_claims_on_stale_text(pipe):
    f = pipe.detect_claims(STALE_TEXT)
    assert f["iphone_only"] and f["attach_to_phone"] and f["camera_dependent"] and f["no_android"]
    assert "$149" in f["prices_seen"]

def test_detect_claims_attach_regression(pipe):
    # regression: "attach the monitor to your phone" is a 21-char gap the old .{0,20} missed
    assert pipe.detect_claims("you attach the monitor to your phone each morning")["attach_to_phone"]

def test_detect_claims_lightning_port(pipe):
    assert pipe.detect_claims("connect via the lightning port to read the strip")["attach_to_phone"]

def test_detect_claims_shared_attributes_not_stale(pipe):
    # hormones + app + dip-strip are common to both products -> no old-product flags (false-positive guard)
    f = pipe.detect_claims(NEUTRAL_TEXT)
    assert not any(f[k] for k in ("iphone_only", "attach_to_phone", "camera_dependent", "no_android"))


# ---------- web judge offline fallback (narrative + competition + price) ----------
def test_judge_web_fallback_flags_old_product(pipe):
    v = pipe.judge("http://x", STALE_TEXT, pipe.detect_claims(STALE_TEXT))
    assert "OLD" in v["says_about_inito"]
    assert v["price_mentioned"] == "$149"
    for key in ("says_about_inito", "mentions_competition", "competitors_named", "sentiment_inito"):
        assert key in v

def test_judge_web_fallback_neutral_mentions_inito(pipe):
    v = pipe.judge("http://n", NEUTRAL_TEXT, pipe.detect_claims(NEUTRAL_TEXT))
    assert v["says_about_inito"] == "Mentions Inito."
    assert v["mentions_competition"] is False

def test_judge_web_fallback_detects_competition(pipe):
    v = pipe.judge("http://c", COMPETE_TEXT, pipe.detect_claims(COMPETE_TEXT))
    assert v["mentions_competition"] is True
    assert set(v["competitors_named"]) >= {"Mira", "Proov"}


# ---------- classify a web record into a sheet row ----------
def test_classify_web_record_shape(pipe):
    rec = {"url": "https://leafsnap.com/inito-review", "platform": "web", "query": "Inito review",
           "intent": "brand_entity", "topic_id": "brand_reviews", "title": "t", "snippet": ""}
    row = pipe.classify_web_record(rec, COMPETE_TEXT)
    assert set(pipe.WEB_COLUMNS) <= set(row)          # every output column is present
    assert row["source"] == "web"
    assert row["ownership"] == "third_party"
    assert row["mentions_competition"] is True
    assert "Mira" in row["competitors_named"]
    assert row["nonprod_url"] is False

def test_classify_web_record_flags_nonprod(pipe):
    rec = {"url": "https://preprod.inito.com/en-us/faqs", "platform": "web", "query": "q",
           "intent": "brand_entity", "topic_id": "x", "title": "", "snippet": ""}
    row = pipe.classify_web_record(rec, NEUTRAL_TEXT)
    assert row["ownership"] == "owned" and row["nonprod_url"] is True

def test_write_web_sheet(pipe):
    import pandas as pd
    rec = {"url": "https://leafsnap.com/x", "platform": "web", "query": "q", "intent": "brand_entity",
           "topic_id": "x", "title": "", "snippet": ""}
    out = pipe.DATA / "run"; out.mkdir()
    pipe.write_web_sheet([pipe.classify_web_record(rec, NEUTRAL_TEXT)], out)
    df = pd.read_csv(out / "web_observations.csv")
    assert list(df.columns) == pipe.WEB_COLUMNS


# ---------- SERP parsing (guards the searchQuery dict/str fix + topic_id threading) ----------
def test_serp_parsing_handles_dict_and_string_query(pipe, monkeypatch):
    fake = [
        {"searchQuery": {"term": "Inito vs Mira"},
         "organicResults": [{"url": "https://miracare.com/x", "title": "t", "description": "d"}],
         "aiOverview": "Inito is iPhone only"},
        {"searchQuery": "Inito reviews",
         "organicResults": [{"url": "https://leafsnap.com/inito-review"}]},
    ]
    monkeypatch.setattr(pipe, "run_actor", lambda *a, **k: fake)
    rows = pipe.discover_serp()
    urls = [r["url"] for r in rows]
    assert "https://miracare.com/x" in urls
    assert any(u.startswith("aioverview::") for u in urls)
    by_q = {r["query"]: r["intent"] for r in rows}
    assert by_q["Inito vs Mira"] == "comparison"
    assert by_q["Inito reviews"] == "brand_entity"

def test_topic_id_threaded_web(pipe, monkeypatch):
    fake = [{"searchQuery": {"term": "Inito vs Mira"},
             "organicResults": [{"url": "https://miracare.com/x", "title": "t", "description": "d"}]}]
    monkeypatch.setattr(pipe, "run_actor", lambda *a, **k: fake)
    rows = pipe.discover_serp()
    assert rows[0]["topic_id"] == "cmp_mira"


# ---------- ads discovery ----------
def test_discover_ads_parses_and_tags_ownership(pipe, monkeypatch):
    monkeypatch.setitem(pipe.CFG, "ads_start_urls", ["https://adstransparency.google.com/advertiser/X?region=US"])
    fake = [{"advertiserName": "Inito", "creativeId": "c1", "format": "TEXT",
             "url": "https://inito.com/lp",
             "variants": [{"text": "Inito clips to your iPhone camera"}]}]
    monkeypatch.setattr(pipe, "run_actor", lambda *a, **k: fake)
    rows = pipe.discover_ads()
    assert len(rows) == 1 and rows[0]["platform"] == "ads" and rows[0]["ownership"] == "owned"
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


# ---------- LLM visibility ----------
def test_judge_llm_response_fallback(pipe):
    v = pipe.judge_llm_response("Inito reviews", "chatgpt", "Inito is a great fertility monitor.")
    assert isinstance(v["inito_mentioned"], bool)
    assert "says_about_inito" in v and "sources_cited" in v

def test_judge_llm_fallback_competition_and_old_product(pipe):
    v = pipe.judge_llm_response("Inito vs Mira", "chatgpt", COMPETE_TEXT)
    assert v["mentions_competition"] is True and "Mira" in v["competitors_named"]
    assert "OLD" in v["says_about_inito"]

def test_discover_llm_visibility_runs_surface(pipe, monkeypatch):
    fake_items = [
        {"prompt": "Inito", "response": "Inito is a good product.", "citations": []},
        {"prompt": "Inito reviews", "response": "Inito has positive reviews.", "citations": []},
    ]
    monkeypatch.setattr(pipe, "run_actor", lambda *a, **k: fake_items)
    rows = pipe.discover_llm_visibility(["chatgpt"], [{"prompt": "Inito", "intent": "brand_entity"},
                                                      {"prompt": "Inito reviews", "intent": "brand_entity"}], 1)
    assert len(rows) == 2
    assert rows[0]["surface"] == "chatgpt" and "mentioned" in rows[0]
    assert set(pipe.LLM_COLUMNS) <= set(rows[0])

def test_discover_llm_visibility_multiple_runs(pipe, monkeypatch):
    # one row per (prompt × run) — no aggregation
    monkeypatch.setattr(pipe, "run_actor",
                        lambda *a, **k: [{"prompt": "Inito", "response": "Inito is great.", "citations": []}])
    rows = pipe.discover_llm_visibility(["chatgpt"], [{"prompt": "Inito", "intent": "brand_entity"}], 3)
    assert sorted(r["run"] for r in rows) == [1, 2, 3]

def test_llm_row_empty_response_is_not_judged(pipe):
    # regression: an empty actor response must NOT be sent to the judge (it fabricates signals).
    row = pipe._llm_row(1, "perplexity", "Inito vs Mira", "comparison", "   ")
    assert row["status"] == "empty"
    assert row["mentioned"] is None and row["says_about_inito"] == ""

def test_llm_row_canonicalises_and_flags_nonprod_sources(pipe):
    row = pipe._llm_row(1, "chatgpt", "Inito", "brand_entity",
                        "Inito is great. See https://inito.com/a?utm_source=chatgpt.com",
                        extra_sources=["https://preprod.inito.com/faqs?os=android",
                                       "https://inito.com/a"])  # dup of the inline link after normalising
    srcs = row["sources_cited"].split(", ")
    assert "https://inito.com/a" in srcs
    assert "https://preprod.inito.com/faqs" in srcs          # tracking params stripped
    assert srcs.count("https://inito.com/a") == 1            # deduped
    assert row["nonprod_url"] is True                         # a preprod source was cited

def test_run_perplexity_uses_sonar_api(pipe, monkeypatch):
    monkeypatch.setattr(pipe, "PPLX_KEY", "pplx-test")
    monkeypatch.setattr(pipe, "perplexity_complete",
                        lambda prompt, model: ("Inito is a fertility monitor.", ["https://inito.com/"]))
    rows = pipe._run_perplexity(1, [{"prompt": "Inito", "intent": "brand_entity"}])
    assert len(rows) == 1 and rows[0]["surface"] == "perplexity" and rows[0]["status"] == "ok"
    assert "https://inito.com" in rows[0]["sources_cited"]

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
    assert len(rows) == 1 and rows[0]["status"] == "error"
    assert "actor exploded" in rows[0]["error_note"]

def test_write_llm_sheet(pipe, monkeypatch):
    import pandas as pd
    monkeypatch.setattr(pipe, "run_actor",
                        lambda *a, **k: [{"prompt": "Inito", "response": "Inito is great.", "citations": []}])
    rows = pipe.discover_llm_visibility(["chatgpt"], [{"prompt": "Inito", "intent": "brand_entity"}], 1)
    out = pipe.DATA / "run"; out.mkdir()
    pipe.write_llm_sheet(rows, out)
    df = pd.read_csv(out / "llm_observations.csv")
    assert list(df.columns) == pipe.LLM_COLUMNS


# ---------- topic catalog ----------
def test_topics_same_set_both_tracks(pipe):
    topics = pipe.CFG["topics"]
    web, llm = pipe.web_topics(), pipe.llm_topics()
    assert len(web) == len(llm) == len(topics)
    assert {t["id"] for t in web} == {t["id"] for t in llm} == {t["id"] for t in topics}

def test_web_and_llm_queries_are_identical(pipe):
    # the unification guarantee: both tracks send the exact same string per topic
    web, llm = pipe.web_topics(), pipe.llm_topics()
    assert [t["q"] for t in web] == [p["prompt"] for p in llm]
    # and each row carries the config `query` verbatim
    for t, w in zip(pipe.CFG["topics"], web):
        assert w["q"] == t["query"]

def test_list_topics_smoke(pipe, capsys):
    pipe.list_topics()
    out = capsys.readouterr().out
    assert "brand_head" in out and "topics" in out


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
    assert pipe.resolve_selection(items, "1,alpha", lf) == [items[0]]   # dedupe

def test_resolve_selection_errors(pipe):
    items = ["a", "b"]
    with pytest.raises(ValueError):
        pipe.resolve_selection(items, "9", lambda s: s)
    with pytest.raises(ValueError):
        pipe.resolve_selection(items, "zzz", lambda s: s)


# ---------- ad-hoc one-off prompts (--extra-prompts) ----------
def test_parse_extra_prompts_empty(pipe):
    assert pipe.parse_extra_prompts(None) == []
    assert pipe.parse_extra_prompts("  ;  ; ") == []

def test_parse_extra_prompts_default_intent(pipe):
    assert pipe.parse_extra_prompts("Inito vs Oova") == [{"prompt": "Inito vs Oova", "intent": "adhoc"}]

def test_parse_extra_prompts_explicit_intent_and_separation(pipe):
    out = pipe.parse_extra_prompts("Inito vs Oova::comparison; Is Inito legit?::purchase")
    assert out == [{"prompt": "Inito vs Oova", "intent": "comparison"},
                   {"prompt": "Is Inito legit?", "intent": "purchase"}]

def test_parse_extra_prompts_dedupes_within_spec(pipe):
    assert pipe.parse_extra_prompts("Inito vs Oova; Inito vs Oova::comparison") == [
        {"prompt": "Inito vs Oova", "intent": "adhoc"}]


# ---------- run folder naming + cleanup ----------
def test_run_dir_name_is_descriptive(pipe):
    name = pipe.run_dir_name("2026-06-20T143005", "llm", ["chatgpt", "perplexity"], 7,
                             num_runs=3, note="weekly check")
    assert name.startswith("2026-06-20T143005__llm__chatgpt+perplexity__7items__3runs__")
    assert "weekly-check" in name

def test_cleanup_empty_removes_only_empty_dir(pipe, tmp_path):
    empty = tmp_path / "empty_run"; empty.mkdir()
    full = tmp_path / "full_run"; full.mkdir(); (full / "x.csv").write_text("a")
    pipe._cleanup_empty(empty)
    pipe._cleanup_empty(full)
    assert not empty.exists() and full.exists()


# ---------- end-to-end via the CLI ----------
def test_cli_llm_extra_prompts_writes_snapshot_and_dedupes(pipe, monkeypatch):
    import pandas as pd
    sent = []
    def fake_actor(actor_id, run_input, label):
        sent.extend(run_input.get("prompts", []))
        return [{"prompt": p, "response": "Inito is great. Worth it.", "citations": []}
                for p in run_input["prompts"]]
    monkeypatch.setattr(pipe, "run_actor", fake_actor)
    # config topic 1 ("Inito fertility monitor") + one ad-hoc + a dup of topic 1 that must be dropped
    pipe.main(["--llm", "--surfaces", "chatgpt", "--prompts", "1",
               "--extra-prompts", "Inito vs Oova::comparison; Inito fertility monitor", "--num-runs", "1", "-y"])
    sheets = glob.glob(str(pipe.DATA / "*__llm__*" / "llm_observations.csv"))
    assert len(sheets) == 1
    df = pd.read_csv(sheets[0])
    assert sorted(df["prompt"].unique().tolist()) == ["Inito fertility monitor", "Inito vs Oova"]
    assert sent.count("Inito fertility monitor") == 1     # the duplicate was not sent twice
    assert df[df["prompt"] == "Inito vs Oova"].iloc[0]["intent"] == "comparison"


# ---------- helpers added in the lean rewrite ----------
def test_host_matches_suffix(pipe):
    assert pipe._host_matches("preprod.inito.com", ["inito.com"])
    assert pipe._host_matches("inito.com", ["inito.com"])
    assert not pipe._host_matches("eviinito.com", ["inito.com"])   # not a real subdomain (no dot boundary)
    assert not pipe._host_matches("notinito.com", ["inito.com"])

def test_describes_old_product(pipe):
    assert pipe._describes_old_product(pipe.detect_claims(STALE_TEXT))
    assert not pipe._describes_old_product(pipe.detect_claims(NEUTRAL_TEXT))

def test_normalize_url_pseudo_url_is_idempotent(pipe):
    # pseudo-URLs (aioverview::<query>) flow through dedupe's normalize_url — must not crash / mutate apart
    pseudo = "aioverview::Inito vs Mira"
    once = pipe.normalize_url(pseudo)
    assert isinstance(once, str) and once and pipe.normalize_url(once) == once

def test_ownership_amazon_non_dp_is_third_party(pipe):
    assert pipe.ownership("https://www.amazon.com/s?k=inito") == "third_party"

def test_extract_links_caps_at_20(pipe):
    text = " ".join(f"https://site{i}.com/page" for i in range(30))
    assert len(pipe.extract_links(text)) == 20


# ---------- real (non-fallback) judge tool-block path ----------
def test_judge_uses_tool_block_when_claude_available(pipe, monkeypatch):
    class _Block:
        type = "tool_use"
        input = {"says_about_inito": "Current wireless reader.", "mentions_competition": False,
                 "competition_summary": None, "competitors_named": [], "sentiment_inito": 0.5,
                 "price_mentioned": "$99"}
    class _Resp: content = [_Block()]
    monkeypatch.setattr(pipe, "claude",
                        type("C", (), {"messages": type("M", (), {"create": lambda self, **k: _Resp()})()})())
    v = pipe.judge("http://x", CURRENT_TEXT, pipe.detect_claims(CURRENT_TEXT))
    assert v["says_about_inito"] == "Current wireless reader." and v["price_mentioned"] == "$99"
    assert "_fallback" not in v                                    # took the real path, not the fallback

def test_judge_llm_uses_tool_block_when_claude_available(pipe, monkeypatch):
    class _Block:
        type = "tool_use"
        input = {"inito_mentioned": True, "inito_rank": 2, "inito_recommended": True,
                 "says_about_inito": "Recommends Inito.", "mentions_competition": True,
                 "competition_summary": "vs Mira", "competitors_named": ["Mira"],
                 "sentiment_inito": 0.6, "price_mentioned": None, "sources_cited": ["https://inito.com/"]}
    class _Resp: content = [_Block()]
    monkeypatch.setattr(pipe, "claude",
                        type("C", (), {"messages": type("M", (), {"create": lambda self, **k: _Resp()})()})())
    row = pipe._llm_row(1, "chatgpt", "best monitor", "category", "Inito is best. Mira is second.")
    assert row["recommended"] is True and row["rank"] == 2 and "Mira" in row["competitors_named"]
    assert row["says_about_inito"] == "Recommends Inito."


# ---------- full Track A orchestration (refresh) ----------
def test_refresh_end_to_end_writes_web_sheet(pipe, monkeypatch):
    import pandas as pd
    def fake_actor(actor_id, run_input, label):
        if label == "serp":
            return [{"searchQuery": "Inito review",
                     "organicResults": [{"url": "https://leafsnap.com/inito-review", "title": "R",
                                         "description": "review"},
                                        {"url": "https://preprod.inito.com/faqs?utm_source=x", "title": "FAQ",
                                         "description": "faq"}]}]
        if label == "content":
            return [{"url": pipe.normalize_url(su["url"]),
                     "text": COMPETE_TEXT if "leafsnap" in su["url"] else CURRENT_TEXT}
                    for su in run_input["startUrls"]]
        return []
    monkeypatch.setattr(pipe, "run_actor", fake_actor)
    out = pipe.DATA / "run"; out.mkdir()
    pipe.refresh(["serp"], [{"q": "Inito review", "intent": "brand_entity", "id": "brand_reviews"}], out)
    df = pd.read_csv(out / "web_observations.csv")
    assert list(df.columns) == pipe.WEB_COLUMNS
    by_url = {r["url"]: r for _, r in df.iterrows()}
    assert by_url["https://leafsnap.com/inito-review"]["mentions_competition"] == True
    pre = by_url["https://preprod.inito.com/faqs"]
    assert pre["ownership"] == "owned" and pre["nonprod_url"] == True   # preprod flagged, params stripped

def test_refresh_empty_discovery_leaves_no_orphan_folder(pipe, monkeypatch):
    monkeypatch.setattr(pipe, "run_actor", lambda *a, **k: [])
    out = pipe.DATA / "empty_run"; out.mkdir()
    pipe.refresh(["serp"], [{"q": "Inito review", "intent": "brand_entity", "id": "x"}], out)
    assert not out.exists()                                          # cleaned up, no empty folder left

def test_run_llm_visibility_writes_sheet(pipe, monkeypatch):
    import pandas as pd
    monkeypatch.setattr(pipe, "run_actor",
                        lambda *a, **k: [{"prompt": "Inito", "response": "Inito is great.", "citations": []}])
    out = pipe.DATA / "llm_run"; out.mkdir()
    pipe.run_llm_visibility(["chatgpt"], [{"prompt": "Inito", "intent": "brand_entity", "id": "brand_head"}], 2, out)
    df = pd.read_csv(out / "llm_observations.csv")
    assert list(df.columns) == pipe.LLM_COLUMNS and len(df) == 2     # one row per run
