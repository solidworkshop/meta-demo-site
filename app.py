#!/usr/bin/env python3
# E-commerce Simulator (Light Mode)
# - Three columns: Manual Sender (Pixel / CAPI / Both), Pixel Auto (browser), CAPI Auto (server)
# - Per-column Advanced & Discrepancy controls (independent)
# - Product catalog with unique URLs (/catalog, /product/<sku>)
# - Appended Events section for margin & PLTV (manual + auto), delay window, match-rate degradation
import os, json, time, uuid, random, hashlib, threading
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List
from flask import Flask, request, Response, jsonify, redirect, url_for

# --------------------------- ENV / CONST ---------------------------
PIXEL_ID        = os.getenv("PIXEL_ID", "")
ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN", "")
TEST_EVENT_CODE = os.getenv("TEST_EVENT_CODE", "")
GRAPH_VER       = os.getenv("GRAPH_VER", "v20.0")
BASE_URL        = os.getenv("BASE_URL", "http://127.0.0.1:5000")
CAPI_URL        = f"https://graph.facebook.com/{GRAPH_VER}/{PIXEL_ID}/events" if PIXEL_ID else None
APP_VERSION     = "3.0.0"

app = Flask(__name__)

_ALLOWED_CURRENCIES = {"AUTO","NULL","USD","EUR","GBP","AUD","CAD","JPY"}

def now_iso(): return datetime.now(tz=timezone.utc).isoformat()
def iso_to_unix(ts_iso):
    dt = datetime.fromisoformat(ts_iso.replace("Z","+00:00"))
    return int(dt.replace(tzinfo=timezone.utc).timestamp())
def sha256_norm(s):
    norm = (s or "").strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()

# --------------------------- CATALOG ---------------------------
# You can expand/replace this with a real csv/db; keep ids stable for consistent dedup testing.
CATALOG: Dict[str, Dict[str, Any]] = {
    "SKU-10001": {"name":"Classic Tee", "category":"Tops", "price":19.99},
    "SKU-10002": {"name":"Performance Tee", "category":"Tops", "price":28.00},
    "SKU-20001": {"name":"Slim Jeans", "category":"Bottoms", "price":54.50},
    "SKU-20002": {"name":"Chino Pants", "category":"Bottoms", "price":48.00},
    "SKU-30001": {"name":"Daily Sneakers", "category":"Shoes", "price":79.00},
    "SKU-30002": {"name":"Trail Runners", "category":"Shoes", "price":96.00},
    "SKU-40001": {"name":"Leather Belt", "category":"Accessories", "price":25.00},
    "SKU-50001": {"name":"Throw Blanket", "category":"Home", "price":39.00},
    "SKU-60001": {"name":"Rain Jacket", "category":"Outerwear", "price":120.00},
}

def product_price(sku, default=29.00):
    try: return float(CATALOG[sku]["price"])
    except Exception: return float(default)

# --------------------------- STATE / CONFIG ---------------------------
def _rng_id(): return str(uuid.uuid4())
def to_bool(v, default=False):
    if isinstance(v, bool): return v
    if isinstance(v, (int,float)): return v != 0
    if isinstance(v, str): return v.strip().lower() in ("1","true","t","yes","y","on")
    return default
def clampf(v, lo, hi, default):
    try: x = float(v)
    except Exception: return default
    return max(lo, min(hi, x))
def clampp(v, lo, hi, default):
    try: x = int(v)
    except Exception: return default
    return max(lo, min(hi, x))

CONFIG_LOCK = threading.Lock()

# Per-column profiles: pixel (browser), capi (server)
# Each profile has independent advanced & discrepancy controls
CONFIG: Dict[str, Any] = {
    "global": {
        "seed": "",
        "enable_pixel": True,
        "enable_capi": True,
    },
    "pixel": {
        # Advanced
        "currency_override": "AUTO",
        "null_price": False,
        "null_currency": False,
        "null_event_id": False,
        # Discrepancy & Chaos
        "mismatch_value_pct": 0.0,
        "mismatch_currency": "NONE",   # NONE|PIXEL (applies here) | CAPI (ignored here)
        "desync_event_id": False,
        "duplicate_event_id_n": 0,
        "drop_pixel_every_n": 0,
        "clock_skew_seconds": 0,
    },
    "capi": {
        # Advanced
        "currency_override": "AUTO",
        "tax_rate": 0.08,
        "free_shipping_threshold": 75.0,
        "shipping_options": [4.99, 6.99, 9.99],
        "null_price": False,
        "null_currency": False,
        "null_event_id": False,
        # Discrepancy & Chaos
        "mismatch_value_pct": 0.0,
        "mismatch_currency": "NONE",  # NONE|CAPI (applies here) | PIXEL (ignored here)
        "clock_skew_seconds": 0,
        "net_capi_latency_ms": 0,
        "net_capi_error_rate": 0.0,
        # Auto streamer (server)
        "rps": 0.5,
    },
    # Appended events (server → CAPI)
    "append": {
        "enable_auto_append": False,
        "append_margin": True,
        "cost_pct_min": 0.40,
        "cost_pct_max": 0.80,
        "append_pltv": True,
        "pltv_min": 120.0,
        "pltv_max": 600.0,
        "delay_min_s": 10.0,
        "delay_max_s": 120.0,
        "drop_event_id_pct": 0.0,         # degrade match
        "scramble_external_id_pct": 0.0,  # degrade match
    }
}

_rng = random.Random()
def reseed():
    with CONFIG_LOCK:
        s = (CONFIG["global"].get("seed") or "").strip()
    if s: _rng.seed(s)
    else: _rng.seed()
reseed()

# Simple event memory for appended events
LAST_PIXEL_BASE: Dict[str, Any] = {}  # {"event_id":..., "event_time": int, "user_id":..., "sku":..., "value":...}
LAST_CAPI_BASE:  Dict[str, Any] = {}

# --------------------------- ECONOMICS HELPERS ---------------------------
def _rand_cost(price, lo, hi):
    try:
        p = float(price)
    except Exception:
        return None
    lo = max(0.0, float(lo))
    hi = max(lo, float(hi))
    pct = _rng.uniform(lo, hi)
    return max(0.0, round(p * pct, 2))

