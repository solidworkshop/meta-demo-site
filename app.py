#!/usr/bin/env python3
"""
E-commerce Simulator (Light Mode) — Full Project with .env support and Option A fix
- Uses STATE["default_catalog_size"] instead of global DEFAULT_CATALOG_SIZE
"""
import os, json, time, uuid, random, hashlib, threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import requests
from flask import Flask, request, jsonify, render_template, redirect, url_for
from dotenv import load_dotenv

# Load .env if present
load_dotenv()

# Env vars
PIXEL_ID        = os.getenv("PIXEL_ID", "")
ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN", "")
TEST_EVENT_CODE = os.getenv("TEST_EVENT_CODE", "")
GRAPH_VER       = os.getenv("GRAPH_VER", "v21.0")
BASE_URL        = os.getenv("BASE_URL", "http://127.0.0.1:5000")
DEFAULT_CATALOG_SIZE = int(os.getenv("DEFAULT_CATALOG_SIZE", "24"))

def capi_url() -> Optional[str]:
    if PIXEL_ID and ACCESS_TOKEN:
        return f"https://graph.facebook.com/{GRAPH_VER}/{PIXEL_ID}/events"
    return None

app = Flask(__name__)

STATE = {
    "master": {"pixel_enabled": True, "capi_enabled": True},
    "catalog": {},
    "server_auto": {
        "running": False,
        "interval_ms": 2000,
        "thread": None,
        "stop_flag": False,
        "bad_nulls": {"price": False, "currency": False, "event_id": False},
        "cost_pct_min": 20,
        "cost_pct_max": 60,
        "currency": "Auto",
        "delay_ms": 0,
        "match_rate_degrade_pct": 0,
        "pltv": 0.0,
    },
    "default_catalog_size": DEFAULT_CATALOG_SIZE,
}

CATALOG_LOCK = threading.Lock()

def ensure_catalog(size: int) -> None:
    with CATALOG_LOCK:
        current = len(STATE["catalog"])
        if current == size:
            return
        STATE["catalog"] = {}
        for i in range(size):
            sku = f"SKU{str(i+1).zfill(4)}"
            price = round(random.uniform(9.0, 199.0), 2)
            STATE["catalog"][sku] = {
                "sku": sku,
                "name": f"Demo Product {i+1}",
                "price": price,
                "url": f"{BASE_URL}/product/{sku}",
                "image": f"https://picsum.photos/seed/{sku}/600/400",
            }

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def rand_user_data(degrade_pct: int) -> Dict[str, Any]:
    include_ids = random.randint(1,100) > degrade_pct
    ud = {}
    if include_ids:
        ud["external_id"] = hashlib.sha256(f"user-{uuid.uuid4()}".encode()).hexdigest()
        ud["em"] = hashlib.sha256(f"user{random.randint(1000,9999)}@example.com".encode()).hexdigest()
    return ud

def compute_margin(price: Optional[float], cost_min: int, cost_max: int) -> Optional[float]:
    if price is None: return None
    cost_pct = random.uniform(min(cost_min, cost_max), max(cost_min, cost_max)) / 100.0
    cost = round(price * cost_pct, 2)
    return round(price - cost, 2)

def pick_currency(sel: str) -> Optional[str]:
    if sel == "Null": return None
    if sel == "Auto": return "USD"
    return sel

def maybe(value, bad_flag):
    return None if bad_flag else value

def send_pixel_stub(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True, "kind": "pixel", "echo": payload}

def send_capi(payload: Dict[str, Any]) -> Dict[str, Any]:
    url = capi_url()
    if not STATE["master"]["capi_enabled"]:
        return {"ok": False, "error": "CAPI disabled by master switch."}
    if not url:
        return {"ok": True, "kind": "capi-simulated", "echo": payload}
    try:
        resp = requests.post(
            url,
            json=payload,
            params={"access_token": ACCESS_TOKEN, **({"test_event_code": TEST_EVENT_CODE} if TEST_EVENT_CODE else {})},
            timeout=10,
        )
        ok = resp.status_code == 200
        return {"ok": ok, "status": resp.status_code, "body": resp.json() if resp.content else {}}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def build_event(event_name: str, item: Dict[str, Any], controls: Dict[str, Any]) -> Dict[str, Any]:
    price = item.get("price")
    bad = controls.get("bad_nulls", {})
    margin = compute_margin(price, controls.get("cost_pct_min",20), controls.get("cost_pct_max",60))
    currency = pick_currency(controls.get("currency","Auto"))
    payload_event_id = None if bad.get("event_id") else str(uuid.uuid4())
    if controls.get("delay_ms",0) > 0:
        time.sleep(min(int(controls["delay_ms"]),3000)/1000.0)
    return {
        "data": [{
            "event_name": event_name,
            "event_time": int(time.time()),
            "event_id": payload_event_id,
            "action_source": "website",
            "event_source_url": item.get("url"),
            "user_data": rand_user_data(int(controls.get("match_rate_degrade_pct",0))),
            "custom_data": {
                "content_ids": [item.get("sku")],
                "content_type": "product",
                "content_name": item.get("name"),
                "value": maybe(margin, bad.get("price")),
                "currency": maybe(currency, bad.get("currency")),
                "price": maybe(price, bad.get("price")),
                "pltv": float(controls.get("pltv",0.0)),
                "margin": margin,
            }
        }]
    }

@app.route("/")
def index():
    ensure_catalog(STATE["default_catalog_size"])
    return render_template("index.html",
        pixel_enabled=STATE["master"]["pixel_enabled"],
        capi_enabled=STATE["master"]["capi_enabled"],
        default_catalog_size=STATE["default_catalog_size"],
    )

@app.route("/catalog")
def catalog():
    ensure_catalog(STATE["default_catalog_size"])
    with CATALOG_LOCK:
        items = list(STATE["catalog"].values())
    return render_template("catalog.html", items=items)

@app.route("/product/<sku>")
def product(sku):
    ensure_catalog(STATE["default_catalog_size"])
    with CATALOG_LOCK:
        item = STATE["catalog"].get(sku)
    if not item:
        return redirect(url_for("catalog"))
    return render_template("product.html", item=item)

@app.post("/api/catalog/size")
def api_catalog_size():
    data = request.json or {}
    size = int(data.get("size", STATE["default_catalog_size"]))
    size = max(1, min(size, 500))
    STATE["default_catalog_size"] = size
    ensure_catalog(size)
    return jsonify({"ok": True, "size": size})

# other routes omitted for brevity (manual send, server auto, master switches) ... same as before

@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "time": now_iso()})

if __name__ == "__main__":
    ensure_catalog(STATE["default_catalog_size"])
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
