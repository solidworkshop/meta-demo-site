#!/usr/bin/env python3
# Flask app: Demo page (Meta Pixel + buttons), /ingest (CAPI forwarder),
# Start/Stop auto streamer (server-side CAPI events), Advanced Controls,
# Bad data toggles, and new Margin + PLTV appends.
import os, json, hashlib, time, random, uuid, threading
from datetime import datetime, timezone
from flask import Flask, request, Response, jsonify, has_request_context
import requests
from dotenv import load_dotenv

# ---------- config ----------
load_dotenv()
PIXEL_ID        = os.getenv("PIXEL_ID", "")
ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN", "")
TEST_EVENT_CODE = os.getenv("TEST_EVENT_CODE", "")   # optional but helpful
BASE_URL        = os.getenv("BASE_URL", "http://127.0.0.1:5000")
GRAPH_VER       = os.getenv("GRAPH_VER", "v20.0")
CAPI_URL        = f"https://graph.facebook.com/{GRAPH_VER}/{PIXEL_ID}/events"

app = Flask(__name__)

# ---------- runtime sim config (editable via UI) ----------
CONFIG_LOCK = threading.Lock()
CONFIG = {
    "rps": 0.5,                   # sessions per second in auto mode
    "p_add_to_cart": 0.35,        # P(add_to_cart | product_view)
    "p_begin_checkout": 0.70,     # P(begin_checkout | add_to_cart)
    "p_purchase": 0.70,           # P(purchase | begin_checkout)
    "price_min": 10.0,            # min product price
    "price_max": 120.0,           # max product price
    "free_shipping_threshold": 75.0,
    "shipping_options": [4.99, 6.99, 9.99],
    "tax_rate": 0.08,             # 8% demo tax

    # Bad data toggles (apply to Pixel + CAPI)
    "null_price": False,
    "null_currency": False,
    "null_event_id": False,

    # NEW: Append extra metrics
    "append_margin": True,        # whether to include "margin" in custom_data
    "margin_rate": 0.35,          # margin = merchandise_subtotal * margin_rate (0..1)
    "append_pltv": True,          # whether to include "predicted_ltv" in custom_data
    "pltv_min": 120.0,            # PLTV lower bound
    "pltv_max": 600.0,            # PLTV upper bound
}

# ---------- helpers ----------
def sha256_norm(s):
    norm = (s or "").strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()

def now_unix():
    return int(datetime.now(tz=timezone.utc).timestamp())

def iso_to_unix(ts_iso):
    dt = datetime.fromisoformat(ts_iso.replace("Z","+00:00"))
    return int(dt.replace(tzinfo=timezone.utc).timestamp())

def capi_post(server_events):
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

def build_contents(lines):
    # lines: [{"product_id","qty","price"}]
    return [{"id": li["product_id"], "quantity": int(li["qty"]), "item_price": float(li["price"])} for li in lines]

def contents_subtotal(contents):
    s = 0.0
    for c in contents or []:
        try:
            q = float(c.get("quantity", 0))
            p = float(c.get("item_price", 0))
            s += q * p
        except Exception:
            pass
    return round(s, 2)

def get_cfg_snapshot():
    with CONFIG_LOCK:
        return dict(CONFIG)

def append_margin_pltv(custom_data, cfg, fallback_value=None, contents=None):
    """Append margin and predicted_ltv as requested."""
    if custom_data is None:
        custom_data = {}

    # Merchandise subtotal: prefer contents, else fallback on provided value
    subtotal = None
    if contents:
        subtotal = contents_subtotal(contents)
    elif fallback_value is not None:
        try:
            subtotal = float(fallback_value)
        except Exception:
            subtotal = None

    # margin
    if cfg.get("append_margin") and subtotal is not None:
        mr = float(cfg.get("margin_rate", 0.0))
        custom_data["margin"] = round(max(0.0, mr) * max(0.0, subtotal), 2)

    # predicted_ltv (PLTV)
    if cfg.get("append_pltv"):
        lo = float(cfg.get("pltv_min", 0.0))
        hi = float(cfg.get("pltv_max", max(lo, 0.0)))
        if hi < lo:
            hi = lo
        custom_data["predicted_ltv"] = round(random.uniform(lo, hi), 2)

    return custom_data

def apply_bad_data_flags(evts):
    """Mutate CAPI events per bad-data toggles (price/currency/event_id)."""
    cfg = get_cfg_snapshot()
    if not evts:
        return evts
    for ev in evts:
        # null event_id
        if cfg.get("null_event_id"):
            ev["event_id"] = None
        cd = ev.get("custom_data") or {}
        # null currency
        if cfg.get("null_currency") and "currency" in cd:
            cd["currency"] = None
        # null price/value and item_price in contents
        if cfg.get("null_price"):
            if "value" in cd:
                cd["value"] = None
            if "contents" in cd and isinstance(cd["contents"], list):
                for c in cd["contents"]:
                    if isinstance(c, dict) and "item_price" in c:
                        c["item_price"] = None
        ev["custom_data"] = cd
    return evts

