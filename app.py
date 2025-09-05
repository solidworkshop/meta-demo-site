#!/usr/bin/env python3
"""
E-commerce Simulator — Toggles V2 (UX polish)
- .env support
- Option A fix (STATE["default_catalog_size"])
- Pixel Auto + CAPI Auto:
  - Toggle signifiers (persisted)
  - Animated border glow while running
  - Traffic indicator dot
  - Inline status text
  - Event counters persisted server-side
"""
import os, json, time, uuid, random, hashlib, threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import requests
from flask import Flask, request, jsonify, render_template, redirect, url_for
from dotenv import load_dotenv

load_dotenv()

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
        "count": 0
    },
    "pixel_auto": {
        "running": False,
        "interval_ms": 2000,
        "bad_nulls": {"price": False, "currency": False, "event_id": False},
        "cost_pct_min": 20,
        "cost_pct_max": 60,
        "currency": "Auto",
        "delay_ms": 0,
        "match_rate_degrade_pct": 0,
        "pltv": 0.0,
        "count": 0
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
    cmin, cmax = sorted([max(0, min(cost_min, 99)), max(0, min(cost_max, 99))])
    cost_pct = random.uniform(cmin, cmax) / 100.0
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
        return {"ok": True, "kind": "capi-simulated", "echo": payload, "note": "PIXEL_ID/ACCESS_TOKEN not set"}
    try:
        resp = requests.post(
            url,
            json=payload,
            params={"access_token": ACCESS_TOKEN, **({"test_event_code": TEST_EVENT_CODE} if TEST_EVENT_CODE else {})},
            timeout=10,
        )
        ok = resp.status_code == 200
        try:
            body = resp.json() if resp.content else {}
        except Exception:
            body = {"raw_text": resp.text}
        return {"ok": ok, "status": resp.status_code, "body": body}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def build_event(event_name: str, item: Dict[str, Any], controls: Dict[str, Any]) -> Dict[str, Any]:
    price = item.get("price")
    currency_sel = controls.get("currency", "Auto")
    bad = controls.get("bad_nulls", {"price": False, "currency": False, "event_id": False})
    cost_min = int(controls.get("cost_pct_min", 20))
    cost_max = int(controls.get("cost_pct_max", 60))
    degrade = int(controls.get("match_rate_degrade_pct", 0))
    delay_ms = int(controls.get("delay_ms", 0))
    pltv = float(controls.get("pltv", 0.0))

    if delay_ms > 0:
        time.sleep(min(delay_ms, 3000)/1000.0)

    margin = compute_margin(price, cost_min, cost_max)
    currency = pick_currency(currency_sel)
    payload_event_id = None if bad.get("event_id") else str(uuid.uuid4())

    return {
        "data": [{
            "event_name": event_name,
            "event_time": int(time.time()),
            "event_id": payload_event_id,
            "action_source": "website",
            "event_source_url": item.get("url"),
            "user_data": rand_user_data(degrade),
            "custom_data": {
                "content_ids": [item.get("sku")],
                "content_type": "product",
                "content_name": item.get("name"),
                "value": maybe(margin, bad.get("price")),
                "currency": maybe(currency, bad.get("currency")),
                "price": maybe(price, bad.get("price")),
                "pltv": pltv,
                "margin": margin,
            }
        }]
    }

# ---------------- ROUTES ----------------
@app.get("/")
def index():
    ensure_catalog(STATE["default_catalog_size"])
    return render_template("index.html",
        pixel_enabled=STATE["master"]["pixel_enabled"],
        capi_enabled=STATE["master"]["capi_enabled"],
        default_catalog_size=STATE["default_catalog_size"],
        pixel_auto=STATE["pixel_auto"],
        server_auto=STATE["server_auto"],
    )

@app.get("/api/status")
def api_status():
    # return state for both autos (excluding thread object)
    sa = {k:v for k,v in STATE["server_auto"].items() if k != "thread"}
    return jsonify({"ok": True, "pixel_auto": STATE["pixel_auto"], "server_auto": sa})

@app.post("/api/pixel_auto/set")
def api_pixel_auto_set():
    data = request.json or {}
    # update persistent settings
    for k in ("running","interval_ms","bad_nulls","cost_pct_min","cost_pct_max","currency","delay_ms","match_rate_degrade_pct","pltv","count"):
        if k in data:
            STATE["pixel_auto"][k] = data[k]
    return jsonify({"ok": True, "pixel_auto": STATE["pixel_auto"]})

@app.post("/api/pixel_auto/increment")
def api_pixel_auto_increment():
    # client calls this after a Pixel send to persist count
    STATE["pixel_auto"]["count"] += 1
    return jsonify({"ok": True, "count": STATE["pixel_auto"]["count"]})

@app.post("/api/pixel_auto/reset_count")
def api_pixel_auto_reset_count():
    STATE["pixel_auto"]["count"] = 0
    return jsonify({"ok": True, "count": 0})

@app.post("/api/server_auto/start")
def api_server_auto_start():
    data = request.json or {}
    STATE["server_auto"]["interval_ms"] = int(data.get("interval_ms", STATE["server_auto"]["interval_ms"]))
    for k in ("bad_nulls","cost_pct_min","cost_pct_max","currency","delay_ms","match_rate_degrade_pct","pltv"):
        if k in data:
            STATE["server_auto"][k] = data[k]
    if STATE["server_auto"]["running"]:
        return jsonify({"ok": True, "running": True, "count": STATE["server_auto"]["count"]})
    STATE["server_auto"]["stop_flag"] = False
    th = threading.Thread(target=_server_auto_loop, daemon=True)
    STATE["server_auto"]["thread"] = th
    STATE["server_auto"]["running"] = True
    th.start()
    return jsonify({"ok": True, "running": True, "count": STATE["server_auto"]["count"]})

@app.post("/api/server_auto/stop")
def api_server_auto_stop():
    STATE["server_auto"]["stop_flag"] = True
    return jsonify({"ok": True, "running": False})

def _server_auto_loop():
    while not STATE["server_auto"]["stop_flag"]:
        controls = {
            "bad_nulls": STATE["server_auto"]["bad_nulls"],
            "cost_pct_min": STATE["server_auto"]["cost_pct_min"],
            "cost_pct_max": STATE["server_auto"]["cost_pct_max"],
            "currency": STATE["server_auto"]["currency"],
            "delay_ms": STATE["server_auto"]["delay_ms"],
            "match_rate_degrade_pct": STATE["server_auto"]["match_rate_degrade_pct"],
            "pltv": STATE["server_auto"]["pltv"],
        }
        ensure_catalog(STATE["default_catalog_size"])
        with CATALOG_LOCK:
            item = random.choice(list(STATE["catalog"].values()))
        payload = build_event("Purchase", item, controls)
        send_capi(payload)
        STATE["server_auto"]["count"] += 1
        time.sleep(max(0.2, STATE["server_auto"]["interval_ms"]/1000.0))
    STATE["server_auto"]["running"] = False

@app.post("/api/server_auto/reset_count")
def api_server_auto_reset_count():
    STATE["server_auto"]["count"] = 0
    return jsonify({"ok": True, "count": 0})

@app.post("/api/master")
def api_master():
    data = request.json or {}
    STATE["master"]["pixel_enabled"] = bool(data.get("pixel_enabled", STATE["master"]["pixel_enabled"]))
    STATE["master"]["capi_enabled"] = bool(data.get("capi_enabled", STATE["master"]["capi_enabled"]))
    return jsonify({"ok": True, "master": STATE["master"]})

@app.post("/api/catalog/size")
def api_catalog_size():
    data = request.json or {}
    size = int(data.get("size", STATE["default_catalog_size"]))
    size = max(1, min(size, 500))
    STATE["default_catalog_size"] = size
    ensure_catalog(size)
    return jsonify({"ok": True, "size": size})

@app.post("/api/manual/send")
def api_manual_send():
    data = request.json or {}
    channel = data.get("channel", "both")
    event_name = data.get("event", "Purchase")
    controls = data.get("controls", {})
    sku = data.get("sku")

    ensure_catalog(STATE["default_catalog_size"])
    with CATALOG_LOCK:
        item = STATE["catalog"].get(sku) if sku else random.choice(list(STATE["catalog"].values()))

    payload = build_event(event_name, item, controls)

    results = []
    if channel in ("pixel", "both"):
        if not STATE["master"]["pixel_enabled"]:
            results.append({"ok": False, "error": "Pixel disabled by master switch."})
        else:
            results.append(send_pixel_stub(payload))
    if channel in ("capi", "both"):
        results.append(send_capi(payload))

    ok = all(r.get("ok") for r in results)
    return jsonify({"ok": ok, "results": results, "sent_payload": payload})

@app.get("/catalog")
def catalog():
    ensure_catalog(STATE["default_catalog_size"])
    with CATALOG_LOCK:
        items = list(STATE["catalog"].values())
    return render_template("catalog.html", items=items)

@app.get("/product/<sku>")
def product(sku):
    ensure_catalog(STATE["default_catalog_size"])
    with CATALOG_LOCK:
        item = STATE["catalog"].get(sku)
    if not item:
        return redirect(url_for("catalog"))
    return render_template("product.html", item=item)

@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "time": now_iso()})

if __name__ == "__main__":
    ensure_catalog(STATE["default_catalog_size"])
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
