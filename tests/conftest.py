"""Stubs network deps before importing pipeline so the whole suite runs offline."""
import sys, types, os, importlib.util, pathlib
import pytest

for name in ("apify_client", "anthropic"):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
sys.modules["apify_client"].ApifyClient = lambda *a, **k: None   # apify client unused in unit tests
sys.modules["anthropic"].Anthropic = lambda *a, **k: None        # forces judge() down its offline fallback
os.environ.setdefault("APIFY_TOKEN", "test")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def pipe(tmp_path, monkeypatch):
    """Fresh pipeline module with DATA pointed at a tmp dir (no writes to repo)."""
    spec = importlib.util.spec_from_file_location("pipeline", ROOT / "pipeline.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "DATA", tmp_path)
    return mod


def mkrow(pipe, url, ownership, status, claims, sentiment=0.0, framing=False,
          intent="brand_entity", rank=1, platform="web"):
    return {
        "url": url, "domain": pipe.domain_of(url), "platform": platform, "query": "q",
        "intent": intent, "rank": rank, "ownership": ownership, "status": status,
        "current_product_named": False,
        "claim_iphone_only": claims.get("iphone_only", False),
        "claim_attach_to_phone": claims.get("attach_to_phone", False),
        "claim_camera_dependent": claims.get("camera_dependent", False),
        "claim_no_android": claims.get("no_android", False),
        "price_mentioned": None, "sentiment_inito": sentiment,
        "competitor_framing": framing, "title": "",
    }