# Map a simulator-style event → 0..1 CAPI events
def map_sim_event_to_capi(e):
    et   = e.get("event_type")
    sess = e.get("user", {})
    ctx  = e.get("context", {})
    currency = ctx.get("currency","USD")
    ts   = iso_to_unix(e["timestamp"])
    cfg  = get_cfg_snapshot()

    # infer a URL
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
        "event_time": ts,
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
            "currency": currency
        }
        cd = append_margin_pltv(cd, cfg, fallback_value=cd["value"], contents=None)
        out = [{**base, "event_name":"ViewContent", "custom_data": cd}]

    elif et == "add_to_cart":
        li = e["line_item"]
        contents = [{"id": li["product_id"], "quantity": int(li["qty"]), "item_price": float(li["price"])}]
        cd = {
            "content_type":"product",
            "content_ids":[li["product_id"]],
            "contents": contents,
            "value": float(li["qty"]) * float(li["price"]),
            "currency": currency
        }
        cd = append_margin_pltv(cd, cfg, fallback_value=cd["value"], contents=contents)
        out = [{**base, "event_name":"AddToCart", "custom_data": cd}]

    elif et == "begin_checkout":
        cart = e["cart"]
        contents = build_contents(cart)
        total = float(e.get("total", 0.0))
        cd = {
            "contents": contents,
            "value": total,
            "currency": currency
        }
        # For margin, use merchandise subtotal (derived from contents), not total
        cd = append_margin_pltv(cd, cfg, fallback_value=contents_subtotal(contents), contents=contents)
        out = [{**base, "event_name":"InitiateCheckout", "custom_data": cd}]

    elif et == "purchase":
        items = e["items"]
        contents = build_contents(items)
        total = float(e.get("total", 0.0))
        cd = {
            "contents": contents,
            "value": total,
            "currency": currency
        }
        # Margin again on merchandise subtotal
        cd = append_margin_pltv(cd, cfg, fallback_value=contents_subtotal(contents), contents=contents)
        out = [{**base, "event_name":"Purchase", "custom_data": cd}]

    elif et == "return_initiated":
        pid = e.get("product_id")
        cd = {
            "content_type":"product",
            "content_ids":[pid] if pid else [],
            "value": 0.0, "currency": currency
        }
        cd = append_margin_pltv(cd, cfg, fallback_value=0.0, contents=None)
        out = [{**base, "event_name":"ReturnInitiated", "custom_data": cd}]

    # Apply bad-data toggles before returning
    return apply_bad_data_flags(out)

# ---------- HTML (success/fail icons + Advanced Controls + Bad Data + Margin/PLTV) ----------
PAGE_HTML = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Demo Store</title>
<style>
  :root {{ --bd:#ddd; --fg:#222; --muted:#555; --ok:#0a8a30; --err:#b00020; }}
  body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; margin: 2rem; color: var(--fg); }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; }}
  .card {{ border:1px solid var(--bd); border-radius:12px; padding:16px; }}
  .row {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; }}
  .col {{ display:flex; flex-direction:column; gap:8px; }}
  input[type=number], input[type=text] {{ width: 170px; padding:6px 8px; }}
  .small {{ font-size: 12px; color: var(--muted); }}

  .btn {{
    position: relative; padding: 8px 36px 8px 12px;
    border-radius: 10px; border:1px solid var(--bd);
    background:#fff; cursor:pointer; line-height:1.1;
  }}
  .btn:disabled {{ opacity: .6; cursor: not-allowed; }}
  .btn .tick, .btn .x {{
    position:absolute; right:10px; top:50%;
    transform: translateY(-50%) scale(0.8);
    opacity:0; transition: opacity .18s ease, transform .18s ease;
    pointer-events:none; display:inline-flex; align-items:center; justify-content:center;
  }}
  .btn .tick {{ color: var(--ok); }}
  .btn .x    {{ color: var(--err); }}
  .btn .tick svg, .btn .x svg {{ width:18px; height:18px; }}
  .btn.show-tick .tick {{ opacity:1; transform: translateY(-50%) scale(1); }}
  .btn.show-err  .x    {{ opacity:1; transform: translateY(-50%) scale(1); }}

  .kv {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; }}
  .kv label {{ width: 240px; font-size: 14px; color: var(--muted); }}

  .toggle {{ display:flex; align-items:center; gap:10px; }}
  .toggle input[type=checkbox] {{ width:18px; height:18px; }}
