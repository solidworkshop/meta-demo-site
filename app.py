#!/usr/bin/env python3
# Flask app with: Pixel demo page, /ingest (CAPI forwarder), and auto Start/Stop streamer
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

# ---------- helpers ----------
def sha256_norm(s: str) -> str:
    norm = (s or "").strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()

def now_unix() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp())

def iso_to_unix(ts_iso: str) -> int:
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

# Map a simulator-style event → 0..1 CAPI events
def map_sim_event_to_capi(e):
    et   = e.get("event_type")
    sess = e.get("user", {})
    ctx  = e.get("context", {})
    currency = ctx.get("currency","USD")
    ts   = iso_to_unix(e["timestamp"])

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

    if et == "page_view":
        return [{**base, "event_name":"PageView"}]

    if et == "product_view":
        p = e["product"]
        return [{**base, "event_name":"ViewContent",
                 "custom_data":{
                     "content_type":"product",
                     "content_ids":[p["product_id"]],
                     "value": float(p["price"]),
                     "currency": currency
                 }}]

    if et == "add_to_cart":
        li = e["line_item"]
        return [{**base, "event_name":"AddToCart",
                 "custom_data":{
                     "content_type":"product",
                     "content_ids":[li["product_id"]],
                     "contents":[{"id": li["product_id"], "quantity": int(li["qty"]), "item_price": float(li["price"])}],
                     "value": float(li["qty"]) * float(li["price"]),
                     "currency": currency
                 }}]

    if et == "begin_checkout":
        cart = e["cart"]
        total = float(e.get("total", 0.0))
        return [{**base, "event_name":"InitiateCheckout",
                 "custom_data":{
                     "contents": build_contents(cart),
                     "value": total,
                     "currency": currency
                 }}]

    if et == "purchase":
        items = e["items"]
        total = float(e.get("total", 0.0))
        return [{**base, "event_name":"Purchase",
                 "custom_data":{
                     "contents": build_contents(items),
                     "value": total,
                     "currency": currency
                 }}]

    # optional: returns as a custom event
    if et == "return_initiated":
        pid = e.get("product_id")
        return [{**base, "event_name":"ReturnInitiated",
                 "custom_data":{
                     "content_type":"product",
                     "content_ids":[pid] if pid else [],
                     "value": 0.0, "currency": currency
                 }}]

    return []

