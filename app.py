#!/usr/bin/env python3
# E-commerce Simulator: Pixel buttons, /ingest forwarder, server auto streamer,
# Presets + seed, diff viewer, dedup meter, discrepancy & chaos toggles,
# Margin (price - random cost) + PLTV values, currency control, file + webhook + GA4 sinks,
# Event console, scenario runner, clock skew, health/version.
import os, json, hashlib, time, threading, uuid, math, random
from datetime import datetime, timezone
from collections import deque, defaultdict
from typing import Any, Dict, List
from flask import Flask, request, Response, jsonify, has_request_context
import requests
from dotenv import load_dotenv

# -------------------- Config & constants --------------------
load_dotenv()

PIXEL_ID        = os.getenv("PIXEL_ID", "")
ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN", "")
TEST_EVENT_CODE = os.getenv("TEST_EVENT_CODE", "")
BASE_URL        = os.getenv("BASE_URL", "http://127.0.0.1:5000")
GRAPH_VER       = os.getenv("GRAPH_VER", "v20.0")
CAPI_URL        = f"https://graph.facebook.com/{GRAPH_VER}/{PIXEL_ID}/events" if PIXEL_ID else None

GA4_MEASUREMENT_ID = os.getenv("GA4_MEASUREMENT_ID", "")
GA4_API_SECRET     = os.getenv("GA4_API_SECRET", "")
GA4_URL            = f"https://www.google-analytics.com/mp/collect?measurement_id={GA4_MEASUREMENT_ID}&api_secret={GA4_API_SECRET}" if (GA4_MEASUREMENT_ID and GA4_API_SECRET) else None

WEBHOOK_URL     = os.getenv("WEBHOOK_URL", "")
try:
    WEBHOOK_HEADERS = json.loads(os.getenv("WEBHOOK_HEADERS","{}") or "{}")
except Exception:
    WEBHOOK_HEADERS = {}

FILE_SINK_PATH  = os.getenv("FILE_SINK_PATH", "")  # e.g. "events.ndjson"

APP_VERSION = "2.1.2"

app = Flask(__name__)

# -------------------- Thread-safe runtime config --------------------
CONFIG_LOCK = threading.Lock()
CONFIG: Dict[str, Any] = {
    # traffic/simulator
    "rps": 0.5,
    "p_add_to_cart": 0.35,
    "p_begin_checkout": 0.70,
    "p_purchase": 0.70,

    # catalog & pricing
    "product_catalog_size": 200,
    "price_min": 10.0,
    "price_max": 120.0,

    # currency control
    "currency_override": "AUTO",  # AUTO | NULL | USD/EUR/...

    # order economics
    "free_shipping_threshold": 75.0,
    "shipping_options": [4.99, 6.99, 9.99],
    "tax_rate": 0.08,

    # signals master switches
    "enable_pixel": True,
    "enable_capi": True,

    # bad-data toggles
    "null_price": False,
    "null_currency": False,
    "null_event_id": False,

    # margin + PLTV
    "append_margin": True,
    "cost_pct_min": 0.40,     # fraction 0..1
    "cost_pct_max": 0.80,     # fraction 0..1
    "append_pltv": True,
    "pltv_min": 120.0,
    "pltv_max": 600.0,

    # seeded randomness
    "seed": "",  # when set, RNG is seeded

    # discrepancy toggles
    "mismatch_value_pct": 0.0,        # e.g. 0.15 -> ±15% applied to Pixel when >0
    "mismatch_currency": "NONE",      # NONE | PIXEL | CAPI
    "desync_event_id": False,         # pixel uses a different event_id than capi
    "duplicate_event_id_n": 0,        # 0 disables; else duplicate every Nth Pixel event_id
    "drop_pixel_every_n": 0,          # drop every Nth Pixel event
    "lag_capi_seconds": 0.0,          # delay before sending to CAPI (simulate network lag)

    # chaos
    "net_capi_latency_ms": 0,         # artificial latency (per request)
    "net_capi_error_rate": 0.0,       # 0..1 force HTTP error (simulate)
    "schema_remove_contents": False,
    "schema_empty_arrays": False,
    "schema_str_numbers": False,      # send numbers as strings
    "schema_unknown_fields": False,
    "clock_skew_seconds": 0,          # ± seconds added to event_time
    "kill_event_types": {             # master kill per type
        "PageView": False, "ViewContent": False, "AddToCart": False,
        "InitiateCheckout": False, "Purchase": False, "ReturnInitiated": False
    },

    # sinks
    "enable_webhook": False,
    "enable_ga4": False,

    # presets name (display only)
    "active_preset": "Default"
}

# seeded RNG
_rng = random.Random()
def reseed():
    with CONFIG_LOCK:
        seed = (CONFIG.get("seed") or "").strip()
    if seed:
        _rng.seed(seed)
    else:
        _rng.seed()  # system time
reseed()

# -------------------- Telemetry & storage --------------------
EVENT_LOG_MAX = 800
EVENT_LOG: deque = deque(maxlen=EVENT_LOG_MAX)  # dicts: {ts, channel, event_name, intended, sent, response, ok, event_id, diff}
COUNTS = defaultdict(int)
DEDUP = {"pixel_ids": set(), "capi_ids": set(), "matched": 0, "pixel_only": 0, "capi_only": 0}
METRICS_LOCK = threading.Lock()

def _log_event(entry: Dict[str,Any]):
    with METRICS_LOCK:
        EVENT_LOG.appendleft(entry)
        ch = entry.get("channel","")
        name = entry.get("event_name","")
        ok = entry.get("ok", False)
        COUNTS[f"sent_{ch}"] += 1
        COUNTS[f"sent_{ch}_{name}"] += 1
        if not ok:
            COUNTS["errors"] += 1
        eid = (entry.get("event_id") or "")
        if eid:
            if ch == "pixel":
                DEDUP["pixel_ids"].add(eid)
            elif ch == "capi":
                DEDUP["capi_ids"].add(eid)
            common = DEDUP["pixel_ids"].intersection(DEDUP["capi_ids"])
            DEDUP["matched"] = len(common)
            DEDUP["pixel_only"] = len(DEDUP["pixel_ids"] - common)
            DEDUP["capi_only"]  = len(DEDUP["capi_ids"] - common)