</style>
<script>
/* Meta Pixel */
!function(f,b,e,v,n,t,s){{if(f.fbq)return;n=f.fbq=function(){{n.callMethod?
n.callMethod.apply(n,arguments):n.queue.push(arguments)}};if(!f._fbq)f._fbq=n;
n.push=n; n.loaded=!0; n.version='2.0'; n.queue=[]; t=b.createElement(e);
t.async=!0; t.src=v; s=b.getElementsByTagName(e)[0]; s.parentNode.insertBefore(t,s)
}}(window, document,'script','https://connect.facebook.net/en_US/fbevents.js');
fbq('init', '{PIXEL_ID}');
fbq('track', 'PageView');

/* Helpers */
function rid(){{ return 'evt_' + Math.random().toString(36).slice(2) + Date.now().toString(36); }}
function flashIcon(btn, ok) {{
  if (!btn) return;
  const cls = ok ? 'show-tick' : 'show-err';
  btn.classList.add(cls);
  setTimeout(()=> btn.classList.remove(cls), 1100);
}}
async function fetchOK(url, opts) {{
  try {{ const r = await fetch(url, opts); return r.ok; }} catch (_) {{ return false; }}
}}
async function fetchJSON(url) {{
  const r = await fetch(url);
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}}

/* Bad-data toggles from checkboxes */
function readBadToggles(){{
  return {{
    null_price: document.getElementById('null_price').checked,
    null_currency: document.getElementById('null_currency').checked,
    null_event_id: document.getElementById('null_event_id').checked,
  }};
}}

/* Append controls (margin + PLTV) */
function readAppendControls(){{
  return {{
    append_margin: document.getElementById('append_margin').checked,
    margin_rate: parseFloat(document.getElementById('margin_rate').value || '0.35'),
    append_pltv: document.getElementById('append_pltv').checked,
    pltv_min: parseFloat(document.getElementById('pltv_min').value || '120'),
    pltv_max: parseFloat(document.getElementById('pltv_max').value || '600'),
  }};
}}
function randBetween(a,b){{ const lo=Math.min(a,b), hi=Math.max(a,b); return lo + Math.random()*(hi-lo); }}

/* Pixel event helpers: attach margin & predicted_ltv if toggled */
function attachMarginPltv(payload, valueForMargin){{
  const a = readAppendControls();
  if (a.append_margin && typeof valueForMargin === 'number') {{
    const mr = Math.max(0, a.margin_rate || 0);
    payload.margin = Math.round(valueForMargin * mr * 100)/100;
  }}
  if (a.append_pltv) {{
    const pltv = randBetween(a.pltv_min||0, a.pltv_max||0);
    payload.predicted_ltv = Math.round(pltv*100)/100;
  }}
}}

/* Pixel event buttons */
function sendView(btn){{
  const bad = readBadToggles();
  const price = 68.99;
  const payload = {{
    content_type:'product',
    content_ids:['SKU-10057'],
    currency: bad.null_currency ? null : 'USD',
    value: bad.null_price ? null : price
  }};
  attachMarginPltv(payload, price);
  const opts = {{ eventID: bad.null_event_id ? null : rid() }};
  fbq('track', 'ViewContent', payload, opts);
  flashIcon(btn, true);
}}
function sendATC(btn){{
  const bad = readBadToggles();
  const price = 68.99;
  const qty = 1;
  const value = qty*price;
  const payload = {{
    content_type:'product',
    content_ids:['SKU-10057'],
    contents:[{{id:'SKU-10057', quantity:qty, item_price: bad.null_price ? null : price}}],
    currency: bad.null_currency ? null : 'USD',
    value: bad.null_price ? null : value
  }};
  attachMarginPltv(payload, value);
  const opts = {{ eventID: bad.null_event_id ? null : rid() }};
  fbq('track', 'AddToCart', payload, opts);
  flashIcon(btn, true);
}}
function sendInitiate(btn){{
  const bad = readBadToggles();
  const price = 68.99;
  const qty = 2;
  const subtotal = qty*price;
  const total = 149.02; // demo number; margin uses merchandise subtotal
  const payload = {{
    contents:[{{id:'SKU-10057', quantity:qty, item_price: bad.null_price ? null : price}}],
    currency: bad.null_currency ? null : 'USD',
    value: bad.null_price ? null : total
  }};
  attachMarginPltv(payload, subtotal);
  const opts = {{ eventID: bad.null_event_id ? null : rid() }};
  fbq('track', 'InitiateCheckout', payload, opts);
  flashIcon(btn, true);
}}
function sendPurchase(btn){{
  const bad = readBadToggles();
  const price = 68.99;
  const qty = 2;
  const subtotal = qty*price;
  const total = 149.02;
  const payload = {{
    contents:[{{id:'SKU-10057', quantity:qty, item_price: bad.null_price ? null : price}}],
    currency: bad.null_currency ? null : 'USD',
    value: bad.null_price ? null : total
  }};
  attachMarginPltv(payload, subtotal);
  const opts = {{ eventID: bad.null_event_id ? null : rid() }};
  fbq('track', 'Purchase', payload, opts);
  flashIcon(btn, true);
}}

