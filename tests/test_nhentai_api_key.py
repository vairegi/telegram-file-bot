"""v12.54 / v1.26 — private nhentai API key rollout — unit tests.

Pure Python, no network, no Mongo/Turso. Run from repo root:
    python3 tests/test_nhentai_api_key.py
"""
from __future__ import annotations

import importlib
import inspect
import os
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
SB = ROOT / "ScraperBot"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SB))

FAILED = []


def check(name: str, cond: bool) -> None:
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILED.append(name)


def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _DummyClient:
    def __init__(self, *a, **kw): ...
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    def get(self, *a, **kw): raise RuntimeError("network disabled in tests")
    def post(self, *a, **kw): raise RuntimeError("network disabled in tests")


_stub("httpx", AsyncClient=_DummyClient, HTTPError=Exception,
      HTTPStatusError=Exception, get=lambda *a, **k: None,
      Limits=lambda *a, **k: None)

# ScraperBot `app` package stubs (pattern from tests/test_english_only.py)
app_pkg = types.ModuleType("app")
app_pkg.__path__ = [str(SB / "app")]
sys.modules["app"] = app_pkg

settings = types.SimpleNamespace(
    user_agent="test-ua", nhentai_api_key="test-key-123",
    english_only=True, trending_tags_enabled=True, trending_tags_top_n=10,
    trending_tags_refresh_sec=86400, bucket_search=20, bucket_galleries=45,
    bucket_search_scraper=16, bucket_galleries_scraper=36, region_suffix="",
)
cfg = types.ModuleType("app.config"); cfg.settings = settings
sys.modules["app.config"] = cfg
mc = types.ModuleType("app.mongo_client")
mc.state_get = lambda k, d=None: d
mc.state_set = lambda k, v: None
mc.cache_put_mongo = lambda *a, **k: False
sys.modules["app.mongo_client"] = mc
_stub("app.turso_client", put=None)

# miniapp backend package stubs (for prefetch_cron / scraper_bridge / cache)
for pkg, rel in (("miniapp", "miniapp"),
                 ("miniapp.backend", "miniapp/backend"),
                 ("miniapp.backend.app", "miniapp/backend/app"),
                 ("miniapp.backend.app.services", "miniapp/backend/app/services")):
    m = _stub(pkg); m.__path__ = [str(ROOT / rel)]
midb = types.ModuleType("miniapp.backend.app.db")
midb.connect = lambda: None
sys.modules["miniapp.backend.app.db"] = midb

# ---- 1) prefetch_cron._nh_headers -----------------------------------------
os.environ.pop("NHENTAI_API_KEY", None)
pf = importlib.import_module("miniapp.backend.app.services.prefetch_cron")
check("prefetch: no key -> no Authorization", "Authorization" not in pf._nh_headers())
check("prefetch: UA preserved", pf._nh_headers()["User-Agent"] == pf._UA)
os.environ["NHENTAI_API_KEY"] = "test-key-123"
importlib.reload(pf)
check("prefetch: key -> Authorization",
      pf._nh_headers().get("Authorization") == "Key test-key-123")
os.environ.pop("NHENTAI_API_KEY", None)

# ---- 2) scraper_bridge._nh_headers + softened backoff ---------------------
try:
    sb = importlib.import_module("miniapp.backend.app.services.scraper_bridge")
    check("bridge: no key -> no Authorization", "Authorization" not in sb._nh_headers())
    os.environ["NHENTAI_API_KEY"] = "test-key-123"
    check("bridge: key -> Authorization (call-time read)",
          sb._nh_headers().get("Authorization") == "Key test-key-123")
    os.environ.pop("NHENTAI_API_KEY", None)
    check("bridge: backoff base 20s", sb._RATE_LIMIT_TTL_SEC == 20)
    check("bridge: backoff cap 120s", sb._RATE_LIMIT_TTL_CAP_SEC == 120)
except Exception as e:  # noqa: BLE001
    print("FAIL bridge import:", e); FAILED.append("bridge import")

# ---- 3) Bot2Fetcher fetcher — source-level (package-name clash safe) ------
fsrc = (ROOT / "Bot2Fetcher/app/fetcher.py").read_text(encoding="utf-8")
check("fetcher: _nh_headers helper present", "def _nh_headers(" in fsrc)
check("fetcher: key gated on env", 'os.environ.get("NHENTAI_API_KEY"' in fsrc)
check("fetcher: _fetch_meta_direct uses helper",
      "headers = _nh_headers()" in fsrc)
check("fetcher: cover CDN headers untouched (no key leak)",
      "_COVER_HEADERS = {" in fsrc and "Authorization" not in
      fsrc.split("_COVER_HEADERS = {")[1].split("}")[0])

# ---- 4) ScraperBot trending_tags ------------------------------------------
importlib.import_module("app.services.trending_tags")
src = (SB / "app/services/trending_tags.py").read_text(encoding="utf-8")
check("trending_tags: Authorization gated on settings.nhentai_api_key",
      'headers["Authorization"] = f"Key {settings.nhentai_api_key}"' in src)

# ---- 5) nhentai_cache.BUCKETS keyed tier ----------------------------------
nhc = importlib.import_module("miniapp.backend.app.services.nhentai_cache")
check("buckets: search 20", nhc.BUCKETS["search"][0] == 20)
check("buckets: galleries 45", nhc.BUCKETS["galleries"][0] == 45)
check("buckets: galleries_list 30", nhc.BUCKETS["galleries_list"][0] == 30)
check("buckets: popular still 8 (flat tier)", nhc.BUCKETS["popular"][0] == 8)
check("buckets: suggestions still 60 (flat tier)", nhc.BUCKETS["suggestions"][0] == 60)

# ---- 6) ScraperBot config defaults (source-level) -------------------------
csrc = (SB / "app/config.py").read_text(encoding="utf-8")
check("config: BUCKET_SEARCH 20", '_env_int("BUCKET_SEARCH", 20)' in csrc)
check("config: BUCKET_GALLERIES 45", '_env_int("BUCKET_GALLERIES", 45)' in csrc)
check("config: BUCKET_SEARCH_SCRAPER 16", '_env_int("BUCKET_SEARCH_SCRAPER", 16)' in csrc)
check("config: BUCKET_GALLERIES_SCRAPER 36", '_env_int("BUCKET_GALLERIES_SCRAPER", 36)' in csrc)
check("config: LIST_429_SLEEP_CAP_SEC 120", '_env_float("LIST_429_SLEEP_CAP_SEC", 120.0)' in csrc)

# ---- 7) ScraperBot cache.bucket_capacity split -----------------------------
bc = importlib.import_module("app.cache")
check("bucket_capacity: search -> 16", bc.bucket_capacity("search") == 16)
check("bucket_capacity: galleries -> 36", bc.bucket_capacity("galleries") == 36)
check("bucket_capacity: galleries_list -> 30", bc.bucket_capacity("galleries_list") == 30)

print("-" * 60)
if FAILED:
    print(f"FAILED {len(FAILED)}: {FAILED}"); sys.exit(1)
print("ALL CHECKS PASSED")