# ---------- HTML page ----------
PAGE_HTML = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Demo Store</title>
<style>
  body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; margin: 2rem; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px; }}
  .card {{ border:1px solid #ddd; border-radius:12px; padding:16px; }}
  button {{ padding:8px 12px; border-radius:8px; border:1px solid #ccc; cursor:pointer; }}
  .row {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; }}
  input[type=number] {{ width: 120px; padding:6px 8px; }}
  .small {{ font-size: 12px; color:#555; }}
</style>
<script>
!function(f,b,e,v,n,t,s){{if(f.fbq)return;n=f.fbq=function(){{n.callMethod?
n.callMethod.apply(n,arguments):n.queue.push(arguments)}};if(!f._fbq)f._fbq=n;
n.push=n; n.loaded=!0; n.version='2.0'; n.queue=[]; t=b.createElement(e);
t.async=!0; t.src=v; s=b.getElementsByTagName(e)[0]; s.parentNode.insertBefore(t,s)
}}(window, document,'script','https://connect.facebook.net/en_US/fbevents.js');
fbq('init', '{PIXEL_ID}');
fbq('track', 'PageView');

function rid(){{ return 'evt_' + Math.random().toString(36).slice(2) + Date.now().toString(36); }}

function sendView(){{
  fbq('track', 'ViewContent', {{
    content_type:'product', content_ids:['SKU-10057'], currency:'USD', value:68.99
  }}, {{eventID: rid()}});
  alert('Pixel: ViewContent sent');
}}
function sendATC(){{
  fbq('track', 'AddToCart', {{
    content_type:'product', content_ids:['SKU-10057'],
    contents:[{{id:'SKU-10057', quantity:1, item_price:68.99}}],
    currency:'USD', value:68.99
  }}, {{eventID: rid()}});
  alert('Pixel: AddToCart sent');
}}
function sendInitiate(){{
  fbq('track', 'InitiateCheckout', {{
    contents:[{{id:'SKU-10057', quantity:2, item_price:68.99}}],
    currency:'USD', value:149.02
  }}, {{eventID: rid()}});
  alert('Pixel: InitiateCheckout sent');
}}
function sendPurchase(){{
  fbq('track', 'Purchase', {{
    contents:[{{id:'SKU-10057', quantity:2, item_price:68.99}}],
    currency:'USD', value:149.02
  }}, {{eventID: rid()}});
  alert('Pixel: Purchase sent');
}}

// --- Auto streamer controls ---
async function refreshStatus(){{
  try {{
    const r = await fetch('/auto/status');
    const j = await r.json();
    document.getElementById('status').textContent = j.running ? 
      ('Running at ' + j.rps + ' sessions/sec') : 'Stopped';
    document.getElementById('startBtn').disabled = j.running;
    document.getElementById('stopBtn').disabled = !j.running;
    if (j.running) {{
      document.getElementById('rps').value = j.rps;
    }}
  }} catch (e) {{
    document.getElementById('status').textContent = 'Unknown (server error)';
  }}
}}
async function startAuto(){{
  const rps = parseFloat(document.getElementById('rps').value || '0.5');
  await fetch('/auto/start?rps=' + encodeURIComponent(rps));
  setTimeout(refreshStatus, 300);
}}
async function stopAuto(){{
  await fetch('/auto/stop');
  setTimeout(refreshStatus, 300);
}}
window.addEventListener('load', refreshStatus);
</script>
<noscript><img height="1" width="1" style="display:none"
  src="https://www.facebook.com/tr?id={PIXEL_ID}&ev=PageView&noscript=1"/></noscript>
</head>
<body>
  <h1>Demo Store</h1>
  <p class="small">Pixel is active. Buttons send browser events. The auto streamer sends server (CAPI) events from the server itself.</p>

  <div class="grid">
    <div class="card"><h3>ViewContent</h3><button onclick="sendView()">Send ViewContent</button></div>
    <div class="card"><h3>AddToCart</h3><button onclick="sendATC()">Send AddToCart</button></div>
    <div class="card"><h3>InitiateCheckout</h3><button onclick="sendInitiate()">Send InitiateCheckout</button></div>
    <div class="card"><h3>Purchase</h3><button onclick="sendPurchase()">Send Purchase</button></div>
    <div class="card">
      <h3>Auto Stream (server → CAPI)</h3>
      <div class="row">
        <label>Sessions/sec:</label>
        <input id="rps" type="number" step="0.1" min="0.1" value="0.5"/>
        <button id="startBtn" onclick="startAuto()">Start Auto Stream</button>
        <button id="stopBtn" onclick="stopAuto()">Stop</button>
      </div>
      <p id="status" class="small">…</p>
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

# ---------- auto streamer (background thread) ----------
_auto_thread = None
_stop_evt = threading.Event()
_current_rps = 0.5

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

def _make_product():
    n = random.randint(10000, 10199)
    price = round(random.uniform(10, 120), 2)
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

def _send_simulated_session_once():
    """Generate a short funnel and push to CAPI (no HTTP back to /ingest)."""
    s = _make_session()
    product = _make_product()

    # page_view
    for evt in [
        _event_base(s, event_type="page_view", page=_pick(["/","/home","/sale"])),
        _event_base(s, event_type="product_view", product=product),
    ]:
        capi_events = map_sim_event_to_capi(evt)
        if capi_events:
            try: capi_post(capi_events)
            except Exception: pass

    # add_to_cart?
    if random.random() < 0.35:
        qty = _pick([1,1,1,2])
        line = {"product_id": product["product_id"], "qty": qty, "price": product["price"]}
        evt = _event_base(s, event_type="add_to_cart", line_item=line, cart_size=1)
        try:
            capi_events = map_sim_event_to_capi(evt)
            if capi_events: capi_post(capi_events)
        except Exception: pass

        # begin_checkout?
        if random.random() < 0.7:
            subtotal = qty * product["price"]
            shipping = 0.0 if subtotal >= 75 else _pick([4.99, 6.99, 9.99])
            tax = round(0.08*subtotal, 2) if s["country"] in ("US","CA") else 0.0
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
            if random.random() < 0.7:
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
    global _current_rps
    # simple loop: every 1/rps seconds, generate one session funnel
    while not _stop_evt.is_set():
        start = time.time()
        try:
            _send_simulated_session_once()
        except Exception:
            pass
        # sleep to honor target rps
        delay = max(0.05, 1.0 / max(0.1, _current_rps))  # clamp values
        elapsed = time.time() - start
        time_to_sleep = max(0.0, delay - elapsed)
        _stop_evt.wait(time_to_sleep)

@app.route("/auto/start")
def auto_start():
    global _auto_thread, _current_rps
    try:
        rps = float(request.args.get("rps", "0.5"))
    except ValueError:
        rps = 0.5
    rps = max(0.1, min(rps, 10.0))  # clamp 0.1..10
    _current_rps = rps

    if _auto_thread is not None and _auto_thread.is_alive():
        return jsonify({"ok": True, "running": True, "rps": _current_rps})

    _stop_evt.clear()
    _auto_thread = threading.Thread(target=_auto_loop, daemon=True)
    _auto_thread.start()
    return jsonify({"ok": True, "running": True, "rps": _current_rps})

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
    return jsonify({"ok": True, "running": running, "rps": round(_current_rps, 2)})

# ---------- entrypoint ----------
if __name__ == "__main__":
    # local run
    port = int(os.getenv("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=True)