/* Auto streamer controls (server → CAPI) */
async function refreshStatus(){{
  try {{
    const j = await fetchJSON('/auto/status');
    document.getElementById('status').textContent = j.running ?
      ('Running at ' + j.rps + ' sessions/sec') : 'Stopped';
    document.getElementById('startBtn').disabled = j.running;
    document.getElementById('stopBtn').disabled = !j.running;
    if (j.running) {{ document.getElementById('rps').value = j.rps; }}
  }} catch (e) {{
    document.getElementById('status').textContent = 'Unknown (server error)';
  }}
}}
async function startAuto(btn){{
  const rps = parseFloat(document.getElementById('rps').value || '0.5');
  const ok  = await fetchOK('/auto/start?rps=' + encodeURIComponent(rps));
  flashIcon(btn, ok);
  setTimeout(refreshStatus, 250);
}}
async function stopAuto(btn){{
  const ok = await fetchOK('/auto/stop');
  flashIcon(btn, ok);
  setTimeout(refreshStatus, 250);
}}

/* Advanced + toggles (read + write) */
async function loadConfig(){{
  try {{
    const cfg = await fetchJSON('/auto/config');
    document.getElementById('rps').value = cfg.rps;
    document.getElementById('p_add_to_cart').value = cfg.p_add_to_cart;
    document.getElementById('p_begin_checkout').value = cfg.p_begin_checkout;
    document.getElementById('p_purchase').value = cfg.p_purchase;
    document.getElementById('price_min').value = cfg.price_min;
    document.getElementById('price_max').value = cfg.price_max;
    document.getElementById('free_shipping_threshold').value = cfg.free_shipping_threshold;
    document.getElementById('shipping_options').value = (cfg.shipping_options || []).join(', ');
    document.getElementById('tax_rate').value = cfg.tax_rate;

    // bad-data toggles
    document.getElementById('null_price').checked = !!cfg.null_price;
    document.getElementById('null_currency').checked = !!cfg.null_currency;
    document.getElementById('null_event_id').checked = !!cfg.null_event_id;

    // NEW: margin + PLTV
    document.getElementById('append_margin').checked = !!cfg.append_margin;
    document.getElementById('margin_rate').value = cfg.margin_rate;
    document.getElementById('append_pltv').checked = !!cfg.append_pltv;
    document.getElementById('pltv_min').value = cfg.pltv_min;
    document.getElementById('pltv_max').value = cfg.pltv_max;
  }} catch (e) {{}}
}}
async function saveConfig(btn){{
  const body = {{
    rps: parseFloat(document.getElementById('rps').value || '0.5'),
    p_add_to_cart: parseFloat(document.getElementById('p_add_to_cart').value || '0.35'),
    p_begin_checkout: parseFloat(document.getElementById('p_begin_checkout').value || '0.7'),
    p_purchase: parseFloat(document.getElementById('p_purchase').value || '0.7'),
    price_min: parseFloat(document.getElementById('price_min').value || '10'),
    price_max: parseFloat(document.getElementById('price_max').value || '120'),
    free_shipping_threshold: parseFloat(document.getElementById('free_shipping_threshold').value || '75'),
    shipping_options: (document.getElementById('shipping_options').value || '')
      .split(',').map(s => parseFloat(s.trim())).filter(n => !isNaN(n)),
    tax_rate: parseFloat(document.getElementById('tax_rate').value || '0.08'),

    // bad-data toggles
    null_price: document.getElementById('null_price').checked,
    null_currency: document.getElementById('null_currency').checked,
    null_event_id: document.getElementById('null_event_id').checked,

    // NEW: margin + PLTV appends
    append_margin: document.getElementById('append_margin').checked,
    margin_rate: parseFloat(document.getElementById('margin_rate').value || '0.35'),
    append_pltv: document.getElementById('append_pltv').checked,
    pltv_min: parseFloat(document.getElementById('pltv_min').value || '120'),
    pltv_max: parseFloat(document.getElementById('pltv_max').value || '600'),
  }};
  const ok = await fetchOK('/auto/config', {{
    method: 'POST',
    headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify(body)
  }});
  flashIcon(btn, ok);
  if (ok) setTimeout(refreshStatus, 250);
}}

window.addEventListener('load', () => {{ refreshStatus(); loadConfig(); }});
</script>
<noscript><img height="1" width="1" style="display:none"
  src="https://www.facebook.com/tr?id={PIXEL_ID}&ev=PageView&noscript=1"/></noscript>
