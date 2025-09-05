#!/usr/bin/env python3
import os, json, time, uuid, random, hashlib
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from flask import Flask, request, jsonify, render_template
import requests

app = Flask(__name__)

# --------------------------- ENV / CONST ---------------------------
PIXEL_ID        = os.getenv("PIXEL_ID", "")
ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN", "")
TEST_EVENT_CODE = os.getenv("TEST_EVENT_CODE", "")  # set this to see events under "Test Events"
GRAPH_VER       = os.getenv("GRAPH_VER", "v20.0")
BASE_URL        = os.getenv("BASE_URL", "http://127.0.0.1:5000")
CATALOG_SIZE    = int(os.getenv("CATALOG_SIZE", "20"))
LITE_MODE_ENV   = os.getenv("LITE_MODE", "0") == "1"
DRY_RUN_ENV     = os.getenv("DRY_RUN", "0") == "1"  # force dry-run even if creds exist

# Safe CAPI URL construction
CAPI_URL: Optional[str] = None
if PIXEL_ID and GRAPH_VER:
    CAPI_URL = f"https://graph.facebook.com/{GRAPH_VER}/{PIXEL_ID}/events"

# Feature flags
ENABLE_PIXEL_AUTO_DEFAULT = os.getenv("ENABLE_PIXEL_AUTO", "0") == "1"
ENABLE_CAPI_AUTO_DEFAULT  = os.getenv("ENABLE_CAPI_AUTO", "0") == "1"
ENABLE_CATALOG_UI_DEFAULT = os.getenv("ENABLE_CATALOG_UI", "1") == "1"

# --------------------------- Helpers ---------------------------
def is_lite_mode() -> bool:
    q = request.args.get("lite")
    if q is not None:
        return q == "1"
    return LITE_MODE_ENV

def hash_email(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def pick_currency(p: str) -> str:
    if p == "AUTO":
        return "USD"
    return p

# Catalog
random.seed(1234)
CATALOG: List[Dict[str, Any]] = []
for i in range(1, CATALOG_SIZE + 1):
    price = round(random.uniform(5, 200), 2)
    sku = f"SKU{i:04d}"
    CATALOG.append({
        "sku": sku,
        "name": f"Demo Product {i}",
        "price": price,
        "url": f"/product/{sku}",
        "image": None,
    })

def compute_margin(price: Optional[float], min_cost_pct: float, max_cost_pct: float) -> Optional[float]:
    if price is None:
        return None
    min_cost = price * (min_cost_pct / 100.0)
    max_cost = price * (max_cost_pct / 100.0)
    cost = random.uniform(min_cost, max_cost)
    return round(price - cost, 2)

# --------------------------- Routes ---------------------------
@app.route("/")
def index():
    lite = is_lite_mode()
    return render_template(
        "index.html",
        lite=lite,
        enable_pixel_auto=ENABLE_PIXEL_AUTO_DEFAULT and not lite,
        enable_capi_auto=ENABLE_CAPI_AUTO_DEFAULT and not lite,
        enable_catalog_ui=ENABLE_CATALOG_UI_DEFAULT,
        pixel_id_set=bool(PIXEL_ID),
        capi_url_set=bool(CAPI_URL),
        test_event_code_set=bool(TEST_EVENT_CODE),
        base_url=BASE_URL,
    )

@app.route("/catalog")
def catalog():
    return render_template("catalog.html", items=CATALOG)

@app.route("/product/<sku>")
def product(sku: str):
    item = next((i for i in CATALOG if i["sku"] == sku), None)
    if not item:
        return "Not found", 404
    return render_template("product.html", item=item)

@app.route("/api/send", methods=["POST"])
def api_send():
    try:
        payload = request.get_json(force=True) or {}
        delay_ms = int(payload.get("delay_ms", 0))
        if delay_ms > 0:
            time.sleep(min(delay_ms, 5000) / 1000.0)

        price = payload.get("price", None)
        currency_raw = payload.get("currency", "AUTO")
        currency = pick_currency(currency_raw)
        if payload.get("allow_null_price", False):
            price = None if price is None else price
        if payload.get("allow_null_currency", False):
            currency = None if currency_raw == "NULL" else currency

        event_id = str(uuid.uuid4())
        if payload.get("allow_null_event_id", False) and random.random() < 0.5:
            event_id = None

        margin = compute_margin(price, float(payload.get("margin_cost_min_pct", 20.0)),
                                      float(payload.get("margin_cost_max_pct", 70.0)))
        pltv = payload.get("pltv", None)

        event_name = payload.get("event_name", "Purchase")
        sku = payload.get("sku")
        product = next((i for i in CATALOG if i["sku"] == sku), None) if sku else None

        common = {
            "event_name": event_name,
            "event_time": int(time.time()),
            "event_id": event_id,
            "custom_data": {
                "value": price,
                "currency": currency,
                "content_ids": [sku] if sku else [],
                "content_type": "product",
                "contents": [{"id": sku, "quantity": 1, "item_price": price}] if sku and price is not None else [],
                "margin": margin,
                "pltv": pltv,
            },
            "action_source": "website",
            "event_source_url": f"{BASE_URL}/product/{sku}" if sku else BASE_URL,
            "user_data": {
                "em": ["d41d8cd98f00b204e9800998ecf8427e"],
                "client_user_agent": request.headers.get("User-Agent", ""),
                "client_ip_address": request.remote_addr,
            }
        }

        # Simulated Pixel result
        pixel_result = None
        if payload["channel"] in ("pixel", "both"):
            pixel_result = {"status": "dry-run", "desc": "Pixel call simulated for UI feedback", "data": common}

        # Conversions API real POST if possible
        capi_result = None
        want_capi = payload["channel"] in ("capi", "both")
        if want_capi:
            if CAPI_URL and ACCESS_TOKEN and not DRY_RUN_ENV:
                params = {"access_token": ACCESS_TOKEN}
                if TEST_EVENT_CODE:
                    params["test_event_code"] = TEST_EVENT_CODE
                body = {"data": [common], "partner_agent": "ecomm-sim/1.0"}
                try:
                    r = requests.post(CAPI_URL, params=params, json=body, timeout=10)
                    try:
                        j = r.json()
                    except Exception:
                        j = {"text": r.text}
                    capi_result = {
                        "status": "ok" if r.status_code in (200, 201) else "http_error",
                        "http_status": r.status_code,
                        "response": j,
                    }
                except Exception as e:
                    capi_result = {"status": "exception", "error": str(e)}
            else:
                capi_result = {"status": "dry-run", "desc": "Missing creds or DRY_RUN enabled; not posted", "data": common}

        return jsonify({
            "ok": True,
            "pixel": pixel_result,
            "capi": capi_result,
            "debug": {"capi_url_set": bool(CAPI_URL), "pixel_id_set": bool(PIXEL_ID), "dry_run": DRY_RUN_ENV}
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
