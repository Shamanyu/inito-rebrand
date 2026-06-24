"""Stubs network deps before importing pipeline so the whole suite runs offline."""
import sys, types, os, importlib.util, pathlib
import pytest

for name in ("apify_client", "anthropic"):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
sys.modules["apify_client"].ApifyClient = lambda *a, **k: None   # apify client unused in unit tests
sys.modules["anthropic"].Anthropic = lambda *a, **k: None        # forces judges down their offline fallback
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
    # point fetch cache path into tmp dir so tests don't touch real cache
    monkeypatch.setattr(mod, "FETCH_CACHE_PATH", tmp_path / "fetch_cache.csv")
    return mod