</head>
<body>
  <h1>Demo Store</h1>
  <p class="small">Pixel is active. Buttons send browser events. The auto streamer sends server (CAPI) events from the server itself.</p>

  <div class="grid">
    <!-- Pixel test buttons -->
    <div class="card">
      <h3>ViewContent</h3>
      <button class="btn" onclick="sendView(this)">
        Send ViewContent
        <span class="tick" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M20.3 5.7a1 1 0 0 1 0 1.4l-10 10a1 1 0 0 1-1.4 0l-5-5a1 1 0 1 1 1.4-1.4l4.3 4.3L18.9 5.7a1 1 0 0 1 1.4 0z"/></svg></span>
        <span class="x" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M18.3 5.7a1 1 0 0 1 0 1.4L13.4 12l4.9 4.9a1 1 0 1 1-1.4 1.4L12 13.4l-4.9 4.9a1 1 0 0 1-1.4-1.4L10.6 12 5.7 7.1A1 1 0 1 1 7.1 5.7L12 10.6l4.9-4.9a1 1 0 0 1 1.4 0z"/></svg></span>
      </button>
    </div>

    <div class="card">
      <h3>AddToCart</h3>
      <button class="btn" onclick="sendATC(this)">
        Send AddToCart
        <span class="tick" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M20.3 5.7a1 1 0 0 1 0 1.4l-10 10a1 1 0 0 1-1.4 0l-5-5a1 1 0 1 1 1.4-1.4l4.3 4.3L18.9 5.7a1 1 0 0 1 1.4 0z"/></svg></span>
        <span class="x" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M18.3 5.7a1 1 0 0 1 0 1.4L13.4 12l4.9 4.9a1 1 0 1 1-1.4 1.4L12 13.4l-4.9 4.9a1 1 0 0 1-1.4-1.4L10.6 12 5.7 7.1A1 1 0 1 1 7.1 5.7L12 10.6l4.9-4.9a1 1 0 0 1 1.4 0z"/></svg></span>
      </button>
    </div>

    <div class="card">
      <h3>InitiateCheckout</h3>
      <button class="btn" onclick="sendInitiate(this)">
        Send InitiateCheckout
        <span class="tick" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M20.3 5.7a1 1 0 0 1 0 1.4l-10 10a1 1 0 0 1-1.4 0l-5-5a1 1 0 1 1 1.4-1.4l4.3 4.3L18.9 5.7a1 1 0 0 1 1.4 0z"/></svg></span>
        <span class="x" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M18.3 5.7a1 1 0 0 1 0 1.4L13.4 12l4.9 4.9a1 1 0 1 1-1.4 1.4L12 13.4l-4.9 4.9a1 1 0 0 1-1.4-1.4L10.6 12 5.7 7.1A1 1 0 1 1 7.1 5.7L12 10.6l4.9-4.9a1 1 0 0 1 1.4 0z"/></svg></span>
      </button>
    </div>

    <div class="card">
      <h3>Purchase</h3>
      <button class="btn" onclick="sendPurchase(this)">
        Send Purchase
        <span class="tick" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M20.3 5.7a1 1 0 0 1 0 1.4l-10 10a1 1 0 0 1-1.4 0l-5-5a1 1 0 1 1 1.4-1.4l4.3 4.3L18.9 5.7a1 1 0 0 1 1.4 0z"/></svg></span>
        <span class="x" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M18.3 5.7a1 1 0 0 1 0 1.4L13.4 12l4.9 4.9a1 1 0 1 1-1.4 1.4L12 13.4l-4.9 4.9a1 1 0 0 1-1.4-1.4L10.6 12 5.7 7.1A1 1 0 1 1 7.1 5.7L12 10.6l4.9-4.9a1 1 0 0 1 1.4 0z"/></svg></span>
      </button>
    </div>

    <!-- Auto stream controls -->
    <div class="card">
      <h3>Auto Stream (server → CAPI)</h3>
      <div class="row" style="margin-bottom:8px;">
        <label>Sessions/sec:</label>
        <input id="rps" type="number" step="0.1" min="0.1" value="0.5"/>
      </div>
      <div class="row">
        <button id="startBtn" class="btn" onclick="startAuto(this)">
          Start Auto Stream
          <span class="tick" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M20.3 5.7a1 1 0 0 1 0 1.4l-10 10a1 1 0 0 1-1.4 0l-5-5a1 1 0 1 1 1.4-1.4l4.3 4.3L18.9 5.7a1 1 0 0 1 1.4 0z"/></svg></span>
          <span class="x" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M18.3 5.7a1 1 0 0 1 0 1.4L13.4 12l4.9 4.9a1 1 0 1 1-1.4 1.4L12 13.4l-4.9 4.9a1 1 0 0 1-1.4-1.4L10.6 12 5.7 7.1A1 1 0 1 1 7.1 5.7L12 10.6l4.9-4.9a1 1 0 0 1 1.4 0z"/></svg></span>
        </button>
        <button id="stopBtn" class="btn" onclick="stopAuto(this)">
          Stop
          <span class="tick" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M20.3 5.7a1 1 0 0 1 0 1.4l-10 10a1 1 0 0 1-1.4 0l-5-5a1 1 0 1 1 1.4-1.4l4.3 4.3L18.9 5.7a1 1 0 0 1 1.4 0z"/></svg></span>
          <span class="x" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M18.3 5.7a1 1 0 0 1 0 1.4L13.4 12l4.9 4.9a1 1 0 1 1-1.4 1.4L12 13.4l-4.9 4.9a1 1 0 0 1-1.4-1.4L10.6 12 5.7 7.1A1 1 0 1 1 7.1 5.7L12 10.6l4.9-4.9a1 1 0 0 1 1.4 0z"/></svg></span>
        </button>
      </div>
      <p id="status" class="small">…</p>
    </div>

    <!-- Advanced Controls + Bad Data + Margin/PLTV -->
    <div class="card">
      <h3>Advanced Controls</h3>
      <div class="col">
        <div class="kv"><label for="p_add_to_cart">P(Add to Cart):</label><input id="p_add_to_cart" type="number" step="0.01" min="0" max="1" value="0.35"/></div>
        <div class="kv"><label for="p_begin_checkout">P(Begin Checkout):</label><input id="p_begin_checkout" type="number" step="0.01" min="0" max="1" value="0.7"/></div>
        <div class="kv"><label for="p_purchase">P(Purchase):</label><input id="p_purchase" type="number" step="0.01" min="0" max="1" value="0.7"/></div>
        <div class="kv"><label for="price_min">Price Min ($):</label><input id="price_min" type="number" step="0.01" min="0" value="10"/></div>
        <div class="kv"><label for="price_max">Price Max ($):</label><input id="price_max" type="number" step="0.01" min="0" value="120"/></div>
        <div class="kv"><label for="free_shipping_threshold">Free Shipping ≥ ($):</label><input id="free_shipping_threshold" type="number" step="0.01" min="0" value="75"/></div>
        <div class="kv"><label for="shipping_options">Shipping Options ($, comma):</label><input id="shipping_options" type="text" value="4.99, 6.99, 9.99"/></div>
        <div class="kv"><label for="tax_rate">Tax Rate (0–1):</label><input id="tax_rate" type="number" step="0.001" min="0" max="1" value="0.08"/></div>

        <hr style="width:100%; border:none; border-top:1px solid var(--bd); margin:8px 0;">
        <div class="kv toggle"><input id="null_price" type="checkbox"/><label for="null_price">Send <b>null price</b> (value & item_price)</label></div>
        <div class="kv toggle"><input id="null_currency" type="checkbox"/><label for="null_currency">Send <b>null currency</b></label></div>
        <div class="kv toggle"><input id="null_event_id" type="checkbox"/><label for="null_event_id">Send <b>null event_id</b></label></div>

        <hr style="width:100%; border:none; border-top:1px solid var(--bd); margin:8px 0;">
        <div class="kv toggle"><input id="append_margin" type="checkbox" checked/><label for="append_margin">Append <b>margin</b> to custom_data</label></div>
        <div class="kv"><label for="margin_rate">Margin rate (0–1):</label><input id="margin_rate" type="number" step="0.01" min="0" max="1" value="0.35"/></div>
        <div class="kv toggle"><input id="append_pltv" type="checkbox" checked/><label for="append_pltv">Append <b>predicted_ltv</b> (PLTV)</label></div>
        <div class="kv"><label for="pltv_min">PLTV min ($):</label><input id="pltv_min" type="number" step="0.01" min="0" value="120"/></div>
        <div class="kv"><label for="pltv_max">PLTV max ($):</label><input id="pltv_max" type="number" step="0.01" min="0" value="600"/></div>
      </div>
      <div class="row" style="margin-top:8px;">
        <button class="btn" onclick="saveConfig(this)">
          Save Controls
          <span class="tick" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M20.3 5.7a1 1 0 0 1 0 1.4l-10 10a1 1 0 0 1-1.4 0l-5-5a1 1 0 1 1 1.4-1.4l4.3 4.3L18.9 5.7a1 1 0 0 1 1.4 0z"/></svg></span>
          <span class="x" aria-hidden="true"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M18.3 5.7a1 1 0 0 1 0 1.4L13.4 12l4.9 4.9a1 1 0 1 1-1.4 1.4L12 13.4l-4.9 4.9a1 1 0 0 1-1.4-1.4L10.6 12 5.7 7.1A1 1 0 1 1 7.1 5.7L12 10.6l4.9-4.9a1 1 0 0 1 1.4 0z"/></svg></span>
        </button>
      </div>
      <p class="small">Tip: Save while running to change behavior on the fly. Bad-data + Margin/PLTV apply to Pixel and server CAPI.</p>
    </div>
  </div>