def append_margin_pltv(custom_data, cfg_append, single_price=None, contents=None):
    cd = dict(custom_data or {})
    if cfg_append.get("append_margin"):
        margin_val = None
        if contents:
            total = 0.0
            for c in contents:
                price = float(c.get("item_price", 0.0) or 0.0)
                qty   = float(c.get("quantity", 1) or 1)
                cost = _rand_cost(price, cfg_append["cost_pct_min"], cfg_append["cost_pct_max"])
                if cost is None: continue
                total += max(0.0, price - cost) * max(0.0, qty)
            margin_val = round(total, 2)
        elif single_price is not None:
            cost = _rand_cost(single_price, cfg_append["cost_pct_min"], cfg_append["cost_pct_max"])
            if cost is not None:
                margin_val = round(max(0.0, float(single_price) - cost), 2)
        if margin_val is not None:
            cd["margin"] = margin_val
    if cfg_append.get("append_pltv"):
        lo = float(cfg_append.get("pltv_min", 0.0))
        hi = max(lo, float(cfg_append.get("pltv_max", lo)))
        cd["predicted_ltv"] = round(_rng.uniform(lo, hi), 2)
    return cd

# --------------------------- CAPI POST ---------------------------
import requests

def capi_post(server_events):
    with CONFIG_LOCK:
        cfg_capi = dict(CONFIG["capi"])
    if not CONFIG["global"].get("enable_capi"):
        return {"skipped": True, "reason": "enable_capi=false"}
    if not CAPI_URL or not ACCESS_TOKEN:
        return {"skipped": True, "reason": "missing_capi_config"}

    lat = int(cfg_capi.get("net_capi_latency_ms", 0))
    if lat > 0: time.sleep(lat/1000.0)
    if _rng.random() < float(cfg_capi.get("net_capi_error_rate", 0.0)):
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

# --------------------------- MAP SIM → CAPI ---------------------------
def _apply_currency_override(cur, override):
    ov = (override or "AUTO").upper()
    if ov == "NULL":
        return None
    elif ov != "AUTO":
        return ov
    return cur

def _maybe_apply_nulls(cd, null_price=False, null_currency=False):
    if null_currency and "currency" in cd:
        cd["currency"] = None
    if null_price:
        if "value" in cd:
            cd["value"] = None
        if "contents" in cd and isinstance(cd["contents"], list):
            for it in cd["contents"]:
                if isinstance(it, dict) and "item_price" in it:
                    it["item_price"] = None
    return cd

def _apply_clock_skew(unix_ts, skew_seconds):
    try: s = int(skew_seconds or 0)
    except Exception: s = 0
    return int(unix_ts + s)

def build_contents(lines):
    return [{"id": li["product_id"], "quantity": int(li["qty"]), "item_price": float(li["price"])} for li in (lines or [])]

def map_manual_to_capi(base_evt: Dict[str,Any]) -> List[Dict[str,Any]]:
    """
    base_evt fields expected:
      - event_name (PageView/ViewContent/AddToCart/InitiateCheckout/Purchase)
      - event_id, currency, value, contents?, content_ids?
      - channel_cfg: CONFIG["capi"] snapshot
      - client_ip, user_agent
      - event_source_url
      - user_id (plain, we sha256)
      - event_time_unix
    """
    cfg_capi = base_evt["channel_cfg"]
    cd = {}
    if base_evt.get("content_ids"): cd["content_ids"] = list(base_evt["content_ids"])
    if base_evt.get("contents"):    cd["contents"] = list(base_evt["contents"])
    if "value" in base_evt:         cd["value"] = base_evt["value"]
    cd["currency"] = _apply_currency_override(base_evt.get("currency"), cfg_capi["currency_override"])
    cd = _maybe_apply_nulls(cd, null_price=cfg_capi.get("null_price"), null_currency=cfg_capi.get("null_currency"))

    # mismatch value ±%
    mv = float(cfg_capi.get("mismatch_value_pct", 0.0) or 0.0)
    if mv and isinstance(cd.get("value"), (int, float)):
        val = float(cd["value"])
        delta = val * mv
        cd["value"] = round(val + _rng.uniform(-delta, delta), 2)

    # clock skew
    ts = _apply_clock_skew(base_evt["event_time_unix"], cfg_capi.get("clock_skew_seconds"))

    ev = {
        "event_name": base_evt["event_name"],
        "event_time": ts,
        "event_id": None if cfg_capi.get("null_event_id") else base_evt["event_id"],
        "action_source": "website",
        "event_source_url": base_evt["event_source_url"],
        "user_data": {
            "external_id": sha256_norm(base_evt.get("user_id","")),
            "client_ip_address": base_evt["client_ip"],
            "client_user_agent": base_evt["user_agent"],
        },
        "custom_data": cd
    }
    return [ev]

# --------------------------- AUTO STREAM (SERVER → CAPI) ---------------------------
_auto_thread = None
_stop_evt = threading.Event()

DEVICES   = ["mobile","mobile","desktop","tablet"]
COUNTRIES = ["US","US","US","CA","GB","DE","AU"]
SOURCES   = ["direct","seo","sem","email","social","referral"]

def _pick(seq): return random.choice(seq)

def _session():
    return {
        "user_id": f"u_{random.randint(1, 9_999_999)}",
        "device": _pick(DEVICES),
        "country": _pick(COUNTRIES),
        "source": _pick(SOURCES),
    }

def _auto_loop():
    while not _stop_evt.is_set():
        with CONFIG_LOCK:
            cfg_capi = dict(CONFIG["capi"])
        if CONFIG["global"].get("enable_capi"):
            # choose a random product and funnel
            sku = random.choice(list(CATALOG.keys()))
            price = product_price(sku)
            qty = _pick([1,1,1,2])
            contents = [{"id": sku, "quantity": qty, "item_price": price}]
            value = round(qty * price, 2)
            user = _session()
            ts_unix = int(time.time())
            base = {
                "event_name": "Purchase",
                "event_id": _rng_id(),
                "currency": "USD",
                "value": value,
                "contents": contents,
                "event_time_unix": ts_unix,
                "event_source_url": BASE_URL.rstrip("/") + f"/product/{sku}",
                "client_ip": "127.0.0.1",
                "user_agent": "AutoRunner/1.0",
                "user_id": user["user_id"],
                "channel_cfg": cfg_capi
            }
            try:
                evs = map_manual_to_capi(base)
                capi_post(evs)
                # remember last capi event for appends
                with app.app_context():
                    LAST_CAPI_BASE.update({
                        "event_id": base["event_id"],
                        "event_time": ts_unix,
                        "user_id": base["user_id"],
                        "sku": sku,
                        "value": value,
                        "contents": contents,
                        "currency": base["currency"],
                        "event_source_url": base["event_source_url"],
                    })
            except Exception:
                pass
        delay = max(0.05, 1.0 / max(0.1, float(cfg_capi.get("rps", 0.5)))))
        _stop_evt.wait(delay)

