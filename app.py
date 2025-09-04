#!/usr/bin/env python3
import os, json, hashlib
from datetime import datetime, timezone
from flask import Flask, request, Response, jsonify
import requests
from dotenv import load_dotenv

# Load secrets from .env
load_dotenv()
PIXEL_ID        = os.getenv("PIXEL_ID", "")
ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN", "")
TEST_EVENT_CODE = os.getenv("TEST_EVENT_CODE", "")   # OK if empty
BASE_URL        = os.getenv("BASE_URL", "http://127.0.0.1:5000")

GRAPH_VER = "v20.0"  # update if Meta releases newer
CAPI_URL  = f"https://graph.facebook.com/{GRAPH_VER}/{PIXEL_ID}/events"

app = Flask(__name__)

# ---------- helpers ----------
def sha256_norm(s: str) -> str:
    norm = (s or "").strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()

def iso_to_unix(ts_iso: str) -> int:
    dt = datetime.fromisoformat(ts_iso.replace("Z","+00:00"))
    return int(dt.replace(tzinfo=timezone.utc).timestamp())

def now_unix() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp())

def build_contents(lines):
    return [{"id": li["product_id"], "quantity": int(li["qty"]), "item_price": float(li["price"])} for li in lines]

def capi_post(server_events: list[dict]) -> dict:
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

# Map simulator → CAPI (kept from earlier)
def map_sim_event_to_capi(e: dict) -> list[dict]:
    et   = e.get("event_type")
    sess = e.get("user", {})
    ctx  = e.get("context", {})
    ts   = iso_to_unix(e["timestamp"])
    currency = ctx.get("currency","USD")

    # infer a URL
    page = e.get("page") or "/"
    if et == "product_view" and "product" in e:
        page = f"/product/{e['product']['product_id']}"
    if et in ("add_to_cart","begin_checkout","purchase"):
        page = "/checkout"
    event_source_url = BASE_URL.rstrip("/") + page

    base = {
        "event_time": ts,
        "event_id": e.get("event_id"),
        "action_source": "website",
        "event_source_url": event_source_url,
        "user_data": {
            "external_id": sha256_norm(sess.get("user_id","")),
            "client_ip_address": request.remote_addr or "127.0.0.1",
            "client_user_agent": request.headers.get("User-Agent","Demo-UA/1.0")
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

    if et == "return_initiated":
        pid = e.get("product_id")
        return [{**base, "event_name":"ReturnInitiated",
                 "custom_data":{
                     "content_type":"product",
                     "content_ids":[pid] if pid else [],
                     "value": 0.0, "currency": currency
                 }}]

    return []

# ---------- routes ----------
@app.route("/")
def home():
    # Single-file HTML with Pixel + test buttons + a new Dedup demo button
    html = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Demo Store</title>
<style>
  body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; margin: 2rem; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; }}
  .card {{ border:1px solid #ddd; border-radius:12px; padding:16px; }}
  button {{ padding:8px 12px; border-radius:8px; border:1px solid #ccc; cursor:pointer; }}
  .small {{ font-size: 12px; color: #555; }}
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

// NEW: Dedup demo – Pixel + CAPI with the SAME event_id
async function sendDedupPurchase(){{
  const id = rid();
  // 1) Pixel (browser) purchase with eventID=id
  fbq('track', 'Purchase', {{
    contents:[{{id:'SKU-10057', quantity:2, item_price:68.99}}],
    currency:'USD', value:149.02
  }}, {{eventID: id}});

  // 2) Tell server to send matching CAPI purchase with the SAME id
  try {{
    const r = await fetch('/dedup-purchase?event_id=' + encodeURIComponent(id));
    const j = await r.json();
    console.log('CAPI response', j);
    alert('Dedup demo: Pixel + CAPI sent with the SAME event_id. Check Test events.');
  }} catch (e) {{
    alert('Server call failed: ' + e);
  }}
}}
</script>
<noscript><img height="1" width="1" style="display:none"
  src="https://www.facebook.com/tr?id={PIXEL_ID}&ev=PageView&noscript=1"/></noscript>
</head>
<body>
  <h1>Demo Store</h1>
  <p class="small">Pixel is active. Buttons send browser events. The Dedup demo sends a Pixel Purchase AND a matching CAPI Purchase using the same <code>event_id</code>.</p>
  <div class="grid">
    <div class="card"><h3>ViewContent</h3><button onclick="sendView()">Send ViewContent</button></div>
    <div class="card"><h3>AddToCart</h3><button onclick="sendATC()">Send AddToCart</button></div>
    <div class="card"><h3>InitiateCheckout</h3><button onclick="sendInitiate()">Send InitiateCheckout</button></div>
    <div class="card"><h3>Purchase</h3><button onclick="sendPurchase()">Send Purchase</button></div>
    <div class="card"><h3>Dedup demo (Purchase)</h3>
      <p class="small">Sends Pixel + CAPI with the SAME <code>event_id</code>.</p>
      <button onclick="sendDedupPurchase()">Send Dedup: Purchase</button>
    </div>
  </div>
</body></html>"""
    return Response(html, mimetype="text/html")

@app.route("/ingest", methods=["POST"])
def ingest():
    """Simulator posts here; we forward to CAPI."""
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

# NEW: route to send a CAPI Purchase with a provided event_id
@app.route("/dedup-purchase")
def dedup_purchase():
    event_id = request.args.get("event_id", "").strip()
    if not event_id:
        return jsonify({"ok": False, "error": "missing event_id"}), 400

    server_event = {
        "event_name": "Purchase",
        "event_time": now_unix(),
        "event_id": event_id,                 # key: SAME as Pixel eventID
        "action_source": "website",
        "event_source_url": BASE_URL.rstrip("/") + "/checkout",
        "user_data": {
            # demo user identifiers; external_id is fine for testing
            "external_id": sha256_norm("demo_user_123"),
            "client_ip_address": request.remote_addr or "127.0.0.1",
            "client_user_agent": request.headers.get("User-Agent","Demo-UA/1.0")
        },
        "custom_data": {
            "contents": [{"id":"SKU-10057","quantity":2,"item_price":68.99}],
            "currency": "USD",
            "value": 149.02
        }
    }

    try:
        meta_resp = capi_post([server_event])
        return jsonify({"ok": True, "meta": meta_resp})
    except requests.HTTPError as e:
        return jsonify({"ok": False, "error": str(e), "meta": e.response.text}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