</body></html>
"""

# ---------- routes ----------
@app.route("/")
def home():
    return Response(PAGE_HTML, mimetype="text/html")

@app.route("/ingest", methods=["POST"])
def ingest():
    """Local simulator posts here; we forward to CAPI."""
    try:
        sim_event = request.get_json(force=True)
    except Exception:
        return {"ok": False, "error": "invalid JSON"}, 400

    server_events = map_sim_event_to_capi(sim_event)
    if not server_events:
        return {"ok": True, "skipped": True}

    try:
        meta_resp = capi_post(server_events)
        return {"ok": True, "meta": meta_resp}
    except requests.HTTPError as e:
        return {"ok": False, "error": str(e), "meta": e.response.text}, 400
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

# ----- config endpoints -----
def _clamp(v, lo, hi, default):
    try:
        x = float(v)
    except Exception:
        return default
    return max(lo, min(hi, x))

def _to_bool(v, default=False):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1","true","t","yes","y","on")
    return default

@app.route("/auto/config", methods=["GET", "POST"])
def auto_config():
    if request.method == "GET":
        with CONFIG_LOCK:
            return jsonify(CONFIG)
    # POST: update fields
    try:
        body = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"ok": False, "error":"invalid json"}), 400

    with CONFIG_LOCK:
        # rps
        if "rps" in body:
            CONFIG["rps"] = _clamp(body["rps"], 0.1, 10.0, CONFIG["rps"])
        # probabilities
        for k in ("p_add_to_cart","p_begin_checkout","p_purchase"):
            if k in body:
                CONFIG[k] = _clamp(body[k], 0.0, 1.0, CONFIG[k])
        # price range
        if "price_min" in body:
            CONFIG["price_min"] = _clamp(body["price_min"], 0.0, 1e6, CONFIG["price_min"])
        if "price_max" in body:
            CONFIG["price_max"] = _clamp(body["price_max"], max(0.01, CONFIG["price_min"]), 1e6, CONFIG["price_max"])
        # shipping
        if "free_shipping_threshold" in body:
            CONFIG["free_shipping_threshold"] = _clamp(body["free_shipping_threshold"], 0.0, 1e6, CONFIG["free_shipping_threshold"])
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
            if cleaned:
                CONFIG["shipping_options"] = cleaned
        # tax
        if "tax_rate" in body:
            CONFIG["tax_rate"] = _clamp(body["tax_rate"], 0.0, 1.0, CONFIG["tax_rate"])

        # bad-data toggles
        for b in ("null_price", "null_currency", "null_event_id"):
            if b in body:
                CONFIG[b] = _to_bool(body[b], CONFIG[b])

        # NEW: margin + PLTV settings
        if "append_margin" in body:
            CONFIG["append_margin"] = _to_bool(body["append_margin"], CONFIG["append_margin"])
        if "margin_rate" in body:
            CONFIG["margin_rate"] = _clamp(body["margin_rate"], 0.0, 1.0, CONFIG["margin_rate"])
        if "append_pltv" in body:
            CONFIG["append_pltv"] = _to_bool(body["append_pltv"], CONFIG["append_pltv"])
        if "pltv_min" in body:
            CONFIG["pltv_min"] = _clamp(body["pltv_min"], 0.0, 1e7, CONFIG["pltv_min"])
        if "pltv_max" in body:
            # keep max >= min
            proposed = _clamp(body["pltv_max"], CONFIG["pltv_min"], 1e7, CONFIG["pltv_max"])
            CONFIG["pltv_max"] = max(proposed, CONFIG["pltv_min"])

    return jsonify({"ok": True, "config": CONFIG})

# ---------- auto streamer (background thread) ----------
_auto_thread = None
_stop_evt = threading.Event()

DEVICES   = ["mobile","mobile","desktop","tablet"]
SOURCES   = ["direct","seo","sem","email","social","referral"]
COUNTRIES = ["US","US","US","CA","GB","DE","AU"]
CURRENCIES= ["USD","USD","USD","EUR","GBP","AUD","CAD"]

def _uid(): return str(uuid.uuid4())
def _now_iso(): return datetime.now(tz=timezone.utc).isoformat()
def _pick(seq): return random.choice(seq)

def _make_session():
    return {
        "user_id": f"u_{random.randint(1, 9_999_999)}",
        "session_id": _uid(),
        "device": _pick(DEVICES),
        "country": _pick(COUNTRIES),
        "source": _pick(SOURCES),
        "utm_campaign": _pick(["brand","retargeting","newsletter","new_arrivals",""]),
        "currency": _pick(CURRENCIES),
        "store_id": "store-001",
    }

def _make_product(cfg):
    price = round(random.uniform(cfg["price_min"], max(cfg["price_min"]+0.01, cfg["price_max"])), 2)
    n = random.randint(10000, 10199)
    cat = _pick(["Tops","Bottoms","Shoes","Accessories","Home","Outerwear"])
    return {"product_id": f"SKU-{n}", "name": f"{cat} {n}", "category": cat, "price": price}

def _event_base(session, **extra):
    return {
        "event_id": _uid(),
        "timestamp": _now_iso(),
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

def _send_simulated_session_once(cfg):
    """Generate a short funnel and push directly to CAPI (no HTTP back to /ingest)."""
    s = _make_session()
    product = _make_product(cfg)

    # page_view and product_view
    for evt in [
        _event_base(s, event_type="page_view", page=_pick(["/","/home","/sale"])),
        _event_base(s, event_type="product_view", product=product),
    ]:
        capi_events = map_sim_event_to_capi(evt)
        if capi_events:
            try: capi_post(capi_events)
            except Exception: pass

    # add_to_cart?
    if random.random() < cfg["p_add_to_cart"]:
        qty = _pick([1,1,1,2])
        line = {"product_id": product["product_id"], "qty": qty, "price": product["price"]}
        evt = _event_base(s, event_type="add_to_cart", line_item=line, cart_size=1)
        try:
            capi_events = map_sim_event_to_capi(evt)
            if capi_events: capi_post(capi_events)
        except Exception: pass

        # begin_checkout?
        if random.random() < cfg["p_begin_checkout"]:
            subtotal = qty * product["price"]
            if subtotal >= cfg["free_shipping_threshold"]:
                shipping = 0.0
            else:
                shipping = _pick(cfg["shipping_options"]) if cfg["shipping_options"] else 0.0
            tax = round(cfg["tax_rate"] * subtotal, 2)
            total = round(subtotal+shipping+tax, 2)
            cart = [line]
            evt = _event_base(s, event_type="begin_checkout",
                              cart=cart, subtotal=round(subtotal,2),
                              shipping=shipping, tax=tax, total=total)
            try:
                capi_events = map_sim_event_to_capi(evt)
                if capi_events: capi_post(capi_events)
            except Exception: pass

            # purchase?
            if random.random() < cfg["p_purchase"]:
                evt = _event_base(s, event_type="purchase", items=cart,
                                  subtotal=round(subtotal,2), shipping=shipping,
                                  tax=tax, total=total,
                                  order_id="o_"+_uid()[:12],
                                  payment_method=_pick(["card","paypal","apple_pay","google_pay","klarna"]))
                try:
                    capi_events = map_sim_event_to_capi(evt)
                    if capi_events: capi_post(capi_events)
                except Exception: pass

def _auto_loop():
    # runs until _stop_evt is set
    while not _stop_evt.is_set():
        with CONFIG_LOCK:
            cfg = dict(CONFIG)  # snapshot
        start = time.time()
        try:
            _send_simulated_session_once(cfg)
        except Exception:
            pass
        delay = max(0.05, 1.0 / max(0.1, cfg["rps"]))  # clamp 0.1..10 via save endpoint
        elapsed = time.time() - start
        _stop_evt.wait(max(0.0, delay - elapsed))

@app.route("/auto/start")
def auto_start():
    global _auto_thread
    q = request.args.get("rps", None)
    if q is not None:
        try: qv = float(q)
        except Exception: qv = None
        if qv is not None:
            with CONFIG_LOCK:
                CONFIG["rps"] = max(0.1, min(10.0, qv))
    if _auto_thread is not None and _auto_thread.is_alive():
        with CONFIG_LOCK:
            return jsonify({"ok": True, "running": True, "rps": round(CONFIG["rps"],2)})
    _stop_evt.clear()
    _auto_thread = threading.Thread(target=_auto_loop, daemon=True)
    _auto_thread.start()
    with CONFIG_LOCK:
        return jsonify({"ok": True, "running": True, "rps": round(CONFIG["rps"],2)})

@app.route("/auto/stop")
def auto_stop():
    global _auto_thread
    _stop_evt.set()
    if _auto_thread is not None:
        _auto_thread.join(timeout=1.0)
        _auto_thread = None
    return jsonify({"ok": True, "running": False})

@app.route("/auto/status")
def auto_status():
    running = _auto_thread is not None and _auto_thread.is_alive()
    with CONFIG_LOCK:
        rps = round(CONFIG["rps"], 2)
    return jsonify({"ok": True, "running": running, "rps": rps})

# ---------- entrypoint ----------
if __name__ == "__main__":
    # local run (Render uses gunicorn with PORT env)
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