@app.get("/auto/start")
def auto_start():
    global _auto_thread
    q = request.args.get("rps")
    with CONFIG_LOCK:
        if q is not None: CONFIG["capi"]["rps"] = clampf(q, 0.1, 10.0, CONFIG["capi"]["rps"])
        if _auto_thread is not None and _auto_thread.is_alive():
            return jsonify({"ok": True, "running": True, "rps": round(CONFIG["capi"]["rps"],2)})
    _stop_evt.clear()
    _auto_thread = threading.Thread(target=_auto_loop, daemon=True)
    _auto_thread.start()
    with CONFIG_LOCK:
        return jsonify({"ok": True, "running": True, "rps": round(CONFIG["capi"]["rps"],2)})

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
        rps = round(CONFIG["capi"]["rps"], 2)
    return jsonify({"ok": True, "running": running, "rps": rps})

# --------------------------- APPENDED EVENTS (SERVER) ---------------------------
def _make_appended_event(base_event: Dict[str,Any], delay_seconds: float) -> Dict[str,Any]:
    with CONFIG_LOCK:
        cfg_capi = dict(CONFIG["capi"])
        cfg_append = dict(CONFIG["append"])

    if not base_event:
        raise ValueError("no base event to append to")

    # Build custom_data starting from base value/contents/currency
    cd = {}
    if "value" in base_event:   cd["value"] = base_event["value"]
    if "contents" in base_event: cd["contents"] = base_event["contents"]
    cd["currency"] = _apply_currency_override(base_event.get("currency"), cfg_capi["currency_override"])

    # Attach margin + PLTV
    single_price = None
    if "contents" in cd and cd["contents"]:
        pass
    elif "value" in cd:
        single_price = cd["value"]
    cd = append_margin_pltv(cd, cfg_append, single_price=single_price, contents=cd.get("contents"))

    # Match degradation: drop event_id or scramble external_id by %
    event_id = base_event["event_id"]
    if _rng.random() < cfg_append.get("drop_event_id_pct", 0.0):
        event_id = None
    ext_id_plain = base_event["user_id"]
    if _rng.random() < cfg_append.get("scramble_external_id_pct", 0.0):
        ext_id_plain = ext_id_plain + "_x"  # simple scramble for demo

    ev = {
        "event_name": "Purchase",  # appended to purchases; adjust as needed
        "event_time": int(base_event["event_time"] + max(0.0, delay_seconds)),
        "event_id": event_id,
        "action_source": "website",
        "event_source_url": base_event.get("event_source_url", BASE_URL),
        "user_data": {
            "external_id": sha256_norm(ext_id_plain),
            "client_ip_address": "127.0.0.1",
            "client_user_agent": "Appender/1.0"
        },
        "custom_data": cd
    }
    # Respect CAPI null toggles
    ev["custom_data"] = _maybe_apply_nulls(
        ev["custom_data"],
        null_price=cfg_capi.get("null_price"),
        null_currency=cfg_capi.get("null_currency")
    )
    # Apply CAPI skew
    ev["event_time"] = _apply_clock_skew(ev["event_time"], cfg_capi.get("clock_skew_seconds"))
    return ev

@app.post("/append/last")
def append_last():
    which = (request.args.get("source","capi") or "capi").lower()
    with CONFIG_LOCK:
        base = dict(LAST_CAPI_BASE if which=="capi" else LAST_PIXEL_BASE)
        cfg_append = dict(CONFIG["append"])
    if not base:
        return {"ok": False, "error":"no base event recorded yet"}, 400
    delay = clampf(request.args.get("delay_s", ""), cfg_append["delay_min_s"], cfg_append["delay_max_s"], cfg_append["delay_min_s"])
    ev = _make_appended_event(base, delay)
    try:
        resp = capi_post([ev])
        return {"ok": True, "meta": resp, "event": ev}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

# Background auto-append to the last seen CAPI base event at random intervals in the window
_append_thread = None
_append_stop = threading.Event()

def _auto_append_loop():
    while not _append_stop.is_set():
        with CONFIG_LOCK:
            cfg = dict(CONFIG["append"])
            base = dict(LAST_CAPI_BASE)
        if cfg.get("enable_auto_append") and base:
            delay = _rng.uniform(cfg["delay_min_s"], cfg["delay_max_s"])
            ev = _make_appended_event(base, delay)
            try:
                capi_post([ev])
            except Exception:
                pass
        _append_stop.wait(5.0)  # check every 5s

def _ensure_append_thread():
    global _append_thread
    if _append_thread is None or not _append_thread.is_alive():
        _append_stop.clear()
        _append_thread = threading.Thread(target=_auto_append_loop, daemon=True)
        _append_thread.start()