def _ndjson_append(row: Dict[str,Any]):
    if not FILE_SINK_PATH:
        return
    try:
        with open(FILE_SINK_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass

# -------------------- Utils --------------------
def sha256_norm(s):
    norm = (s or "").strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()

def iso_to_unix(ts_iso):
    dt = datetime.fromisoformat(ts_iso.replace("Z","+00:00"))
    return int(dt.replace(tzinfo=timezone.utc).timestamp())

def get_cfg_snapshot():
    with CONFIG_LOCK:
        return json.loads(json.dumps(CONFIG))  # deep copy

def clampf(v, lo, hi, default):
    try: x = float(v)
    except Exception: return default
    return max(lo, min(hi, x))

def clampp(v, lo, hi, default):
    try: x = int(v)
    except Exception: return default
    return max(lo, min(hi, x))

def to_bool(v, default=False):
    if isinstance(v, bool): return v
    if isinstance(v, (int,float)): return v != 0
    if isinstance(v, str): return v.strip().lower() in ("1","true","t","yes","y","on")
    return default

def rand_choice(seq):
    return _rng.choice(seq)

def rand_uniform(a,b):
    lo, hi = (a,b) if a <= b else (b,a)
    return _rng.random() * (hi - lo) + lo

def rand_triangular(low, high, mode=None):
    """Triangular distribution for prices."""
    if mode is None:
        mode = (low + high) / 2
    u = _rng.random()
    c = (mode - low) / (high - low)
    if u < c:
        return low + math.sqrt(u * (high - low) * (mode - low))
    else:
        return high - math.sqrt((1 - u) * (high - low) * (high - mode))

def now_iso():
    return datetime.now(tz=timezone.utc).isoformat()

# -------------------- Economics helpers --------------------
def _rand_cost(price, cfg):
    try:
        p = float(price)
    except Exception:
        return None
    lo = max(0.0, float(cfg.get("cost_pct_min", 0.4)))
    hi = max(lo, float(cfg.get("cost_pct_max", 0.8)))
    pct = rand_uniform(lo, hi)
    return max(0.0, round(p * pct, 2))

def _margin_from_contents(contents, cfg):
    total = 0.0
    for c in contents or []:
        try:
            price = float(c.get("item_price"))
            qty   = float(c.get("quantity", 1))
        except Exception:
            continue
        cost = _rand_cost(price, cfg)
        if cost is None: 
            continue
        total += max(0.0, price - cost) * max(0.0, qty)
    return round(total, 2)

def append_margin_pltv(custom_data, cfg, single_price=None, contents=None):
    if custom_data is None: custom_data = {}
    if cfg.get("append_margin"):
        margin_val = None
        if contents:
            margin_val = _margin_from_contents(contents, cfg)
        elif single_price is not None:
            try:
                price = float(single_price); cost = _rand_cost(price, cfg)
                if cost is not None:
                    margin_val = round(max(0.0, price - cost), 2)
            except Exception:
                pass
        if margin_val is not None:
            custom_data["margin"] = margin_val
    if cfg.get("append_pltv"):
        lo = float(cfg.get("pltv_min", 0.0))
        hi = float(cfg.get("pltv_max", max(lo, 0.0)))
        if hi < lo: hi = lo
        custom_data["predicted_ltv"] = round(rand_uniform(lo, hi), 2)
    return custom_data

# -------------------- Mapping sim → CAPI/GA4 helpers --------------------
def _apply_currency_override(cur, cfg, channel="capi"):
    ov = (cfg.get("currency_override") or "AUTO").upper()
    mm = (cfg.get("mismatch_currency") or "NONE").upper()
    if mm == "PIXEL" and channel == "pixel":
        return None if cur else "USD"
    if mm == "CAPI" and channel == "capi":
        return None if cur else "USD"
    if ov == "NULL":
        return None
    elif ov != "AUTO":
        return ov
    return cur

def _maybe_str_nums(obj, cfg):
    if not cfg.get("schema_str_numbers"): return obj
    def t(v):
        if isinstance(v, (int, float)): return str(v)
        if isinstance(v, list): return [t(x) for x in v]
        if isinstance(v, dict): return {k: t(x) for k,x in v.items()}
        return v
    return t(obj)

def _schema_mutations(custom_data, cfg):
    if not custom_data: return custom_data
    cd = dict(custom_data)
    if cfg.get("schema_remove_contents"):
        cd.pop("contents", None)
    if cfg.get("schema_empty_arrays"):
        if "contents" in cd: cd["contents"] = []
        if "content_ids" in cd: cd["content_ids"] = []
    if cfg.get("schema_unknown_fields"):
        cd["unknown_field_xyz"] = "foo"
    return cd

def _apply_clock_skew(ts_unix, cfg):
    skew = int(cfg.get("clock_skew_seconds", 0))
    return int(ts_unix + skew)

# -------------------- Posting sinks --------------------
def capi_post(server_events, cfg):
    if not cfg.get("enable_capi"):
        return {"skipped": True, "reason": "enable_capi=false"}
    if not CAPI_URL or not ACCESS_TOKEN:
        return {"skipped": True, "reason": "missing_capi_config"}

    lat = int(cfg.get("net_capi_latency_ms", 0))
    if lat > 0: time.sleep(lat/1000.0)
    if rand_uniform(0.0, 1.0) < float(cfg.get("net_capi_error_rate", 0.0)):
        class Dummy: status_code=503; text="Simulated upstream error"
        raise requests.HTTPError("503 Simulated", response=Dummy())

    payload = {"data": server_events}
    if TEST_EVENT_CODE:
        payload["test_event_code"] = TEST_EVENT_CODE
    r = requests.post(
        CAPI_URL,
        params={"access_token": ACCESS_TOKEN},
        json=payload,
        timeout=10
    )
    r.raise_for_status()
    return r.json()

def webhook_post(server_events, cfg):
    if not cfg.get("enable_webhook"): return {"skipped": True}
    if not WEBHOOK_URL: return {"skipped": True, "reason":"missing_webhook_url"}
    try:
        r = requests.post(WEBHOOK_URL, headers=WEBHOOK_HEADERS, json={"events": server_events}, timeout=10)
        return {"status": r.status_code, "ok": r.ok}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def ga4_post(mapped_events: List[Dict[str,Any]], cfg):
    if not cfg.get("enable_ga4"): return {"skipped": True}
    if not GA4_URL: return {"skipped": True, "reason":"missing_ga4_config"}
    body = {"client_id": str(uuid.uuid4()), "events": mapped_events}
    try:
        r = requests.post(GA4_URL, json=body, timeout=10)
        return {"status": r.status_code, "ok": r.ok, "text": r.text[:300]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# -------------------- Sim mapping --------------------
_ALLOWED_CURRENCIES = {"AUTO","NULL","USD","EUR","GBP","AUD","CAD","JPY"}

DEVICES   = ["mobile","mobile","desktop","tablet"]
SOURCES   = ["direct","seo","sem","email","social","referral"]
COUNTRIES = ["US","US","US","CA","GB","DE","AU"]
CURRENCIES= ["USD","USD","USD","EUR","GBP","AUD","CAD","JPY"]
CATS      = ["Tops","Bottoms","Shoes","Accessories","Home","Outerwear"]

def _uid(): return str(uuid.uuid4())

def _make_session(cfg):
    return {
        "user_id": f"u_{_rng.randint(1, 9_999_999)}",
        "session_id": _uid(),
        "device": rand_choice(DEVICES),
        "country": rand_choice(COUNTRIES),
        "source": rand_choice(SOURCES),
        "utm_campaign": rand_choice(["brand","retargeting","newsletter","new_arrivals",""]),
        "currency": rand_choice(CURRENCIES),
        "store_id": "store-001",
    }

def _make_product(cfg):
    base = 10000
    size = max(1, int(cfg.get("product_catalog_size", 200)))
    n = base + _rng.randint(0, size - 1)
    lo, hi = float(cfg["price_min"]), max(float(cfg["price_min"])+0.01, float(cfg["price_max"]))
    price = round(rand_triangular(lo, hi, (lo*0.7 + hi*0.3)), 2)
    cat = rand_choice(CATS)
    return {"product_id": f"SKU-{n}", "name": f"{cat} {n}", "category": cat, "price": price}

def build_contents(lines):
    return [{"id": li["product_id"], "quantity": int(li["qty"]), "item_price": float(li["price"])} for li in (lines or [])]

def contents_subtotal(contents):
    s = 0.0
    for c in contents or []:
        try: s += float(c.get("quantity",0)) * float(c.get("item_price",0))
        except Exception: pass
    return round(s,2)

def map_sim_event_to_capi(e, cfg):
    et   = e.get("event_type")
    sess = e.get("user", {})
    ctx  = e.get("context", {})

    cur = ctx.get("currency","USD")
    cur = _apply_currency_override(cur, cfg, channel="capi")

    ts_unix = _apply_clock_skew(iso_to_unix(e["timestamp"]), cfg)

    page = e.get("page") or "/"
    if et == "product_view" and "product" in e:
        page = f"/product/{e['product']['product_id']}"
    if et in ("add_to_cart","begin_checkout","purchase"):
        page = "/checkout"
    event_source_url = BASE_URL.rstrip("/") + page

    client_ip = request.remote_addr if has_request_context() else "127.0.0.1"
    user_agent = (request.headers.get("User-Agent","AutoRunner/1.0") if has_request_context()
                  else "AutoRunner/1.0")

    base = {
        "event_time": ts_unix,
        "event_id": e.get("event_id"),
        "action_source": "website",
        "event_source_url": event_source_url,
        "user_data": {
            "external_id": sha256_norm(sess.get("user_id","")),
            "client_ip_address": client_ip,
            "client_user_agent": user_agent
        }
    }

    out = []
    if et == "page_view":
        out = [{**base, "event_name":"PageView"}]

    elif et == "product_view":
        p = e["product"]
        cd = {
            "content_type":"product",
            "content_ids":[p["product_id"]],
            "value": float(p["price"]),
            "currency": cur
        }
        cd = append_margin_pltv(cd, cfg, single_price=cd["value"], contents=None)
        cd = _schema_mutations(cd, cfg)
        out = [{**base, "event_name":"ViewContent", "custom_data": cd}]

    elif et == "add_to_cart":
        li = e["line_item"]
        contents = [{"id": li["product_id"], "quantity": int(li["qty"]), "item_price": float(li["price"])}]
        cd = {
            "content_type":"product",
            "content_ids":[li["product_id"]],
            "contents": contents,
            "value": float(li["qty"]) * float(li["price"]),
            "currency": cur
        }
        cd = append_margin_pltv(cd, cfg, single_price=None, contents=contents)
        cd = _schema_mutations(cd, cfg)
        out = [{**base, "event_name":"AddToCart", "custom_data": cd}]

    elif et == "begin_checkout":
        cart = e["cart"]
        contents = build_contents(cart)
        total = float(e.get("total", 0.0))
        cd = {"contents": contents, "value": total, "currency": cur}
        cd = append_margin_pltv(cd, cfg, single_price=None, contents=contents)
        cd = _schema_mutations(cd, cfg)
        out = [{**base, "event_name":"InitiateCheckout", "custom_data": cd}]

    elif et == "purchase":
        items = e["items"]
        contents = build_contents(items)
        total = float(e.get("total", 0.0))
        cd = {"contents": contents, "value": total, "currency": cur}
        cd = append_margin_pltv(cd, cfg, single_price=None, contents=contents)
        cd = _schema_mutations(cd, cfg)
        out = [{**base, "event_name":"Purchase", "custom_data": cd}]

    elif et == "return_initiated":
        pid = e.get("product_id")
        cd = {"content_type":"product", "content_ids":[pid] if pid else [], "value": 0.0, "currency": cur}
        cd = append_margin_pltv(cd, cfg, single_price=0.0, contents=None)
        cd = _schema_mutations(cd, cfg)
        out = [{**base, "event_name":"ReturnInitiated", "custom_data": cd}]

    # apply bad-data toggles
    for ev in out:
        if cfg.get("null_event_id"):
            ev["event_id"] = None
        cd = ev.get("custom_data") or {}
        if cfg.get("null_currency") and "currency" in cd:
            cd["currency"] = None
        if cfg.get("null_price"):
            if "value" in cd:
                cd["value"] = None
            if "contents" in cd and isinstance(cd["contents"], list):
                for c in cd["contents"]:
                    if isinstance(c, dict) and "item_price" in c:
                        c["item_price"] = None
        ev["custom_data"] = cd

    out = [_maybe_str_nums(ev, cfg) for ev in out]
    return out

def map_sim_event_to_ga4(e, cfg) -> List[Dict[str,Any]]:
    et = e.get("event_type")
    events = []
    if et == "product_view":
        p = e["product"]
        events.append({"name":"view_item", "params":{"currency": e["context"]["currency"], "value": p["price"], "items":[{"item_id": p["product_id"], "price": p["price"]}]}})
    elif et == "add_to_cart":
        li = e["line_item"]
        events.append({"name":"add_to_cart", "params":{"currency": e["context"]["currency"], "value": li["qty"]*li["price"], "items":[{"item_id": li["product_id"], "quantity":li["qty"], "price": li["price"]}]}})
    elif et == "begin_checkout":
        total = e.get("total", 0.0)
        events.append({"name":"begin_checkout", "params":{"currency": e["context"]["currency"], "value": total}})
    elif et == "purchase":
        total = e.get("total", 0.0)
        events.append({"name":"purchase", "params":{"currency": e["context"]["currency"], "value": total, "transaction_id": e.get("order_id","")}})
    return events

# -------------------- HTML (embedded) --------------------
def banner_html():
    banners = []
    if not PIXEL_ID:
        banners.append("PIXEL_ID missing: Pixel will initialize but may not attribute correctly.")
    if not (ACCESS_TOKEN and PIXEL_ID):
        banners.append("CAPI not fully configured: set PIXEL_ID and ACCESS_TOKEN to enable server sends.")
    if CONFIG.get("enable_ga4") and not (GA4_MEASUREMENT_ID and GA4_API_SECRET):
        banners.append("GA4 enabled but GA4 credentials missing.")
    return "".join(f'<div class="banner">{{msg}}</div>' for msg in banners)

PAGE_HTML_HEAD = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Demo Store — Simulator</title>
<style>
:root { --bd:#223040; --fg:#e6eef7; --muted:#9fb3c8; --ok:#28c76f; --err:#ff5c5c; --bg:#0b0f14; --panel:#0e1520; }
* { box-sizing:border-box }
body { margin:0; font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; background: radial-gradient(1200px 800px at 80% -10%, #12263c 0%, #0b0f14 60%); color: var(--fg); }
.container { max-width:1200px; margin:20px auto; padding:0 16px; }
.banner { background:#331; color:#ffd; border:1px solid #633; padding:8px 12px; border-radius:10px; margin:10px 0; }
.grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap:14px; }
.card { border:1px solid var(--bd); border-radius:14px; padding:14px; background: linear-gradient(180deg, rgba(255,255,255,0.04), transparent 70%), var(--panel); }
.row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
.kv { display:flex; gap:10px; align-items:center; }
.small { font-size:12px; color:var(--muted); }
h1 { margin:8px 0 6px; font-size:22px; }
h3 { margin:0 0 8px; font-size:16px; }
input, select, textarea { background:#0e1722; color:var(--fg); border:1px solid var(--bd); border-radius:10px; padding:8px 10px; }
textarea { width:100%; min-height:90px; }
.btn { position:relative; border:1px solid var(--bd); background:#0e1722; color:var(--fg); padding:8px 12px; border-radius:10px; cursor:pointer; }
.btn .tick, .btn .x { position:absolute; right:8px; top:50%; transform:translateY(-50%) scale(0.8); opacity:0; transition:opacity .18s, transform .18s; }
.btn .tick { color:var(--ok); } .btn .x { color:var(--err); }
.btn .tick svg, .btn .x svg { width:18px; height:18px; }
.btn.show-tick .tick { opacity:1; transform:translateY(-50%) scale(1); }
.btn.show-err .x { opacity:1; transform:translateY(-50%) scale(1); }
.table { width:100%; border-collapse:collapse; font-size:12px; }
.table th, .table td { border-bottom:1px solid #183048; padding:6px 8px; vertical-align:top; }
.badge { display:inline-block; border:1px solid var(--bd); padding:2px 8px; border-radius:999px; font-size:11px; color:var(--muted); }
.topbar { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin:10px 0; }
.spark { width:120px; height:36px; }
pre { margin:0; white-space:pre-wrap; word-break:break-word; }
code { color:#cbd9ff; }
.warn { color:#ffcf6e }
.flex { display:flex; gap:10px; }
.modal{ position:fixed; inset:0; background:rgba(0,0,0,0.6); display:none; align-items:center; justify-content:center; }
.modal .panel{ background:#0e1520; border:1px solid var(--bd); border-radius:12px; max-width:760px; width:92vw; max-height:80vh; padding:12px; overflow:auto; }
.modal .panel h4{ margin:0 0 6px; }
.modal .close{ float:right; cursor:pointer; }
.hidden{ display:none !important; }
</style>
<script>
function rid(){ return 'evt_' + Math.random().toString(36).slice(2) + Date.now().toString(36); }
function flashIcon(btn, ok){ if(!btn) return; const cls = ok ? 'show-tick' : 'show-err'; btn.classList.add(cls); setTimeout(()=>btn.classList.remove(cls), 1000); }
async function fetchJSON(url, opts){ const r = await fetch(url, opts); if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); }
async function fetchOK(url, opts){ try{ const r = await fetch(url, opts); return r.ok; }catch(e){ return false; } }
function copyTxt(t){ navigator.clipboard.writeText(t).catch(()=>{}); }
function getCookie(name){ return document.cookie.split('; ').find(r=>r.startsWith(name+'='))?.split('=')[1] || ''; }
</script>
"""

PAGE_HTML_BODY_PREFIX = """
</head><body>
<div class="container">
  <h1>Demo Store Simulator <span class="small">v__APP_VERSION__</span></h1>
  __BANNERS__
  <div class="topbar">
    <span class="badge" id="badge_pixel">Pixel: ?</span>
    <span class="badge" id="badge_capi">CAPI: ?</span>
    <span class="badge" id="badge_test">Test Code: __TEST_ONOFF__</span>
    <span class="badge" id="badge_seed">Seed: none</span>
    <span class="badge" id="badge_preset">Preset: Default</span>
    <span class="badge" id="badge_ga4">GA4: __GA4_ONOFF__</span>
  </div>

  <div class="grid">
    <div class="card">
      <h3>Pixel Test</h3>
      <p class="small">Sends browser events only if <b>Enable Pixel</b> is on. We also ping the server for telemetry to power the dedup meter.</p>
      <div class="row" style="gap:10px; margin-bottom:6px;">
        <button class="btn" onclick="sendView(this)">ViewContent
          <span class="tick" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M20.3 5.7a1 1 0 0 1 0 1.4l-10 10a1 1 0 0 1-1.4 0l-5-5a1 1 0 1 1 1.4-1.4l4.3 4.3L18.9 5.7a1 1 0 0 1 1.4 0z"/></svg></span>
          <span class="x" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M18.3 5.7a1 1 0 0 1 0 1.4L13.4 12l4.9 4.9a1 1 0 1 1-1.4 1.4L12 13.4l-4.9 4.9a1 1 0 0 1-1.4-1.4L10.6 12 5.7 7.1A1 1 0 1 1 7.1 5.7L12 10.6l4.9-4.9a1 1 0 0 1 1.4 0z"/></svg></span>
        </button>
        <button class="btn" onclick="sendATC(this)">AddToCart
          <span class="tick" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M20.3 5.7a1 1 0 0 1 0 1.4l-10 10a1 1 0 0 1-1.4 0l-5-5a1 1 0 1 1 1.4-1.4l4.3 4.3L18.9 5.7a1 1 0 0 1 1.4 0z"/></svg></span>
          <span class="x" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M18.3 5.7a1 1 0 0 1 0 1.4L13.4 12l4.9 4.9a1 1 0 1 1-1.4 1.4L12 13.4l-4.9 4.9a1 1 0 0 1-1.4-1.4L10.6 12 5.7 7.1A1 1 0 1 1 7.1 5.7L12 10.6l4.9-4.9a1 1 0 0 1 1.4 0z"/></svg></span>
        </button>
        <button class="btn" onclick="sendInitiate(this)">InitiateCheckout
          <span class="tick" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M20.3 5.7a1 1 0 0 1 0 1.4l-10 10a1 1 0 0 1-1.4 0l-5-5a1 1 0 1 1 1.4-1.4l4.3 4.3L18.9 5.7a1 1 0 0 1 1.4 0z"/></svg></span>
          <span class="x" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M18.3 5.7a1 1 0 0 1 0 1.4L13.4 12l4.9 4.9a1 1 0 1 1-1.4 1.4L12 13.4l-4.9 4.9a1 1 0 0 1-1.4-1.4L10.6 12 5.7 7.1A1 1 0 1 1 7.1 5.7L12 10.6l4.9-4.9a1 1 0 0 1 1.4 0z"/></svg></span>
        </button>
        <button class="btn" onclick="sendPurchase(this)">Purchase
          <span class="tick" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M20.3 5.7a1 1 0 0 1 0 1.4l-10 10a1 1 0 0 1-1.4 0l-5-5a1 1 0 1 1 1.4-1.4l4.3 4.3L18.9 5.7a1 1 0 0 1 1.4 0z"/></svg></span>
          <span class="x" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M18.3 5.7a1 1 0 0 1 0 1.4L13.4 12l4.9 4.9a1 1 0 1 1-1.4 1.4L12 13.4l-4.9 4.9a1 1 0 0 1-1.4-1.4L10.6 12 5.7 7.1A1 1 0 1 1 7.1 5.7L12 10.6l4.9-4.9a1 1 0 0 1 1.4 0z"/></svg></span>
        </button>
      </div>

      <div class="row">
        <label class="small">Mirror Pixel → CAPI</label>
        <input id="mirror_to_capi" type="checkbox"/>
      </div>

      <div class="row">
        <span class="small">Next Event Preview:</span>
        <pre id="previewBox" style="width:100%; background:#0b1320; border:1px solid var(--bd); border-radius:10px; padding:8px; margin-top:6px; height:120px; overflow:auto;">{}</pre>
      </div>
    </div>

    <!-- NEW: Pixel Auto (browser-side) -->
    <div class="card">
      <h3>Pixel Auto (browser → Pixel)</h3>
      <p class="small">Automatically fires Meta Pixel events from the browser. Respects Enable Pixel, currency/price nulls, mismatch, and event-ID toggles.</p>
      <div class="row">
        <label>Events/sec</label>
        <input id="px_rps" type="number" step="0.1" min="0.1" value="0.5"/>
      </div>
      <div class="row">
        <button id="pxStartBtn" class="btn" onclick="pixelAutoStart(this)">
          Start
          <span class="tick" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M20.3 5.7a1 1 0 0 1 0 1.4l-10 10a1 1 0 0 1-1.4 0l-5-5a1 1 0 1 1 1.4-1.4l4.3 4.3L18.9 5.7a1 1 0 0 1 1.4 0z"/></svg></span>
          <span class="x" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M18.3 5.7a1 1 0 0 1 0 1.4L13.4 12l4.9 4.9a1 1 0 1 1-1.4 1.4L12 13.4l-4.9 4.9a1 1 0 0 1-1.4-1.4L10.6 12 5.7 7.1A1 1 0 1 1 7.1 5.7L12 10.6l4.9-4.9a1 1 0 0 1 1.4 0z"/></svg></span>
        </button>
        <button id="pxStopBtn" class="btn" onclick="pixelAutoStop(this)" disabled>
          Stop
          <span class="tick" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M20.3 5.7a1 1 0 0 1 0 1.4l-10 10a1 1 0 0 1-1.4 0l-5-5a1 1 0 1 1 1.4-1.4l4.3 4.3L18.9 5.7a1 1 0 0 1 1.4 0z"/></svg></span>
          <span class="x" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M18.3 5.7a1 1 0 0 1 0 1.4L13.4 12l4.9 4.9a1 1 0 1 1-1.4 1.4L12 13.4l-4.9 4.9a1 1 0 0 1-1.4-1.4L10.6 12 5.7 7.1A1 1 0 1 1 7.1 5.7L12 10.6l4.9-4.9a1 1 0 0 1 1.4 0z"/></svg></span>
        </button>
      </div>
      <p id="px_status" class="small">Stopped</p>
    </div>

    <div class="card">
      <h3>Auto Stream (server → CAPI)</h3>
      <p class="small">Streams sessions to CAPI (and optional sinks). If <b>Enable CAPI</b> is off, nothing is sent.</p>
      <div class="row">
        <label>Sessions/sec</label><input id="rps" type="number" step="0.1" min="0.1" value="0.5"/>
      </div>
      <div class="row">
        <button id="startBtn" class="btn" onclick="startAuto(this)">Start
          <span class="tick" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M20.3 5.7a1 1 0 0 1 0 1.4l-10 10a1 1 0 0 1-1.4 0l-5-5a1 1 0 1 1 1.4-1.4l4.3 4.3L18.9 5.7a1 1 0 0 1 1.4 0z"/></svg></span>
          <span class="x" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M18.3 5.7a1 1 0 0 1 0 1.4L13.4 12l4.9 4.9a1 1 0 1 1-1.4 1.4L12 13.4l-4.9 4.9a1 1 0 0 1-1.4-1.4L10.6 12 5.7 7.1A1 1 0 1 1 7.1 5.7L12 10.6l4.9-4.9a1 1 0 0 1 1.4 0z"/></svg></span>
        </button>
        <button id="stopBtn" class="btn" onclick="stopAuto(this)">Stop
          <span class="tick" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M20.3 5.7a1 1 0 0 1 0 1.4l-10 10a1 1 0 0 1-1.4 0l-5-5a1 1 0 1 1 1.4-1.4l4.3 4.3L18.9 5.7a1 1 0 0 1 1.4 0z"/></svg></span>
          <span class="x" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M18.3 5.7a1 1 0 0 1 0 1.4L13.4 12l4.9 4.9a1 1 0 1 1-1.4 1.4L12 13.4l-4.9 4.9a1 1 0 0 1-1.4-1.4L10.6 12 5.7 7.1A1 1 0 1 1 7.1 5.7L12 10.6l4.9-4.9a1 1 0 0 1 1.4 0z"/></svg></span>
        </button>
      </div>
      <p id="status" class="small">…</p>
      <div class="row small">
        <canvas id="spark_sent" class="spark"></canvas>
        <canvas id="spark_pur" class="spark"></canvas>
        <span id="dedup" class="small">Dedup: …</span>
      </div>
    </div>

    <div class="card">
      <h3>Presets & Seed</h3>
      <div class="row">
        <input id="preset_name" placeholder="Preset name" style="width:160px"/>
        <button class="btn" onclick="savePreset(this)">Save</button>
        <button class="btn" onclick="loadPreset(this)">Load</button>
        <button class="btn" onclick="resetDefaults(this)">Reset</button>
        <button class="btn" onclick="exportPreset(this)">Export</button>
        <label class="small">Import JSON<input id="import_preset" type="file" accept="application/json"></label>
      </div>
      <div class="row">
        <label>Seed</label><input id="seed" placeholder="fixed seed for reproducibility" style="width:220px"/>
        <button class="btn" onclick="saveSeed(this)">Apply Seed</button>
      </div>
      <p class="small">Seed makes traffic repeatable. Presets capture all controls below.</p>
    </div>

    <div class="card" data-panel="adv">
      <h3>Advanced Controls</h3>
      <div class="row"><label class="small">Visible</label><input id="toggle_adv" type="checkbox" checked/></div>
      <hr/>
      <div class="row"><label class="small">Enable Pixel</label><input id="enable_pixel" type="checkbox"/></div>
      <div class="row"><label class="small">Enable CAPI</label><input id="enable_capi" type="checkbox"/></div>
      <hr/>
      <div class="row"><label>P(Add to Cart)</label><input id="p_add_to_cart" type="number" step="0.01" min="0" max="1" value="0.35"/></div>
      <div class="row"><label>P(Begin Checkout)</label><input id="p_begin_checkout" type="number" step="0.01" min="0" max="1" value="0.70"/></div>
      <div class="row"><label>P(Purchase)</label><input id="p_purchase" type="number" step="0.01" min="0" max="1" value="0.70"/></div>
      <div class="row"><label>Catalog size</label><input id="product_catalog_size" type="number" step="1" min="1" value="200"/></div>
      <div class="row"><label>Price Min</label><input id="price_min" type="number" step="0.01" min="0" value="10"/></div>
      <div class="row"><label>Price Max</label><input id="price_max" type="number" step="0.01" min="0" value="120"/></div>
      <div class="row"><label>Currency</label>
        <select id="currency_override">
          <option value="AUTO">Auto</option><option value="USD">USD</option><option value="EUR">EUR</option>
          <option value="GBP">GBP</option><option value="AUD">AUD</option><option value="CAD">CAD</option>
          <option value="JPY">JPY</option><option value="NULL">Null</option>
        </select>
      </div>
      <hr/>
      <div class="row"><label>Free Ship ≥</label><input id="free_shipping_threshold" type="number" step="0.01" min="0" value="75"/></div>
      <div class="row"><label>Shipping Options</label><input id="shipping_options" type="text" value="4.99, 6.99, 9.99"/></div>
      <div class="row"><label>Tax Rate</label><input id="tax_rate" type="number" step="0.001" min="0" max="1" value="0.08"/></div>
      <hr/>
      <div class="row"><label>Null price</label><input id="null_price" type="checkbox"/></div>
      <div class="row"><label>Null currency</label><input id="null_currency" type="checkbox"/></div>
      <div class="row"><label>Null event_id</label><input id="null_event_id" type="checkbox"/></div>
      <hr/>
      <div class="row"><label>Append margin</label><input id="append_margin" type="checkbox" checked/></div>
      <div class="row"><label>Cost % Min (0–1)</label><input id="cost_pct_min" type="number" step="0.01" min="0" max="1" value="0.4"/></div>
      <div class="row"><label>Cost % Max (0–1)</label><input id="cost_pct_max" type="number" step="0.01" min="0" max="1" value="0.8"/></div>
      <div class="row"><label>Append PLTV</label><input id="append_pltv" type="checkbox" checked/></div>
      <div class="row"><label>PLTV min</label><input id="pltv_min" type="number" step="0.01" min="0" value="120"/></div>
      <div class="row"><label>PLTV max</label><input id="pltv_max" type="number" step="0.01" min="0" value="600"/></div>
    </div>

    <div class="card" data-panel="chaos">
      <h3>Discrepancy & Chaos</h3>
      <div class="row"><label class="small">Visible</label><input id="toggle_chaos" type="checkbox" checked/></div>
      <hr/>
      <div class="row"><label>Mismatch value ±%</label><input id="mismatch_value_pct" type="number" step="0.01" min="0" max="1" value="0"/></div>
      <div class="row"><label>Mismatch currency</label>
        <select id="mismatch_currency"><option>NONE</option><option>PIXEL</option><option>CAPI</option></select>
      </div>
      <div class="row"><label>Desync event_id (Pixel ≠ CAPI)</label><input id="desync_event_id" type="checkbox"/></div>
      <div class="row"><label>Duplicate Pixel ID every N</label><input id="duplicate_event_id_n" type="number" step="1" min="0" value="0"/></div>
      <div class="row"><label>Drop Pixel every N</label><input id="drop_pixel_every_n" type="number" step="1" min="0" value="0"/></div>
      <div class="row"><label>Lag CAPI (s)</label><input id="lag_capi_seconds" type="number" step="0.1" min="0" value="0"/></div>
      <hr/>
      <div class="row"><label>Network latency (ms)</label><input id="net_capi_latency_ms" type="number" step="1" min="0" value="0"/></div>
      <div class="row"><label>Error rate (0–1)</label><input id="net_capi_error_rate" type="number" step="0.01" min="0" max="1" value="0"/></div>
      <hr/>
      <div class="row"><label>Remove contents</label><input id="schema_remove_contents" type="checkbox"/></div>
      <div class="row"><label>Empty arrays</label><input id="schema_empty_arrays" type="checkbox"/></div>
      <div class="row"><label>Numbers as strings</label><input id="schema_str_numbers" type="checkbox"/></div>
      <div class="row"><label>Unknown fields</label><input id="schema_unknown_fields" type="checkbox"/></div>
      <div class="row"><label>Clock skew (s)</label><input id="clock_skew_seconds" type="number" step="1" value="0"/></div>
      <hr/>
      <div class="row"><label>Kill PageView</label><input id="kill_PageView" type="checkbox"/></div>
      <div class="row"><label>Kill ViewContent</label><input id="kill_ViewContent" type="checkbox"/></div>
      <div class="row"><label>Kill AddToCart</label><input id="kill_AddToCart" type="checkbox"/></div>
      <div class="row"><label>Kill InitiateCheckout</label><input id="kill_InitiateCheckout" type="checkbox"/></div>
      <div class="row"><label>Kill Purchase</label><input id="kill_Purchase" type="checkbox"/></div>
    </div>

    <div class="card">
      <h3>Sinks & Tools</h3>
      <div class="row"><label>Webhook sink</label><input id="enable_webhook" type="checkbox"/></div>
      <div class="row"><label>GA4 sink</label><input id="enable_ga4" type="checkbox"/></div>
      <div class="row"><button class="btn" onclick="saveConfig(this)">Save Controls</button></div>
      <p class="small">File sink: __FILE_SINK_LABEL__</p>
      <div class="row"><button class="btn" onclick="downloadReplay()">Download Replay Bundle</button></div>
    </div>

    <div class="card">
      <h3>Dataset Quality Helper</h3>
      <p class="small">Summarizes last 100 server (CAPI) events: presence of match keys.</p>
      <div class="row small">
        <button class="btn" onclick="refreshDQ(this)">Refresh</button>
        <span id="dq_text">…</span>
      </div>
    </div>

    <div class="card" style="grid-column:1/-1;">
      <h3>Event Console</h3>
      <div class="row small">
        <label>Filter</label>
        <select id="f_channel"><option value="">channel</option><option>pixel</option><option>capi</option><option>webhook</option><option>ga4</option></select>
        <select id="f_type"><option value="">type</option><option>PageView</option><option>ViewContent</option><option>AddToCart</option><option>InitiateCheckout</option><option>Purchase</option><option>ReturnInitiated</option></select>
        <select id="f_ok"><option value="">status</option><option value="1">ok</option><option value="0">error</option></select>
        <button class="btn" onclick="refreshConsole(this)">Refresh</button>
      </div>
      <div id="console_table" style="max-height:320px; overflow:auto; margin-top:6px;"></div>
    </div>

    <div class="card" style="grid-column:1/-1;">
      <h3>Scenario Runner (JSON)</h3>
      <p class="small">Paste a simple JSON scenario (steps with event types and waits). Example:
      <code>{{"name":"cart_abandon","steps":[{{"page_view":{{"url":"/"}}}},{{"product_view":{{"sku":"SKU-10123"}}}},{{"add_to_cart":{{"qty":1}}}},{{"wait":12}},{{"begin_checkout":{{}}}}]}}</code></p>
      <textarea id="scenario_text" placeholder='{{"name":"demo","steps":[{{"page_view":{{}}}},{{"product_view":{{}}}},{{"add_to_cart":{{"qty":1}}}},{{"begin_checkout":{{}}}},{{"purchase":{{}}}}]}}'></textarea>
      <div class="row"><button class="btn" onclick="runScenario(this)">Run Scenario</button></div>
      <p id="scenario_status" class="small">…</p>
    </div>
  </div>
"""

PAGE_HTML_FOOT = """
<script>
// ----- Pixel loader -----
(function(){
  var s=document.createElement('script'); s.async=true; s.src='https://connect.facebook.net/en_US/fbevents.js';
  document.head.appendChild(s);
  window.fbq = window.fbq || function(){ (fbq.q=fbq.q||[]).push(arguments); };
  fbq.loaded=true; fbq.version='2.0'; fbq.queue=[];
  fbq('init', '__PIXEL_ID__');
})();

// ----- UI helpers -----
function readBadToggles(){
  return {
    null_price: document.getElementById('null_price').checked,
    null_currency: document.getElementById('null_currency').checked,
    null_event_id: document.getElementById('null_event_id').checked,
  };
}
function randBetween(a,b){ const lo=Math.min(a,b), hi=Math.max(a,b); return lo + Math.random()*(hi-lo); }
function marginFromPrice(price, minPct, maxPct){
  const pct = randBetween(minPct, maxPct);
  const cost = Math.max(0, price * pct);
  return Math.max(0, Math.round((price - cost) * 100)/100);
}
function attachMarginPltv(payload, price, cfg){
  if (document.getElementById('append_margin').checked){
    let m = null;
    if (Array.isArray(payload.contents) && payload.contents.length){
      m = 0;
      const lo = parseFloat(document.getElementById('cost_pct_min').value||'0.4');
      const hi = parseFloat(document.getElementById('cost_pct_max').value||'0.8');
      for(const c of payload.contents) {
        if (typeof c.item_price === 'number') m += marginFromPrice(c.item_price, lo, hi) * (c.quantity||1);
      }
      m = Math.round(m*100)/100;
    } else if (typeof price === 'number') {
      const lo = parseFloat(document.getElementById('cost_pct_min').value||'0.4');
      const hi = parseFloat(document.getElementById('cost_pct_max').value||'0.8');
      m = marginFromPrice(price, lo, hi);
    }
    if (m != null) payload.margin = m;
  }
  if (document.getElementById('append_pltv').checked){
    const lo = parseFloat(document.getElementById('pltv_min').value||'120');
    const hi = parseFloat(document.getElementById('pltv_max').value||'600');
    payload.predicted_ltv = Math.round(randBetween(lo,hi)*100)/100;
  }
}
function currentCurrency(channel){
  const mode = (document.getElementById('currency_override').value || 'AUTO').toUpperCase();
  const bad = readBadToggles();
  const mm = (document.getElementById('mismatch_currency').value||'NONE').toUpperCase();
  if (mm==='PIXEL' && channel==='pixel') return null;
  if (mode==='NULL') return null;
  if (bad.null_currency) return null;
  if (mode!=='AUTO') return mode;
  return 'USD';
}

// ----- Next-event preview -----
function updatePreview(){
  const price = 68.99, qty = 2, total = 149.02;
  const p = { contents:[{id:'SKU-10057',quantity:qty,item_price:price}], currency: currentCurrency('pixel'), value: total };
  attachMarginPltv(p, null, null);
  document.getElementById('previewBox').textContent = JSON.stringify(p, null, 2);
}

// ----- Discrepancy helpers -----
function maybeMismatchValue(val){
  const pct = parseFloat(document.getElementById('mismatch_value_pct').value||'0');
  if (!pct || !val) return val;
  const delta = val * pct;
  return Math.round((val + randBetween(-delta, delta))*100)/100;
}
function maybeDesyncEventId(eid){
  return document.getElementById('desync_event_id').checked ? eid + '_px' : eid;
}
function maybeDropPixel(){
  const n = parseInt(document.getElementById('drop_pixel_every_n').value||'0',10);
  if (!n) return false;
  window.__pxCount = (window.__pxCount||0) + 1;
  return window.__pxCount % n === 0;
}
function maybeDupePixelId(eid){
  const n = parseInt(document.getElementById('duplicate_event_id_n').value||'0',10);
  if (!n) return eid;
  window.__dupeIdx = (window.__dupeIdx||0) + 1;
  if (window.__dupeIdx % n === 0) return (window.__lastPxEid || eid);
  window.__lastPxEid = eid;
  return eid;
}

// ----- Pixel send & server telemetry -----
let ENABLE_PIXEL = true;
async function sendPixel(name, payload, opts, btn){
  if (!ENABLE_PIXEL) { flashIcon(btn, false); return; }
  const intended = JSON.parse(JSON.stringify(payload));
  if ('value' in payload) payload.value = maybeMismatchValue(payload.value);
  const eid0 = (opts && opts.eventID) || rid();
  const eid = maybeDupePixelId(maybeDesyncEventId(eid0));
  if (maybeDropPixel()) {
    await fetch('/metrics/pixel', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({event_name:name,intended, sent:payload, event_id:eid, dropped:true})}).catch(()=>{});
    flashIcon(btn, true); return;
  }
  try { fbq('track', name, payload, { eventID: eid }); } catch(e) {}
  await fetch('/metrics/pixel', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({event_name:name,intended, sent:payload, event_id:eid})}).catch(()=>{});

  // Mirror to CAPI if enabled (shares event_id for dedup). Includes fbp/fbc cookies when present.
  try {
    if (document.getElementById('mirror_to_capi')?.checked) {
      const body = { event_name: name, payload, event_id: eid, fbp: getCookie('_fbp')||null, fbc: getCookie('_fbc')||null };
      await fetch('/mirror/pixel', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
    }
  } catch(e){}

  flashIcon(btn, true);
}

function sendView(btn){
  const bad = readBadToggles();
  const price = 68.99;
  const payload = {
    content_type:'product',
    content_ids:['SKU-10057'],
    currency: currentCurrency('pixel'),
    value: bad.null_price ? null : price
  };
  attachMarginPltv(payload, price);
  sendPixel('ViewContent', payload, {eventID: bad.null_event_id ? null : rid()}, btn);
}
function sendATC(btn){
  const bad = readBadToggles();
  const price = 68.99, qty = 1, value = qty*price;
  const payload = {
    content_type:'product',
    content_ids:['SKU-10057'],
    contents:[{id:'SKU-10057', quantity:qty, item_price: bad.null_price ? null : price}],
    currency: currentCurrency('pixel'),
    value: bad.null_price ? null : value
  };
  attachMarginPltv(payload, null);
  sendPixel('AddToCart', payload, {eventID: bad.null_event_id ? null : rid()}, btn);
}
function sendInitiate(btn){
  const bad = readBadToggles();
  const price = 68.99, qty = 2, total = 149.02;
  const payload = {
    contents:[{id:'SKU-10057', quantity:qty, item_price: bad.null_price ? null : price}],
    currency: currentCurrency('pixel'),
    value: bad.null_price ? null : total
  };
  attachMarginPltv(payload, null);
  sendPixel('InitiateCheckout', payload, {eventID: bad.null_event_id ? null : rid()}, btn);
}
function sendPurchase(btn){
  const bad = readBadToggles();
  const price = 68.99, qty = 2, total = 149.02;
  const payload = {
    contents:[{id:'SKU-10057', quantity:qty, item_price: bad.null_price ? null : price}],
    currency: currentCurrency('pixel'),
    value: bad.null_price ? null : total
  };
  attachMarginPltv(payload, null);
  sendPixel('Purchase', payload, {eventID: bad.null_event_id ? null : rid()}, btn);
}

// ----- Pixel Auto Stream (browser -> Pixel) -----
let __pxTimer = null;

function pixelAutoTick(){
  if (!ENABLE_PIXEL) return;
  const r = Math.random();
  if (r < 0.50)      { sendView(null); }
  else if (r < 0.75) { sendATC(null); }
  else if (r < 0.90) { sendInitiate(null); }
  else               { sendPurchase(null); }
}

function pixelAutoStart(btn){
  const rps = parseFloat(document.getElementById('px_rps').value || '0.5');
  const interval = Math.max(50, 1000 / Math.max(0.1, rps));
  if (__pxTimer) clearInterval(__pxTimer);
  __pxTimer = setInterval(pixelAutoTick, interval);
  document.getElementById('px_status').textContent = 'Running at ' + (Math.round((1000/interval)*100)/100) + ' events/sec';
  document.getElementById('pxStartBtn').disabled = true;
  document.getElementById('pxStopBtn').disabled = false;
  flashIcon(btn, true);
}

function pixelAutoStop(btn){
  if (__pxTimer) clearInterval(__pxTimer);
  __pxTimer = null;
  document.getElementById('px_status').textContent = 'Stopped';
  document.getElementById('pxStartBtn').disabled = false;
  document.getElementById('pxStopBtn').disabled = true;
  flashIcon(btn, true);
}

function pxRefreshStatus(){
  const running = !!__pxTimer;
  document.getElementById('px_status').textContent = running ? 'Running' : 'Stopped';
  document.getElementById('pxStartBtn').disabled = running;
  document.getElementById('pxStopBtn').disabled = !running;
}

// ----- Auto stream controls (server) -----
async function refreshStatus(){
  try {
    const j = await fetchJSON('/auto/status');
    document.getElementById('status').textContent = j.running ? ('Running at '+j.rps+' sessions/sec') : 'Stopped';
    document.getElementById('startBtn').disabled = j.running;
    document.getElementById('stopBtn').disabled = !j.running;
    if (j.running) document.getElementById('rps').value = j.rps;
  } catch(e){ document.getElementById('status').textContent = 'Unknown'; }
}
async function startAuto(btn){
  const rps = parseFloat(document.getElementById('rps').value||'0.5');
  const ok = await fetchOK('/auto/start?rps='+encodeURIComponent(rps));
  flashIcon(btn, ok); setTimeout(refreshStatus, 250);
}
async function stopAuto(btn){
  const ok = await fetchOK('/auto/stop'); flashIcon(btn, ok); setTimeout(refreshStatus, 250);
}

// ----- Presets & Seed -----
async function saveSeed(btn){
  const seed = document.getElementById('seed').value||'';
  const ok = await fetchOK('/config/seed', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({seed})}); 
  flashIcon(btn, ok); updateBadges();
}
async function savePreset(btn){
  const name = document.getElementById('preset_name').value||'Preset';
  const ok = await fetchOK('/presets/save?name='+encodeURIComponent(name), {method:'POST'});
  flashIcon(btn, ok); updateBadges();
}
async function loadPreset(btn){
  const name = document.getElementById('preset_name').value||'Preset';
  const ok = await fetchOK('/presets/load?name='+encodeURIComponent(name), {method:'POST'});
  flashIcon(btn, ok); await loadConfig();
}
async function exportPreset(btn){
  const name = document.getElementById('preset_name').value||'Preset';
  const j = await fetchJSON('/presets/export?name='+encodeURIComponent(name));
  const blob = new Blob([JSON.stringify(j,null,2)], {type:'application/json'});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = (j.name||'preset')+'.json'; a.click();
}
document.getElementById('import_preset').addEventListener('change', async (e)=>{
  const f = e.target.files[0]; if(!f) return;
  const text = await f.text();
  await fetch('/presets/import', {method:'POST', headers:{'Content-Type':'application/json'}, body:text});
  await loadConfig(); updateBadges();
});
async function resetDefaults(btn){
  const ok = await fetchOK('/presets/reset', {method:'POST'});
  flashIcon(btn, ok); await loadConfig();
}

// ----- Config I/O -----
async function loadConfig(){
  const cfg = await fetchJSON('/auto/config');
  ENABLE_PIXEL = !!cfg.enable_pixel;
  document.getElementById('enable_pixel').checked = ENABLE_PIXEL;
  document.getElementById('enable_capi').checked = !!cfg.enable_capi;
  document.getElementById('rps').value = cfg.rps;
  document.getElementById('p_add_to_cart').value = cfg.p_add_to_cart;
  document.getElementById('p_begin_checkout').value = cfg.p_begin_checkout;
  document.getElementById('p_purchase').value = cfg.p_purchase;
  document.getElementById('product_catalog_size').value = cfg.product_catalog_size;
  document.getElementById('price_min').value = cfg.price_min;
  document.getElementById('price_max').value = cfg.price_max;
  document.getElementById('currency_override').value = (cfg.currency_override||'AUTO');
  document.getElementById('free_shipping_threshold').value = cfg.free_shipping_threshold;
  document.getElementById('shipping_options').value = (cfg.shipping_options||[]).join(', ');
  document.getElementById('tax_rate').value = cfg.tax_rate;
  document.getElementById('null_price').checked = !!cfg.null_price;
  document.getElementById('null_currency').checked = !!cfg.null_currency;
  document.getElementById('null_event_id').checked = !!cfg.null_event_id;
  document.getElementById('append_margin').checked = !!cfg.append_margin;
  document.getElementById('cost_pct_min').value = cfg.cost_pct_min;
  document.getElementById('cost_pct_max').value = cfg.cost_pct_max;
  document.getElementById('append_pltv').checked = !!cfg.append_pltv;
  document.getElementById('pltv_min').value = cfg.pltv_min;
  document.getElementById('pltv_max').value = cfg.pltv_max;
  document.getElementById('seed').value = cfg.seed||'';
  document.getElementById('preset_name').value = cfg.active_preset||'';
  document.getElementById('mismatch_value_pct').value = cfg.mismatch_value_pct||0;
  document.getElementById('mismatch_currency').value = (cfg.mismatch_currency||'NONE');
  document.getElementById('desync_event_id').checked = !!cfg.desync_event_id;
  document.getElementById('duplicate_event_id_n').value = cfg.duplicate_event_id_n||0;
  document.getElementById('drop_pixel_every_n').value = cfg.drop_pixel_every_n||0;
  document.getElementById('lag_capi_seconds').value = cfg.lag_capi_seconds||0;
  document.getElementById('net_capi_latency_ms').value = cfg.net_capi_latency_ms||0;
  document.getElementById('net_capi_error_rate').value = cfg.net_capi_error_rate||0;
  document.getElementById('schema_remove_contents').checked = !!cfg.schema_remove_contents;
  document.getElementById('schema_empty_arrays').checked = !!cfg.schema_empty_arrays;
  document.getElementById('schema_str_numbers').checked = !!cfg.schema_str_numbers;
  document.getElementById('schema_unknown_fields').checked = !!cfg.schema_unknown_fields;
  document.getElementById('clock_skew_seconds').value = cfg.clock_skew_seconds||0;
  document.getElementById('kill_PageView').checked = !!(cfg.kill_event_types||{})["PageView"];
  document.getElementById('kill_ViewContent').checked = !!(cfg.kill_event_types||{})["ViewContent"];
  document.getElementById('kill_AddToCart').checked = !!(cfg.kill_event_types||{})["AddToCart"];
  document.getElementById('kill_InitiateCheckout').checked = !!(cfg.kill_event_types||{})["InitiateCheckout"];
  document.getElementById('kill_Purchase').checked = !!(cfg.kill_event_types||{})["Purchase"];
  document.getElementById('enable_webhook').checked = !!cfg.enable_webhook;
  document.getElementById('enable_ga4').checked = !!cfg.enable_ga4;
  updateBadges(); updatePreview();
}
async function saveConfig(btn){
  const body = {
    enable_pixel: document.getElementById('enable_pixel').checked,
    enable_capi: document.getElementById('enable_capi').checked,
    rps: parseFloat(document.getElementById('rps').value||'0.5'),
    p_add_to_cart: parseFloat(document.getElementById('p_add_to_cart').value||'0.35'),
    p_begin_checkout: parseFloat(document.getElementById('p_begin_checkout').value||'0.7'),
    p_purchase: parseFloat(document.getElementById('p_purchase').value||'0.7'),
    product_catalog_size: parseInt(document.getElementById('product_catalog_size').value||'200',10),
    price_min: parseFloat(document.getElementById('price_min').value||'10'),
    price_max: parseFloat(document.getElementById('price_max').value||'120'),
    currency_override: document.getElementById('currency_override').value||'AUTO',
    free_shipping_threshold: parseFloat(document.getElementById('free_shipping_threshold').value||'75'),
    shipping_options: (document.getElementById('shipping_options').value||'').split(',').map(s=>parseFloat(s.trim())).filter(x=>!isNaN(x)),
    tax_rate: parseFloat(document.getElementById('tax_rate').value||'0.08'),
    null_price: document.getElementById('null_price').checked,
    null_currency: document.getElementById('null_currency').checked,
    null_event_id: document.getElementById('null_event_id').checked,
    append_margin: document.getElementById('append_margin').checked,
    cost_pct_min: parseFloat(document.getElementById('cost_pct_min').value||'0.4'),
    cost_pct_max: parseFloat(document.getElementById('cost_pct_max').value||'0.8'),
    append_pltv: document.getElementById('append_pltv').checked,
    pltv_min: parseFloat(document.getElementById('pltv_min').value||'120'),
    pltv_max: parseFloat(document.getElementById('pltv_max').value||'600'),
    mismatch_value_pct: parseFloat(document.getElementById('mismatch_value_pct').value||'0'),
    mismatch_currency: document.getElementById('mismatch_currency').value||'NONE',
    desync_event_id: document.getElementById('desync_event_id').checked,
    duplicate_event_id_n: parseInt(document.getElementById('duplicate_event_id_n').value||'0',10),
    drop_pixel_every_n: parseInt(document.getElementById('drop_pixel_every_n').value||'0',10),
    lag_capi_seconds: parseFloat(document.getElementById('lag_capi_seconds').value||'0'),
    net_capi_latency_ms: parseInt(document.getElementById('net_capi_latency_ms').value||'0',10),
    net_capi_error_rate: parseFloat(document.getElementById('net_capi_error_rate').value||'0'),
    schema_remove_contents: document.getElementById('schema_remove_contents').checked,
    schema_empty_arrays: document.getElementById('schema_empty_arrays').checked,
    schema_str_numbers: document.getElementById('schema_str_numbers').checked,
    schema_unknown_fields: document.getElementById('schema_unknown_fields').checked,
    clock_skew_seconds: parseInt(document.getElementById('clock_skew_seconds').value||'0',10),
    kill_event_types: {
      PageView: document.getElementById('kill_PageView').checked,
      ViewContent: document.getElementById('kill_ViewContent').checked,
      AddToCart: document.getElementById('kill_AddToCart').checked,
      InitiateCheckout: document.getElementById('kill_InitiateCheckout').checked,
      Purchase: document.getElementById('kill_Purchase').checked,
    },
    enable_webhook: document.getElementById('enable_webhook').checked,
    enable_ga4: document.getElementById('enable_ga4').checked,
  };
  const ok = await fetchOK('/auto/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  flashIcon(btn, ok);
  if (ok) setTimeout(refreshStatus, 250);
  updateBadges(); updatePreview();
}
function updateBadges(){
  fetchJSON('/auto/config').then(cfg => {
    document.getElementById('badge_pixel').textContent = 'Pixel: ' + (cfg.enable_pixel?'on':'off');
    document.getElementById('badge_capi').textContent  = 'CAPI: ' + (cfg.enable_capi?'on':'off');
    document.getElementById('badge_seed').textContent  = 'Seed: ' + (cfg.seed?cfg.seed:'none');
    document.getElementById('badge_preset').textContent= 'Preset: ' + (cfg.active_preset||'Default');
  }).catch(()=>{});
}

// ----- Metrics polling (spark + dedup) -----
const sparkSent = document.getElementById('spark_sent').getContext('2d');
const sparkPur  = document.getElementById('spark_pur').getContext('2d');
let sA=[], sB=[];
function drawSpark(ctx, arr){
  ctx.clearRect(0,0,120,36);
  if(!arr.length) return;
  const max=Math.max(...arr,1), min=Math.min(...arr,0);
  ctx.beginPath();
  arr.forEach((v,i)=>{
    const x=i*(120/Math.max(1,arr.length-1));
    const y=36 - ((v-min)/(max-min||1))*34 - 1;
    if(i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
  });
  ctx.strokeStyle='#9bd';
  ctx.stroke();
}
async function pollMetrics(){
  try {
    const m = await fetchJSON('/metrics');
    sA.push(m.sent_total||0); if (sA.length>30) sA=sA.slice(-30);
    sB.push(m.purchases||0); if (sB.length>30) sB=sB.slice(-30);
    drawSpark(sparkSent, sA); drawSpark(sparkPur, sB);
    document.getElementById('dedup').textContent = `Dedup matched: ${m.dedup.matched} | pixel-only: ${m.dedup.pixel_only} | capi-only: ${m.dedup.capi_only}`;
  } catch(e) {}
}
setInterval(pollMetrics, 1000);

// ----- Event console -----
let __eventsCache = [];
function inspectIdx(i){
  const r = __eventsCache[i];
  if(!r) return;
  const modal = document.getElementById('inspect_modal');
  document.getElementById('inspect_text').textContent = JSON.stringify(r, null, 2);
  modal.style.display='flex';
}
function closeInspect(){ document.getElementById('inspect_modal').style.display='none'; }

async function refreshConsole(btn){
  const ch = document.getElementById('f_channel').value;
  const ty = document.getElementById('f_type').value;
  const ok = document.getElementById('f_ok').value;
  const q = new URLSearchParams(); if (ch) q.set('channel', ch); if (ty) q.set('type',ty); if (ok) q.set('ok', ok);
  const j = await fetchJSON('/api/events?'+q.toString());
  __eventsCache = j.items;
  const rows = j.items.map((r,idx) => `
    <tr>
      <td class="small">${r.ts}</td>
      <td>${r.channel}</td>
      <td>${r.event_name||''}</td>
      <td class="small">${r.ok?'ok':'err'}</td>
      <td class="small"><button class="btn" onclick='copyTxt(JSON.stringify({intended: r.intended, sent:r.sent}, null, 2))'>Copy</button></td>
      <td class="small"><button class="btn" onclick='inspectIdx(${idx})'>Inspect</button></td>
    </tr>
  `).join('');
  document.getElementById('console_table').innerHTML = `
    <table class="table">
      <thead><tr><th>time</th><th>channel</th><th>type</th><th>status</th><th>payload</th><th>inspect</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// ----- Scenario runner -----
async function runScenario(btn){
  const text = document.getElementById('scenario_text').value||'';
  document.getElementById('scenario_status').textContent = 'Running...';
  const ok = await fetchOK('/scenario/run', {method:'POST', headers:{'Content-Type':'application/json'}, body:text});
  flashIcon(btn, ok);
  document.getElementById('scenario_status').textContent = ok ? 'Scenario started.' : 'Error.';
}

// ----- Replay -----
async function downloadReplay(){
  const j = await fetchJSON('/replay/export');
  const blob = new Blob([JSON.stringify(j,null,2)], {type:'application/json'});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'replay_bundle.json'; a.click();
}

// ----- DQ helper -----
async function refreshDQ(btn){
  try{
    const j = await fetchJSON('/dq/keys');
    const parts = [];
    parts.push('events analyzed: '+j.total);
    for(const k of ['external_id','fbp','fbc','client_ip_address','client_user_agent']){
      const c = j.keys[k]||0;
      parts.push(k+': '+c);
    }
    document.getElementById('dq_text').textContent = parts.join(' | ');
    if(btn) flashIcon(btn,true);
  }catch(e){ if(btn) flashIcon(btn,false); }
}

// ----- Panel visibility -----
function applyPanelVisibility(){
  const adv = document.querySelector('[data-panel="adv"]');
  const chaos = document.querySelector('[data-panel="chaos"]');
  const advOn = document.getElementById('toggle_adv').checked;
  const chOn = document.getElementById('toggle_chaos').checked;
  // Keep first two children (title+toggle row) visible; hide the rest
  function setCard(card, on){
    if(!card) return;
    [...card.children].forEach((el, idx)=>{ if(idx>1) el.style.display = on ? '' : 'none'; });
  }
  setCard(adv, advOn);
  setCard(chaos, chOn);
}

document.getElementById('toggle_adv').addEventListener('change', applyPanelVisibility);
document.getElementById('toggle_chaos').addEventListener('change', applyPanelVisibility);

// ----- Init -----
window.addEventListener('load', async () => {
  await loadConfig(); updatePreview(); refreshStatus(); pollMetrics(); refreshConsole();
  pxRefreshStatus(); // initialize Pixel Auto controls
  applyPanelVisibility();
  if (document.getElementById('enable_pixel').checked) { try { fbq('track','PageView'); } catch(e){} }
});
</script>
<noscript><img height="1" width="1" style="display:none" src="https://www.facebook.com/tr?id=__PIXEL_ID__&ev=PageView&noscript=1"/></noscript>

<!-- Inspector Modal -->
<div id="inspect_modal" class="modal" onclick="closeInspect()">
  <div class="panel" onclick="event.stopPropagation()">
    <span class="close" onclick="closeInspect()">✕</span>
    <h4>Event Inspector</h4>
    <pre id="inspect_text" style="white-space:pre-wrap;"></pre>
  </div>
</div>

</div></body></html>
"""

# -------------------- Routes: HTML --------------------
@app.route("/")
def home():
    banners = banner_html()
    test_onoff = "on" if TEST_EVENT_CODE else "off"
    ga4_onoff = "on" if GA4_URL else "off"
    file_sink_label = FILE_SINK_PATH if FILE_SINK_PATH else "(disabled — set FILE_SINK_PATH env)"
    html = (
        PAGE_HTML_HEAD
        + PAGE_HTML_BODY_PREFIX
            .replace("__APP_VERSION__", APP_VERSION)
            .replace("__BANNERS__", banners)
            .replace("__TEST_ONOFF__", test_onoff)
            .replace("__GA4_ONOFF__", ga4_onoff)
            .replace("__FILE_SINK_LABEL__", file_sink_label)
        + PAGE_HTML_FOOT
            .replace("__PIXEL_ID__", PIXEL_ID or "")
    )
    return Response(html, mimetype="text/html")

# -------------------- Metrics endpoints --------------------
@app.post("/metrics/pixel")
def metrics_pixel():
    """Client pings here when it fires a Pixel event so we can power dedup & diff."""
    try:
        body = request.get_json(force=True) or {}
    except Exception:
        return {"ok": False}, 400
    name = body.get("event_name","")
    intended = body.get("intended") or {}
    sent = body.get("sent") or {}
    eid = body.get("event_id")
    dropped = bool(body.get("dropped"))
    diff = {"value": {"intended": intended.get("value"), "sent": sent.get("value")},
            "currency": {"intended": intended.get("currency"), "sent": sent.get("currency")}}
    entry = {
        "ts": now_iso(), "channel":"pixel", "event_name":name, "ok": (not dropped),
        "intended": intended, "sent": sent, "response": {"dropped": dropped}, "event_id": eid, "diff": diff
    }
    _log_event(entry)
    _ndjson_append({"channel":"pixel","event":entry})
    return {"ok": True}

@app.get("/metrics")
def metrics():
    with METRICS_LOCK:
        out = {
            "sent_total": COUNTS.get("sent_capi",0) + COUNTS.get("sent_pixel",0),
            "purchases": COUNTS.get("sent_capi_Purchase",0) + COUNTS.get("sent_pixel_Purchase",0),
            "errors": COUNTS.get("errors",0),
            "dedup": {
                "matched": DEDUP["matched"],
                "pixel_only": DEDUP["pixel_only"],
                "capi_only": DEDUP["capi_only"]
            }
        }
    return jsonify(out)

@app.get("/api/events")
def api_events():
    ch = request.args.get("channel") or ""
    ty = request.args.get("type") or ""
    ok = request.args.get("ok")
    okf = None if ok == "" or ok is None else (ok == "1")
    with METRICS_LOCK:
        items = list(EVENT_LOG)
    out = []
    for e in items:
        if ch and e.get("channel") != ch: continue
        if ty and e.get("event_name") != ty: continue
        if okf is not None and bool(e.get("ok")) != okf: continue
        out.append(e)
    return jsonify({"items": out[:200]})

# -------------------- Presets & seed --------------------
PRESETS_DIR = ".presets"
os.makedirs(PRESETS_DIR, exist_ok=True)

@app.post("/config/seed")
def set_seed():
    try:
        seed = (request.get_json(force=True) or {}).get("seed","")
    except Exception:
        seed = ""
    with CONFIG_LOCK:
        CONFIG["seed"] = seed
    reseed()
    return {"ok": True}

DEFAULT_CONFIG = json.loads(json.dumps(CONFIG))  # snapshot of boot defaults

def _preset_path(name:str) -> str:
    safe = "".join(c for c in name if c.isalnum() or c in ("-","_")).strip() or "Preset"
    return os.path.join(PRESETS_DIR, safe + ".json")

@app.post("/presets/save")
def presets_save():
    name = request.args.get("name","Preset")
    path = _preset_path(name)
    with CONFIG_LOCK:
        cfg = dict(CONFIG)
        cfg["active_preset"] = name
    with open(path,"w",encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return {"ok": True, "name": name}

@app.post("/presets/load")
def presets_load():
    name = request.args.get("name","Preset")
    path = _preset_path(name)
    if not os.path.exists(path):
        return {"ok": False, "error":"not found"}, 404
    with open(path,"r",encoding="utf-8") as f:
        cfg = json.load(f)
    with CONFIG_LOCK:
        CONFIG.clear(); CONFIG.update(cfg)
    reseed()
    return {"ok": True, "config": get_cfg_snapshot()}

@app.get("/presets/export")
def presets_export():
    name = request.args.get("name","Preset")
    path = _preset_path(name)
    if not os.path.exists(path):
        with CONFIG_LOCK:
            return jsonify({"name": name, "config": CONFIG})
    with open(path,"r",encoding="utf-8") as f:
        data = json.load(f)
    return jsonify({"name": name, **data})

@app.post("/presets/import")
def presets_import():
    try:
        data = request.get_json(force=True)
    except Exception:
        return {"ok": False, "error":"invalid json"}, 400
    if not isinstance(data, dict):
        return {"ok": False, "error":"bad shape"}, 400
    name = data.get("active_preset") or data.get("name") or "Imported"
    path = _preset_path(name)
    with open(path,"w",encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    with CONFIG_LOCK:
        CONFIG.clear(); CONFIG.update(data)
    reseed()
    return {"ok": True, "name": name}

@app.post("/presets/reset")
def presets_reset():
    with CONFIG_LOCK:
        CONFIG.clear(); CONFIG.update(DEFAULT_CONFIG)
    reseed()
    return {"ok": True, "config": get_cfg_snapshot()}

# -------------------- Auto stream --------------------
_auto_thread = None
_stop_evt = threading.Event()

def _event_base(session, **extra):
    return {
        "event_id": _uid(),
        "timestamp": now_iso(),
        "user": {
            "user_id": session["user_id"],
            "session_id": session["session_id"],
            "device": session["device"],
            "country": session["country"],
            "source": session["source"],
            "utm_campaign": session["utm_campaign"],
        },
        "context": {"currency": session["currency"], "store_id": session["store_id"]},
        **extra
    }

def _send_one_through_sinks(sim_evt, cfg):
    capi_events = map_sim_event_to_capi(sim_evt, cfg)
    ga4_events  = map_sim_event_to_ga4(sim_evt, cfg) if cfg.get("enable_ga4") else []

    kill = cfg.get("kill_event_types", {})
    for ev in capi_events:
        if kill.get(ev.get("event_name",""), False):
            return

    lag = float(cfg.get("lag_capi_seconds", 0.0))
    if lag > 0: time.sleep(max(0.0, lag))

    # Post CAPI
    resp = None
    ok = True
    if capi_events:
        try:
            resp = capi_post(capi_events, cfg)
            ok = True
        except requests.HTTPError as e:
            resp = {"error": str(e), "text": getattr(e.response,'text','')[:400]}
            ok = False
        except Exception as e:
            resp = {"error": str(e)}
            ok = False

        for ev in capi_events:
            entry = {
                "ts": now_iso(), "channel":"capi", "event_name": ev.get("event_name"),
                "intended": sim_evt, "sent": ev, "response": resp, "ok": ok, "event_id": ev.get("event_id")
            }
            _log_event(entry)
            _ndjson_append({"channel":"capi","event":entry})

    # webhook sink
    if capi_events and cfg.get("enable_webhook"):
        wresp = webhook_post(capi_events, cfg)
        entry = {
            "ts": now_iso(), "channel":"webhook", "event_name": capi_events[0].get("event_name") if capi_events else "",
            "intended": sim_evt, "sent": {"events": capi_events}, "response": wresp, "ok": bool(wresp.get("ok", True))
        }
        _log_event(entry)
        _ndjson_append({"channel":"webhook","event":entry})

    # GA4 sink
    if ga4_events:
        gresp = ga4_post(ga4_events, cfg)
        entry = {
            "ts": now_iso(), "channel":"ga4", "event_name": ga4_events[0].get("name") if ga4_events else "",
            "intended": sim_evt, "sent": {"events": ga4_events}, "response": gresp, "ok": bool(gresp.get("ok", True))
        }
        _log_event(entry)
        _ndjson_append({"channel":"ga4","event":entry})

def _send_simulated_session_once(cfg):
    s = _make_session(cfg)
    product = _make_product(cfg)

    for evt in [
        _event_base(s, event_type="page_view", page=rand_choice(["/","/home","/sale"])),
        _event_base(s, event_type="product_view", product=product),
    ]:
        _send_one_through_sinks(evt, cfg)

    if _rng.random() < cfg["p_add_to_cart"]:
        qty = rand_choice([1,1,1,2])
        line = {"product_id": product["product_id"], "qty": qty, "price": product["price"]}
        evt = _event_base(s, event_type="add_to_cart", line_item=line, cart_size=1)
        _send_one_through_sinks(evt, cfg)

        if _rng.random() < cfg["p_begin_checkout"]:
            subtotal = qty * product["price"]
            shipping = 0.0 if subtotal >= cfg["free_shipping_threshold"] else (rand_choice(cfg["shipping_options"]) if cfg["shipping_options"] else 0.0)
            tax = round(cfg["tax_rate"] * subtotal, 2)
            total = round(subtotal + shipping + tax, 2)
            cart = [line]
            evt = _event_base(s, event_type="begin_checkout", cart=cart, subtotal=round(subtotal,2),
                              shipping=shipping, tax=tax, total=total)
            _send_one_through_sinks(evt, cfg)

            if _rng.random() < cfg["p_purchase"]:
                evt = _event_base(s, event_type="purchase", items=cart, subtotal=round(subtotal,2),
                                  shipping=shipping, tax=tax, total=total,
                                  order_id="o_"+_uid()[:12], payment_method=rand_choice(["card","paypal","apple_pay","google_pay","klarna"]))
                _send_one_through_sinks(evt, cfg)

def _auto_loop():
    while not _stop_evt.is_set():
        cfg = get_cfg_snapshot()
        start = time.time()
        try:
            if cfg.get("enable_capi"):
                _send_simulated_session_once(cfg)
        except Exception:
            pass
        delay = max(0.05, 1.0 / max(0.1, cfg["rps"]))
        elapsed = time.time() - start
        _stop_evt.wait(max(0.0, delay - elapsed))

@app.get("/auto/start")
def auto_start():
    global _auto_thread
    q = request.args.get("rps")
    if q is not None:
        with CONFIG_LOCK:
            CONFIG["rps"] = clampf(q, 0.1, 10.0, CONFIG["rps"])
    if _auto_thread is not None and _auto_thread.is_alive():
        with CONFIG_LOCK:
            return jsonify({"ok": True, "running": True, "rps": round(CONFIG["rps"],2)})
    _stop_evt.clear()
    _auto_thread = threading.Thread(target=_auto_loop, daemon=True)
    _auto_thread.start()
    with CONFIG_LOCK:
        return jsonify({"ok": True, "running": True, "rps": round(CONFIG["rps"],2)})

@app.get("/auto/stop")
def auto_stop():
    global _auto_thread
    _stop_evt.set()
    if _auto_thread is not None:
        _auto_thread.join(timeout=1.0)
        _auto_thread = None
    return jsonify({"ok": True, "running": False})

@app.get("/auto/status")
def auto_status():
    running = _auto_thread is not None and _auto_thread.is_alive()
    with CONFIG_LOCK:
        rps = round(CONFIG["rps"], 2)
    return jsonify({"ok": True, "running": running, "rps": rps})

# -------------------- /ingest forwarder (client → server → CAPI) --------------------
@app.post("/ingest")
def ingest():
    """Accept a single sim event and forward to CAPI/sinks."""
    try:
        sim_event = request.get_json(force=True)
    except Exception:
        return {"ok": False, "error": "invalid JSON"}, 400
    cfg = get_cfg_snapshot()
    try:
        _send_one_through_sinks(sim_event, cfg)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

# -------------------- Pixel → CAPI mirror (shares event_id) --------------------
@app.post("/mirror/pixel")
def mirror_pixel():
    """
    Accepts a simple pixel-style payload and forwards to CAPI with the same event_id,
    allowing deduplication tests against browser pixel events.
    Body: { event_name, payload, event_id, fbp?, fbc? }
    """
    try:
        body = request.get_json(force=True) or {}
    except Exception:
        return {"ok": False, "error": "invalid JSON"}, 400

    event_name = body.get("event_name") or ""
    payload = body.get("payload") or {}
    event_id = body.get("event_id")
    fbp = body.get("fbp") or None
    fbc = body.get("fbc") or None

    cfg = get_cfg_snapshot()

    # Build minimal CAPI event
    ev = {
        "event_name": event_name,
        "event_time": int(time.time()),
        "action_source": "website",
        "event_id": event_id,
        "event_source_url": BASE_URL.rstrip("/") + "/pixel-test",
        "user_data": {
            "client_ip_address": request.remote_addr or "127.0.0.1",
            "client_user_agent": request.headers.get("User-Agent","Mirror/1.0"),
        },
        "custom_data": {}
    }
    if isinstance(payload, dict):
        # allow currency/value/contents/etc to pass through
        for k in ("currency","value","contents","content_ids","content_type","predicted_ltv","margin"):
            if k in payload:
                ev.setdefault("custom_data", {})[k] = payload[k]
    # fbp/fbc pass-through
    if fbp: ev["user_data"]["fbp"] = fbp
    if fbc: ev["user_data"]["fbc"] = fbc

    try:
        resp = capi_post([ev], cfg)
        ok = True
    except requests.HTTPError as e:
        resp = {"error": str(e), "text": getattr(e.response,'text','')[:400]}
        ok = False
    except Exception as e:
        resp = {"error": str(e)}
        ok = False

    entry = {
        "ts": now_iso(), "channel":"capi", "event_name": event_name,
        "intended": {"mirror": True, "pixel_payload": payload}, "sent": ev, "response": resp,
        "ok": ok, "event_id": event_id
    }
    _log_event(entry)
    _ndjson_append({"channel":"capi","event":entry})
    return jsonify({"ok": ok, "response": resp})

# -------------------- Config endpoints --------------------
@app.route("/auto/config", methods=["GET", "POST"])
def auto_config():
    if request.method == "GET":
        with CONFIG_LOCK:
            return jsonify(CONFIG)

    try:
        body = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"ok": False, "error":"invalid json"}), 400

    with CONFIG_LOCK:
        # master
        for k in ("enable_pixel","enable_capi","enable_webhook","enable_ga4","append_margin","append_pltv","null_price","null_currency","null_event_id","desync_event_id","schema_remove_contents","schema_empty_arrays","schema_str_numbers","schema_unknown_fields"):
            if k in body: CONFIG[k] = to_bool(body[k], CONFIG.get(k,False))
        # traffic
        if "rps" in body: CONFIG["rps"] = clampf(body["rps"], 0.1, 10.0, CONFIG["rps"])
        for k in ("p_add_to_cart","p_begin_checkout","p_purchase"):
            if k in body: CONFIG[k] = clampf(body[k], 0.0, 1.0, CONFIG[k])
        # catalog/pricing
        if "product_catalog_size" in body: CONFIG["product_catalog_size"] = clampp(body["product_catalog_size"], 1, 100000, CONFIG["product_catalog_size"])
        if "price_min" in body: CONFIG["price_min"] = clampf(body["price_min"], 0.0, 1e6, CONFIG["price_min"])
        if "price_max" in body: CONFIG["price_max"] = clampf(body["price_max"], max(0.01, CONFIG["price_min"]), 1e6, CONFIG["price_max"])
        # currency
        if "currency_override" in body:
            v = str(body["currency_override"]).upper()
            CONFIG["currency_override"] = v if v in _ALLOWED_CURRENCIES else CONFIG["currency_override"]
        # economics
        if "free_shipping_threshold" in body: CONFIG["free_shipping_threshold"] = clampf(body["free_shipping_threshold"], 0.0, 1e6, CONFIG["free_shipping_threshold"])
        if "shipping_options" in body:
            arr = body["shipping_options"] or []
            cleaned = []
            for v in arr:
                try:
                    fv = float(v)
                    if fv >= 0:
                        cleaned.append(round(fv, 2))
                except Exception:
                    pass
            if cleaned: CONFIG["shipping_options"] = cleaned
        if "tax_rate" in body: CONFIG["tax_rate"] = clampf(body["tax_rate"], 0.0, 1.0, CONFIG["tax_rate"])
        # margin + PLTV
        if "cost_pct_min" in body: CONFIG["cost_pct_min"] = clampf(body["cost_pct_min"], 0.0, 1.0, CONFIG["cost_pct_min"])
        if "cost_pct_max" in body:
            proposed = clampf(body["cost_pct_max"], CONFIG["cost_pct_min"], 1.0, CONFIG["cost_pct_max"])
            CONFIG["cost_pct_max"] = max(proposed, CONFIG["cost_pct_min"])
        if "pltv_min" in body: CONFIG["pltv_min"] = clampf(body["pltv_min"], 0.0, 1e7, CONFIG["pltv_min"])
        if "pltv_max" in body:
            proposed = clampf(body["pltv_max"], CONFIG["pltv_min"], 1e7, CONFIG["pltv_max"])
            CONFIG["pltv_max"] = max(proposed, CONFIG["pltv_min"])
        # discrepancies/chaos
        for k in ("mismatch_value_pct","lag_capi_seconds","net_capi_error_rate"):
            if k in body: CONFIG[k] = clampf(body[k], 0.0, 10.0, CONFIG[k])
        for k in ("duplicate_event_id_n","drop_pixel_every_n","net_capi_latency_ms","clock_skew_seconds"):
            if k in body: CONFIG[k] = clampp(body[k], 0, 10**9, CONFIG[k])
        if "mismatch_currency" in body:
            v = str(body["mismatch_currency"]).upper()
            CONFIG["mismatch_currency"] = v if v in ("NONE","PIXEL","CAPI") else CONFIG["mismatch_currency"]
        if "kill_event_types" in body:
            d = body["kill_event_types"] or {}
            keep = {}
            for name in ("PageView","ViewContent","AddToCart","InitiateCheckout","Purchase","ReturnInitiated"):
                keep[name] = to_bool(d.get(name, CONFIG["kill_event_types"].get(name, False)))
            CONFIG["kill_event_types"] = keep

    return jsonify({"ok": True, "config": get_cfg_snapshot()})

# -------------------- Scenario runner --------------------
_sc_thread = None
def _scenario_loop(steps: List[Dict[str,Any]]):
    cfg = get_cfg_snapshot()
    s = _make_session(cfg)
    product = _make_product(cfg)
    for step in steps:
        if "wait" in step:
            try:
                time.sleep(max(0.0, float(step["wait"])))
            except Exception:
                pass
            continue
        if "page_view" in step:
            _send_one_through_sinks(_event_base(s, event_type="page_view", page=step["page_view"].get("url","/")), cfg)
        if "product_view" in step:
            _send_one_through_sinks(_event_base(s, event_type="product_view", product=product), cfg)
        if "add_to_cart" in step:
            qty = int(step["add_to_cart"].get("qty",1)); line = {"product_id": product["product_id"], "qty": qty, "price": product["price"]}
            _send_one_through_sinks(_event_base(s, event_type="add_to_cart", line_item=line, cart_size=1), cfg)
        if "begin_checkout" in step or "purchase" in step:
            qty = 1
            subtotal = qty * product["price"]
            shipping = 0.0 if subtotal >= cfg["free_shipping_threshold"] else (rand_choice(cfg["shipping_options"]) if cfg["shipping_options"] else 0.0)
            tax = round(cfg["tax_rate"] * subtotal, 2)
            total = round(subtotal + shipping + tax, 2)
            cart = [{"product_id": product["product_id"], "qty": qty, "price": product["price"]}]
            if "begin_checkout" in step:
                _send_one_through_sinks(_event_base(s, event_type="begin_checkout", cart=cart, subtotal=round(subtotal,2), shipping=shipping, tax=tax, total=total), cfg)
            if "purchase" in step:
                _send_one_through_sinks(_event_base(s, event_type="purchase", items=cart, subtotal=round(subtotal,2),
                                  shipping=shipping, tax=tax, total=total, order_id="o_"+_uid()[:12]), cfg)

@app.post("/scenario/run")
def scenario_run():
    global _sc_thread
    try:
        body = request.get_json(force=True) or {}
    except Exception:
        return {"ok": False, "error":"invalid json"}, 400
    steps = body.get("steps", [])
    if not isinstance(steps, list):
        return {"ok": False, "error":"bad steps"}, 400
    t = threading.Thread(target=_scenario_loop, args=(steps,), daemon=True)
    t.start()
    _sc_thread = t
    return {"ok": True}

# -------------------- Replay export --------------------
@app.get("/replay/export")
def replay_export():
    """Return a replayable bundle: last N capi events with timestamps."""
    with METRICS_LOCK:
        items = [e for e in list(EVENT_LOG) if e.get("channel")=="capi"][:200]
    bundle = {
        "version": APP_VERSION,
        "created_at": now_iso(),
        "events": [{"event_name": e.get("event_name"), "sent": e.get("sent"), "ts": e.get("ts")} for e in items]
    }
    return jsonify(bundle)

# -------------------- Dataset Quality summary --------------------
@app.get("/dq/keys")
def dq_keys():
    """Summarize presence of key fields in last 100 CAPI events."""
    with METRICS_LOCK:
        items = [e for e in list(EVENT_LOG) if e.get("channel")=="capi"][:100]
    keys = {"external_id":0,"fbp":0,"fbc":0,"client_ip_address":0,"client_user_agent":0}
    for e in items:
        sent = e.get("sent") or {}
        ud = (sent.get("user_data") or {})
        if ud.get("external_id"): keys["external_id"] += 1
        if ud.get("fbp"): keys["fbp"] += 1
        if ud.get("fbc"): keys["fbc"] += 1
        if ud.get("client_ip_address"): keys["client_ip_address"] += 1
        if ud.get("client_user_agent"): keys["client_user_agent"] += 1
    return jsonify({"total": len(items), "keys": keys})

# -------------------- Health & version --------------------
@app.get("/healthz")
def healthz():
    ok = True
    msg = []
    if not PIXEL_ID: msg.append("PIXEL_ID missing")
    if not ACCESS_TOKEN: msg.append("ACCESS_TOKEN missing")
    return jsonify({"ok": ok, "warnings": msg})

@app.get("/version")
def version():
    return jsonify({"version": APP_VERSION})

# -------------------- Entry --------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT","5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