# --------------------------- HTML ---------------------------
PAGE_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Demo Store — Light</title>
<style>
:root { --bg:#fff; --fg:#1f2937; --muted:#6b7280; --bd:#e5e7eb; --ok:#10b981; --err:#ef4444; --panel:#f9fafb; }
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial;background:var(--bg);color:var(--fg)}
.container{max-width:1280px;margin:20px auto;padding:0 16px}
.grid3{display:grid;grid-template-columns:repeat(3,minmax(280px,1fr));gap:14px}
.card{border:1px solid var(--bd);border-radius:12px;padding:14px;background:var(--panel)}
h1{margin:6px 0 12px;font-size:22px}
h3{margin:0 0 8px;font-size:16px}
.small{font-size:12px;color:var(--muted)}
.kv{display:flex;gap:8px;align-items:center;margin:4px 0}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
input,select,button{border:1px solid var(--bd);border-radius:10px;padding:8px 10px;background:#fff;color:var(--fg)}
button.btn{cursor:pointer;position:relative}
.btn .tick,.btn .x{position:absolute;right:8px;top:50%;transform:translateY(-50%) scale(.8);opacity:0;transition:.18s}
.btn .tick{color:var(--ok)}.btn .x{color:var(--err)}
.btn.show-tick .tick{opacity:1;transform:translateY(-50%) scale(1)}
.btn.show-err .x{opacity:1;transform:translateY(-50%) scale(1)}
pre{background:#fff;border:1px solid var(--bd);padding:8px;border-radius:10px;max-height:140px;overflow:auto}
.badge{display:inline-block;border:1px solid var(--bd);padding:2px 8px;border-radius:999px;font-size:11px;color:var(--muted)}
hr{border:none;border-top:1px solid var(--bd);margin:8px 0}
a{color:#2563eb;text-decoration:none}
a:hover{text-decoration:underline}
.banner{background:#fef3c7;border:1px solid #fde68a;padding:8px 12px;border-radius:10px;margin:8px 0;color:#92400e}
</style>
<script>
// Pixel init
(function(){
  var s=document.createElement('script'); s.async=true; s.src='https://connect.facebook.net/en_US/fbevents.js';
  document.head.appendChild(s);
  window.fbq = window.fbq || function(){ (fbq.q=fbq.q||[]).push(arguments); };
  fbq.loaded=true; fbq.version='2.0'; fbq.queue=[];
  fbq('init', '__PIXEL_ID__');
})();
function rid(){ return 'evt_' + Math.random().toString(36).slice(2) + Date.now().toString(36); }
function flashIcon(btn, ok){ if(!btn) return; const c=ok?'show-tick':'show-err'; btn.classList.add(c); setTimeout(()=>btn.classList.remove(c), 1000); }
async function jget(u){ const r = await fetch(u); if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); }
async function jok(u,m){ try{ const r=await fetch(u,m); return r.ok; }catch(e){ return false; } }
function copyTxt(t){ navigator.clipboard.writeText(t).catch(()=>{}); }

// ---------- Global badges ----------
async function loadBadges(){
  const cfg = await jget('/config');
  document.getElementById('b_pixel').textContent = 'Pixel: ' + (cfg.global.enable_pixel?'on':'off');
  document.getElementById('b_capi').textContent  = 'CAPI: ' + (cfg.global.enable_capi?'on':'off');
  document.getElementById('b_seed').textContent  = 'Seed: ' + (cfg.global.seed?cfg.global.seed:'none');
}

// ---------- Manual Sender ----------
function currentCurrencyPixel(){
  const mode = (document.getElementById('pixel_currency_override').value || 'AUTO').toUpperCase();
  const nullCur = document.getElementById('pixel_null_currency').checked;
  if (mode==='NULL' || nullCur) return null;
  if (mode!=='AUTO') return mode;
  return 'USD';
}
async function manualSend(btn){
  const channel = document.getElementById('manual_channel').value; // pixel|capi|both
  const sku = document.getElementById('manual_sku').value;
  const qty = parseInt(document.getElementById('manual_qty').value||'1',10);
  const evt = document.getElementById('manual_event').value; // ViewContent/AddToCart/InitiateCheckout/Purchase
  const eidBase = rid();
  const price = parseFloat(document.getElementById('manual_price').value||String(document.getElementById('manual_price').placeholder));
  const value = Math.round(price * (evt==='ViewContent'?1:(evt==='AddToCart'?qty:qty)) * 100)/100;
  const contents = (evt==='ViewContent')?[]:[{id:sku, quantity:qty, item_price: price}];
  const content_ids = (evt==='ViewContent')?[sku]:[];
  const eventURL = window.location.origin + '/product/' + encodeURIComponent(sku);

  // PIXEL arm
  if (channel==='pixel' || channel==='both'){
    const cfgNullP = document.getElementById('pixel_null_price').checked;
    const nullEvent = document.getElementById('pixel_null_event_id').checked;
    const payload = {};
    if (evt==='ViewContent'){
      payload.content_type='product'; payload.content_ids=[sku];
      payload.currency=currentCurrencyPixel(); payload.value= cfgNullP? null : price;
    } else {
      payload.contents=contents; payload.currency=currentCurrencyPixel(); payload.value= cfgNullP? null : value;
    }
    // send
    try { fbq('track', evt, payload, {eventID: nullEvent? null : eidBase}); } catch(e){}
    // ping server so appended events can use it
    await fetch('/metrics/pixel', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({
      event_name: evt, event_id: eidBase, user_id: 'u_manual', value: value, contents: contents, currency: payload.currency, event_source_url: eventURL
    })}).catch(()=>{});
  }

  // CAPI arm
  if (channel==='capi' || channel==='both'){
    const r = await jget('/send/manual?' + new URLSearchParams({
      event_name: evt, sku: sku, qty: String(qty), price: String(price), eid: eidBase
    }));
    if (!r.ok) { flashIcon(btn,false); return; }
  }

  flashIcon(btn, true);
}

// ---------- Pixel Auto ----------
let __pxTimer = null;
function pixelAutoTick(){
  const sku = document.getElementById('manual_sku').value;
  const price = parseFloat(document.getElementById('manual_price').value||String(document.getElementById('manual_price').placeholder));
  const r = Math.random();
  if (r<0.5){
    const payload = {content_type:'product', content_ids:[sku], currency: currentCurrencyPixel(), value: document.getElementById('pixel_null_price').checked? null : price};
    try{ fbq('track','ViewContent',payload,{eventID: document.getElementById('pixel_null_event_id').checked? null : rid()}); }catch(e){}
    fetch('/metrics/pixel',{method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({event_name:'ViewContent', event_id: rid(), user_id:'u_auto_px', value: price, contents:[], currency: payload.currency, event_source_url: window.location.origin+'/product/'+sku})})
  } else if (r<0.8){
    const contents=[{id:sku, quantity:1, item_price: price}];
    const payload = {contents, currency: currentCurrencyPixel(), value: document.getElementById('pixel_null_price').checked? null : price};
    try{ fbq('track','AddToCart',payload,{eventID: document.getElementById('pixel_null_event_id').checked? null : rid()}); }catch(e){}
    fetch('/metrics/pixel',{method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({event_name:'AddToCart', event_id: rid(), user_id:'u_auto_px', value: price, contents, currency: payload.currency, event_source_url: window.location.origin+'/product/'+sku})})
  } else {
    const qty=1; const contents=[{id:sku, quantity:qty, item_price: price}]; const total=price*qty;
    const payload = {contents, currency: currentCurrencyPixel(), value: document.getElementById('pixel_null_price').checked? null : total};
    try{ fbq('track','Purchase',payload,{eventID: document.getElementById('pixel_null_event_id').checked? null : rid()}); }catch(e){}
    fetch('/metrics/pixel',{method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({event_name:'Purchase', event_id: rid(), user_id:'u_auto_px', value: total, contents, currency: payload.currency, event_source_url: window.location.origin+'/product/'+sku})})
  }
}
function pxStart(btn){
  const rps = parseFloat(document.getElementById('px_rps').value||'0.5');
  const interval = Math.max(50, 1000 / Math.max(0.1, rps));
  if (__pxTimer) clearInterval(__pxTimer);
  __pxTimer = setInterval(pixelAutoTick, interval);
  document.getElementById('px_status').textContent = 'Running @ '+(Math.round((1000/interval)*100)/100)+' ev/s';
  document.getElementById('pxStartBtn').disabled = true;
  document.getElementById('pxStopBtn').disabled = false;
  flashIcon(btn, true);
}
function pxStop(btn){
  if (__pxTimer) clearInterval(__pxTimer); __pxTimer=null;
  document.getElementById('px_status').textContent = 'Stopped';
  document.getElementById('pxStartBtn').disabled = false;
  document.getElementById('pxStopBtn').disabled = true;
  flashIcon(btn,true);
}

// ---------- Appended Events ----------
async function appendLast(btn, source){
  const delay = parseFloat(document.getElementById('append_delay').value||'10');
  const r = await jget('/append/last?'+new URLSearchParams({source, delay_s:String(delay)}));
  flashIcon(btn, !!r.ok);
}
async function appendToggle(btn){
  const on = document.getElementById('append_auto').checked;
  const ok = await jok('/config/append', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({enable_auto_append: on})});
  flashIcon(btn, ok);
}

// ---------- Save per-column controls ----------
async function savePixel(btn){
  const body = {
    pixel: {
      currency_override: document.getElementById('pixel_currency_override').value,
      null_price: document.getElementById('pixel_null_price').checked,
      null_currency: document.getElementById('pixel_null_currency').checked,
      null_event_id: document.getElementById('pixel_null_event_id').checked,
      mismatch_value_pct: parseFloat(document.getElementById('pixel_mismatch_value_pct').value||'0'),
      mismatch_currency: document.getElementById('pixel_mismatch_currency').value,
      desync_event_id: document.getElementById('pixel_desync_event_id').checked,
      duplicate_event_id_n: parseInt(document.getElementById('pixel_duplicate_event_id_n').value||'0',10),
      drop_pixel_every_n: parseInt(document.getElementById('pixel_drop_every_n').value||'0',10),
      clock_skew_seconds: parseInt(document.getElementById('pixel_clock_skew').value||'0',10),
    }
  };
  const ok = await jok('/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  flashIcon(btn, ok); loadBadges();
}
async function saveCapi(btn){
  const body = {
    capi: {
      currency_override: document.getElementById('capi_currency_override').value,
      tax_rate: parseFloat(document.getElementById('capi_tax_rate').value||'0.08'),
      free_shipping_threshold: parseFloat(document.getElementById('capi_free_ship').value||'75'),
      shipping_options: (document.getElementById('capi_shipping').value||'').split(',').map(s=>parseFloat(s.trim())).filter(x=>!isNaN(x)),
      null_price: document.getElementById('capi_null_price').checked,
      null_currency: document.getElementById('capi_null_currency').checked,
      null_event_id: document.getElementById('capi_null_event_id').checked,
      mismatch_value_pct: parseFloat(document.getElementById('capi_mismatch_value_pct').value||'0'),
      mismatch_currency: document.getElementById('capi_mismatch_currency').value,
      clock_skew_seconds: parseInt(document.getElementById('capi_clock_skew').value||'0',10),
      net_capi_latency_ms: parseInt(document.getElementById('capi_latency').value||'0',10),
      net_capi_error_rate: parseFloat(document.getElementById('capi_error_rate').value||'0'),
      rps: parseFloat(document.getElementById('rps').value||'0.5'),
    }
  };
  const ok = await jok('/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  flashIcon(btn, ok); loadBadges();
}
async function saveAppend(btn){
  const body = {
    append: {
      enable_auto_append: document.getElementById('append_auto').checked,
      append_margin: document.getElementById('append_margin').checked,
      cost_pct_min: parseFloat(document.getElementById('cost_pct_min').value||'0.4'),
      cost_pct_max: parseFloat(document.getElementById('cost_pct_max').value||'0.8'),
      append_pltv: document.getElementById('append_pltv').checked,
      pltv_min: parseFloat(document.getElementById('pltv_min').value||'120'),
      pltv_max: parseFloat(document.getElementById('pltv_max').value||'600'),
      delay_min_s: parseFloat(document.getElementById('delay_min_s').value||'10'),
      delay_max_s: parseFloat(document.getElementById('delay_max_s').value||'120'),
      drop_event_id_pct: parseFloat(document.getElementById('drop_event_id_pct').value||'0'),
      scramble_external_id_pct: parseFloat(document.getElementById('scramble_external_id_pct').value||'0'),
    }
  };
  const ok = await jok('/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  flashIcon(btn, ok);
}

// ---------- Load controls ----------
async function loadAll(){
  await loadBadges();
  const cfg = await jget('/config');

  // Manual defaults
  const firstSku = Object.keys(cfg.catalog)[0] || 'SKU-10001';
  document.getElementById('manual_sku').innerHTML = Object.keys(cfg.catalog).map(s=>`<option value="${s}">${s} — ${cfg.catalog[s].name}</option>`).join('');
  document.getElementById('manual_sku').value = firstSku;
  document.getElementById('manual_price').placeholder = cfg.catalog[firstSku].price;

  // Pixel column
  const p = cfg.pixel;
  document.getElementById('pixel_currency_override').value = p.currency_override;
  document.getElementById('pixel_null_price').checked = !!p.null_price;
  document.getElementById('pixel_null_currency').checked = !!p.null_currency;
  document.getElementById('pixel_null_event_id').checked = !!p.null_event_id;
  document.getElementById('pixel_mismatch_value_pct').value = p.mismatch_value_pct||0;
  document.getElementById('pixel_mismatch_currency').value = p.mismatch_currency||'NONE';
  document.getElementById('pixel_desync_event_id').checked = !!p.desync_event_id;
  document.getElementById('pixel_duplicate_event_id_n').value = p.duplicate_event_id_n||0;
  document.getElementById('pixel_drop_every_n').value = p.drop_pixel_every_n||0;
  document.getElementById('pixel_clock_skew').value = p.clock_skew_seconds||0;

  // CAPI column
  const c = cfg.capi;
  document.getElementById('capi_currency_override').value = c.currency_override;
  document.getElementById('capi_tax_rate').value = c.tax_rate;
  document.getElementById('capi_free_ship').value = c.free_shipping_threshold;
  document.getElementById('capi_shipping').value = (c.shipping_options||[]).join(', ');
  document.getElementById('capi_null_price').checked = !!c.null_price;
  document.getElementById('capi_null_currency').checked = !!c.null_currency;
  document.getElementById('capi_null_event_id').checked = !!c.null_event_id;
  document.getElementById('capi_mismatch_value_pct').value = c.mismatch_value_pct||0;
  document.getElementById('capi_mismatch_currency').value = c.mismatch_currency||'NONE';
  document.getElementById('capi_clock_skew').value = c.clock_skew_seconds||0;
  document.getElementById('capi_latency').value = c.net_capi_latency_ms||0;
  document.getElementById('capi_error_rate').value = c.net_capi_error_rate||0;
  document.getElementById('rps').value = c.rps||0.5;

  // Append panel
  const a = cfg.append;
  document.getElementById('append_auto').checked = !!a.enable_auto_append;
  document.getElementById('append_margin').checked = !!a.append_margin;
  document.getElementById('cost_pct_min').value = a.cost_pct_min;
  document.getElementById('cost_pct_max').value = a.cost_pct_max;
  document.getElementById('append_pltv').checked = !!a.append_pltv;
  document.getElementById('pltv_min').value = a.pltv_min;
  document.getElementById('pltv_max').value = a.pltv_max;
  document.getElementById('delay_min_s').value = a.delay_min_s;
  document.getElementById('delay_max_s').value = a.delay_max_s;
  document.getElementById('drop_event_id_pct').value = a.drop_event_id_pct||0;
  document.getElementById('scramble_external_id_pct').value = a.scramble_external_id_pct||0;
}

// Manual price sync on SKU change
async function onSkuChange(sel){
  const cfg = await jget('/config');
  const sku = sel.value;
  document.getElementById('manual_price').placeholder = cfg.catalog[sku]?.price ?? 29.00;
}

window.addEventListener('load', async () => { await loadAll(); });
</script>
</head>
<body>
  <div class="container">
    <h1>Demo Store (Light) <span class="small">v__APP_VERSION__</span></h1>
    <div class="banner">Pixel ID: <b>__PIXEL_ID__</b> · CAPI: <b>__CAPI_ON__</b> · Test Code: <b>__TEST_ON__</b></div>
    <div class="row" style="margin:8px 0 12px;">
      <span class="badge" id="b_pixel">Pixel: ?</span>
      <span class="badge" id="b_capi">CAPI: ?</span>
      <span class="badge" id="b_seed">Seed: none</span>
      <a class="badge" href="/catalog">Catalog</a>
    </div>

    <!-- Three main columns -->
    <div class="grid3">
      <!-- Column 1: Manual Sender -->
      <div class="card">
        <h3>Manual Sender</h3>
        <div class="kv"><label>Channel</label>
          <select id="manual_channel"><option value="pixel">Pixel</option><option value="capi">CAPI</option><option value="both">Both</option></select>
        </div>
        <div class="kv"><label>Event</label>
          <select id="manual_event"><option>ViewContent</option><option>AddToCart</option><option>InitiateCheckout</option><option>Purchase</option></select>
        </div>
        <div class="kv"><label>SKU</label>
          <select id="manual_sku" onchange="onSkuChange(this)"></select>
        </div>
        <div class="kv"><label>Qty</label><input id="manual_qty" type="number" min="1" step="1" value="1"/></div>
        <div class="kv"><label>Price ($)</label><input id="manual_price" type="number" step="0.01" min="0" placeholder="29.00"/></div>
        <div class="row"><button class="btn" onclick="manualSend(this)">Send
          <span class="tick" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M20.3 5.7a1 1 0 0 1 0 1.4l-10 10a1 1 0 0 1-1.4 0l-5-5a1 1 0 1 1 1.4-1.4l4.3 4.3L18.9 5.7a1 1 0 0 1 1.4 0z"/></svg></span>
          <span class="x" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M18.3 5.7a1 1 0 0 1 0 1.4L13.4 12l4.9 4.9a1 1 0 1 1-1.4 1.4L12 13.4l-4.9 4.9a1 1 0 0 1-1.4-1.4L10.6 12 5.7 7.1A1 1 0 1 1 7.1 5.7L12 10.6l4.9-4.9a1 1 0 0 1 1.4 0z"/></svg></span></button></div>
        <hr/>
        <h3>Advanced (Manual – Pixel)</h3>
        <div class="kv"><label>Currency</label>
          <select id="pixel_currency_override"><option>AUTO</option><option>USD</option><option>EUR</option><option>GBP</option><option>AUD</option><option>CAD</option><option>JPY</option><option>NULL</option></select>
        </div>
        <div class="kv"><label>Null price</label><input id="pixel_null_price" type="checkbox"/></div>
        <div class="kv"><label>Null currency</label><input id="pixel_null_currency" type="checkbox"/></div>
        <div class="kv"><label>Null event_id</label><input id="pixel_null_event_id" type="checkbox"/></div>
        <h3 class="small" style="margin-top:8px;">Discrepancy (Manual – Pixel)</h3>
        <div class="kv"><label>Mismatch value ±%</label><input id="pixel_mismatch_value_pct" type="number" step="0.01" min="0" max="1" value="0"/></div>
        <div class="kv"><label>Mismatch currency</label><select id="pixel_mismatch_currency"><option>NONE</option><option>PIXEL</option></select></div>
        <div class="kv"><label>Desync event_id</label><input id="pixel_desync_event_id" type="checkbox"/></div>
        <div class="kv"><label>Duplicate every N</label><input id="pixel_duplicate_event_id_n" type="number" step="1" min="0" value="0"/></div>
        <div class="kv"><label>Drop every N</label><input id="pixel_drop_every_n" type="number" step="1" min="0" value="0"/></div>
        <div class="kv"><label>Clock skew (s)</label><input id="pixel_clock_skew" type="number" step="1" value="0"/></div>
        <div class="row"><button class="btn" onclick="savePixel(this)">Save Pixel Controls<span class="tick"></span><span class="x"></span></button></div>
      </div>

      <!-- Column 2: Pixel Auto -->
      <div class="card">
        <h3>Pixel Auto (browser → Pixel)</h3>
        <div class="kv"><label>Events/sec</label><input id="px_rps" type="number" step="0.1" min="0.1" value="0.5"/></div>
        <div class="row">
          <button id="pxStartBtn" class="btn" onclick="pxStart(this)">Start<span class="tick"></span><span class="x"></span></button>
          <button id="pxStopBtn" class="btn" onclick="pxStop(this)" disabled>Stop<span class="tick"></span><span class="x"></span></button>
        </div>
        <p id="px_status" class="small">Stopped</p>
        <hr/>
        <p class="small">Uses the Pixel controls in the Manual column (same profile) so you can keep them in one place for browser events.</p>
      </div>

      <!-- Column 3: CAPI Auto -->
      <div class="card">
        <h3>Auto Stream (server → CAPI)</h3>
        <div class="kv"><label>Sessions/sec</label><input id="rps" type="number" step="0.1" min="0.1" value="0.5"/></div>
        <div class="row">
          <button id="startBtn" class="btn" onclick="(async(b)=>{const ok=await jok('/auto/start?'+new URLSearchParams({rps:document.getElementById('rps').value||'0.5'})); flashIcon(b,ok) })(this)">Start<span class="tick"></span><span class="x"></span></button>
          <button id="stopBtn" class="btn" onclick="(async(b)=>{const ok=await jok('/auto/stop'); flashIcon(b,ok)})(this)">Stop<span class="tick"></span><span class="x"></span></button>
        </div>
        <p id="status" class="small">…</p>
        <hr/>
        <h3>Advanced (CAPI)</h3>
        <div class="kv"><label>Currency</label>
          <select id="capi_currency_override"><option>AUTO</option><option>USD</option><option>EUR</option><option>GBP</option><option>AUD</option><option>CAD</option><option>JPY</option><option>NULL</option></select>
        </div>
        <div class="kv"><label>Tax rate (0–1)</label><input id="capi_tax_rate" type="number" step="0.001" min="0" max="1" value="0.08"/></div>
        <div class="kv"><label>Free ship ≥ ($)</label><input id="capi_free_ship" type="number" step="0.01" min="0" value="75"/></div>
        <div class="kv"><label>Shipping ($, comma)</label><input id="capi_shipping" type="text" value="4.99, 6.99, 9.99"/></div>
        <div class="kv"><label>Null price</label><input id="capi_null_price" type="checkbox"/></div>
        <div class="kv"><label>Null currency</label><input id="capi_null_currency" type="checkbox"/></div>
        <div class="kv"><label>Null event_id</label><input id="capi_null_event_id" type="checkbox"/></div>
        <h3 class="small" style="margin-top:8px;">Discrepancy (CAPI)</h3>
        <div class="kv"><label>Mismatch value ±%</label><input id="capi_mismatch_value_pct" type="number" step="0.01" min="0" max="1" value="0"/></div>
        <div class="kv"><label>Mismatch currency</label><select id="capi_mismatch_currency"><option>NONE</option><option>CAPI</option></select></div>
        <div class="kv"><label>Clock skew (s)</label><input id="capi_clock_skew" type="number" step="1" value="0"/></div>
        <div class="kv"><label>Latency (ms)</label><input id="capi_latency" type="number" step="1" min="0" value="0"/></div>
        <div class="kv"><label>Error rate</label><input id="capi_error_rate" type="number" step="0.01" min="0" max="1" value="0"/></div>
        <div class="row"><button class="btn" onclick="saveCapi(this)">Save CAPI Controls<span class="tick"></span><span class="x"></span></button></div>
      </div>
    </div>

    <!-- Appended Events Section -->
    <div class="card" style="margin-top:14px;">
      <h3>Appended Events (server → CAPI)</h3>
      <p class="small">Send margin & PLTV as appended signals after the original event. You can degrade match and adjust delay window.</p>
      <div class="row">
        <label><input id="append_auto" type="checkbox"/> Auto-append (periodic)</label>
        <button class="btn" onclick="appendToggle(this)">Apply<span class="tick"></span><span class="x"></span></button>
      </div>
      <div class="row">
        <div class="kv"><label>Margin</label><input id="append_margin" type="checkbox" checked/></div>
        <div class="kv"><label>Cost% min</label><input id="cost_pct_min" type="number" step="0.01" min="0" max="1" value="0.4"/></div>
        <div class="kv"><label>Cost% max</label><input id="cost_pct_max" type="number" step="0.01" min="0" max="1" value="0.8"/></div>
        <div class="kv"><label>PLTV</label><input id="append_pltv" type="checkbox" checked/></div>
        <div class="kv"><label>PLTV min</label><input id="pltv_min" type="number" step="0.01" min="0" value="120"/></div>
        <div class="kv"><label>PLTV max</label><input id="pltv_max" type="number" step="0.01" min="0" value="600"/></div>
      </div>
      <div class="row">
        <div class="kv"><label>Delay min (s)</label><input id="delay_min_s" type="number" step="0.1" min="0" value="10"/></div>
        <div class="kv"><label>Delay max (s)</label><input id="delay_max_s" type="number" step="0.1" min="0" value="120"/></div>
        <div class="kv"><label>Manual delay (s)</label><input id="append_delay" type="number" step="0.1" min="0" value="10"/></div>
      </div>
      <div class="row">
        <div class="kv"><label>Drop event_id %</label><input id="drop_event_id_pct" type="number" step="0.01" min="0" max="1" value="0"/></div>
        <div class="kv"><label>Scramble external_id %</label><input id="scramble_external_id_pct" type="number" step="0.01" min="0" max="1" value="0"/></div>
      </div>
      <div class="row">
        <button class="btn" onclick="appendLast(this,'capi')">Append to last CAPI<span class="tick"></span><span class="x"></span></button>
        <button class="btn" onclick="appendLast(this,'pixel')">Append to last Pixel<span class="tick"></span><span class="x"></span></button>
        <button class="btn" onclick="saveAppend(this)">Save Append Controls<span class="tick"></span><span class="x"></span></button>
      </div>
    </div>
  </div>

  <noscript><img height="1" width="1" style="display:none" src="https://www.facebook.com/tr?id=__PIXEL_ID__&ev=PageView&noscript=1"/></noscript>
</body></html>
"""

# --------------------------- ROUTES: HTML ---------------------------
@app.get("/")
def home():
    html = PAGE_HTML.replace("__APP_VERSION__", APP_VERSION)\
                    .replace("__PIXEL_ID__", PIXEL_ID or "")\
                    .replace("__CAPI_ON__", "on" if (CAPI_URL and ACCESS_TOKEN) else "off")\
                    .replace("__TEST_ON__", "on" if TEST_EVENT_CODE else "off")
    return Response(html, mimetype="text/html")

@app.get("/catalog")
def catalog():
    # super simple catalog index
    rows = "".join([f'<li><a href="/product/{sku}">{sku} — {p["name"]} (${p["price"]})</a></li>' for sku,p in CATALOG.items()])
    page = f"""<!doctype html><meta charset="utf-8"><title>Catalog</title>
    <div style="font-family:system-ui;padding:16px">
      <h2>Catalog</h2>
      <ul>{rows}</ul>
      <p><a href="/">← Back</a></p>
    </div>"""
    return Response(page, mimetype="text/html")

@app.get("/product/<sku>")
def product_page(sku):
    p = CATALOG.get(sku)
    if not p:
        return redirect(url_for('catalog'))
    # very minimal product page; Pixel fires client-side when you click Manual/Auto in main UI
    html = f"""<!doctype html><meta charset="utf-8"><title>{p["name"]}</title>
    <div style="font-family:system-ui;padding:16px">
      <h2>{p["name"]}</h2>
      <p>SKU: {sku} · Category: {p["category"]} · Price: ${p["price"]}</p>
      <p><a href="/">← Back</a></p>
    </div>"""
    return Response(html, mimetype="text/html")

# --------------------------- ROUTES: CONFIG ---------------------------
@app.get("/config")
def get_config():
    with CONFIG_LOCK:
        out = json.loads(json.dumps(CONFIG))
        out["catalog"] = CATALOG
    # ensure append thread if requested
    if out["append"].get("enable_auto_append"): _ensure_append_thread()
    return jsonify(out)

@app.post("/config")
def set_config():
    try:
        body = request.get_json(force=True) or {}
    except Exception:
        return {"ok": False, "error":"invalid json"}, 400
    with CONFIG_LOCK:
        for key in ("global","pixel","capi","append"):
            if key in body and isinstance(body[key], dict):
                CONFIG[key].update(body[key])
    if CONFIG["append"].get("enable_auto_append"): _ensure_append_thread()
    return jsonify({"ok": True})

@app.post("/config/append")
def set_append_only():
    try:
        body = request.get_json(force=True) or {}
    except Exception:
        return {"ok": False}, 400
    with CONFIG_LOCK:
        CONFIG["append"].update({k:v for k,v in body.items() if k in CONFIG["append"]})
    if CONFIG["append"].get("enable_auto_append"): _ensure_append_thread()
    return {"ok": True}

# --------------------------- ROUTES: SERVER SENDS ---------------------------
@app.get("/send/manual")
def send_manual_capi():
    # Minimal manual → CAPI bridge for the Manual Sender "CAPI" / "Both"
    evt = request.args.get("event_name","ViewContent")
    sku = request.args.get("sku","SKU-10001")
    qty = clampp(request.args.get("qty","1"), 1, 999999, 1)
    price = clampf(request.args.get("price","0"), 0.0, 1e7, product_price(sku))
    eid = request.args.get("eid", _rng_id())
    contents = [] if evt=="ViewContent" else [{"product_id": sku, "qty": int(qty), "price": price}]
    content_ids = [sku] if evt=="ViewContent" else []
    value = price if evt=="ViewContent" else round(int(qty)*price, 2)
    with CONFIG_LOCK:
        cfg_capi = dict(CONFIG["capi"])
    base = {
        "event_name": evt,
        "event_id": eid,
        "currency": "USD",
        "value": value,
        "contents": build_contents(contents),
        "content_ids": content_ids,
        "event_time_unix": int(time.time()),
        "event_source_url": BASE_URL.rstrip("/") + f"/product/{sku}",
        "client_ip": request.remote_addr or "127.0.0.1",
        "user_agent": request.headers.get("User-Agent","Manual/1.0"),
        "user_id": "u_manual",
        "channel_cfg": cfg_capi
    }
    try:
        evs = map_manual_to_capi(base)
        resp = capi_post(evs)
        # remember last capi base
        LAST_CAPI_BASE.update({
            "event_id": eid,
            "event_time": base["event_time_unix"],
            "user_id": base["user_id"],
            "sku": sku,
            "value": value,
            "contents": base["contents"],
            "currency": base["currency"],
            "event_source_url": base["event_source_url"],
        })
        return jsonify({"ok": True, "meta": resp})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# Pixel metrics intake so server can remember last pixel base for appended events
@app.post("/metrics/pixel")
def metrics_pixel():
    try:
        body = request.get_json(force=True) or {}
    except Exception:
        return {"ok": False}, 400
    LAST_PIXEL_BASE.update({
        "event_id": body.get("event_id"),
        "event_time": int(time.time()),
        "user_id": body.get("user_id","u_unknown"),
        "sku": (body.get("contents") or [{}])[0].get("id") if body.get("contents") else (body.get("content_ids") or [None])[0],
        "value": body.get("value"),
        "contents": body.get("contents"),
        "currency": body.get("currency"),
        "event_source_url": body.get("event_source_url") or BASE_URL
    })
    return {"ok": True}

# --------------------------- AUTO STATUS ---------------------------
@app.get("/auto/status")
def auto_status_simple():
    running = _auto_thread is not None and _auto_thread.is_alive()
    with CONFIG_LOCK:
        rps = round(CONFIG["capi"]["rps"], 2)
    return jsonify({"ok": True, "running": running, "rps": rps})

# --------------------------- MAIN ---------------------------
if __name__ == "__main__":
    # kick off append thread if enabled
    if CONFIG["append"].get("enable_auto_append"):
        _ensure_append_thread()
    port = int(os.getenv("PORT","5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
