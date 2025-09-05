#!/usr/bin/env python3
"""
E‑commerce Simulator (Light Mode) — Full Project
- Three columns: Manual Sender (Pixel/CAPI/Both), Pixel Auto (browser), CAPI Auto (server)
- Per-column Advanced & Discrepancy controls (independent where it matters)
- Product catalog with unique URLs (/catalog, /product/<sku>), simple in‑memory "DB"
- Appended metrics: margin & PLTV (manual + auto), optional delay window, match-rate degradation
- Bad data toggles: null price/currency/event_id
- Master switches: enable/disable Pixel and CAPI
- Button success/fail = border flash (green on success, red persists on error)
"""
import os, json, time, uuid, random, hashlib, threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import requests
from flask import Flask, request, jsonify, render_template, redirect, url_for

# --------------------------- ENV / CONST ---------------------------
PIXEL_ID        = os.getenv("PIXEL_ID", "")
ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN", "")
TEST_EVENT_CODE = os.getenv("TEST_EVENT_CODE", "")
GRAPH_VER       = os.getenv("GRAPH_VER", "v21.0")
BASE_URL        = os.getenv("BASE_URL", "http://127.0.0.1:5000")
DEFAULT_CATALOG_SIZE = int(os.getenv("DEFAULT_CATALOG_SIZE", "24"))

# Build CAPI URL if creds exist
def capi_url() -> Optional[str]:
    if PIXEL_ID and ACCESS_TOKEN:
        return f"https://graph.facebook.com/{GRAPH_VER}/{PIXEL_ID}/events"
    return None

# Flask
app = Flask(__name__)

# --------------------------- In‑Memory State ---------------------------
class CatalogItem(Dict[str, Any]):
    pass

STATE = {
    "master": {
        "pixel_enabled": True,
        "capi_enabled": True,
    },
    "catalog": {},  # sku -> CatalogItem
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
        "match_rate_degrade_pct": 0,  # 0..100
        "pltv": 0.0,
    }
}

CATALOG_LOCK = threading.Lock()

# --------------------------- Helpers ---------------------------
def ensure_catalog(size: int) -> None:
    """Ensure the in‑memory catalog has exactly `size` items."""
    with CATALOG_LOCK:
        current = len(STATE["catalog"])
        if current == size:
            return
        # Reset to deterministic catalog for predictability
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
    """Return fake user data, optionally degrading match quality."""
    # Simple model: with degrade_pct chance, omit external_id / email
    include_ids = random.randint(1,100) > degrade_pct
    ud = {}
    if include_ids:
        ud["external_id"] = hashlib.sha256(f"user-{uuid.uuid4()}".encode()).hexdigest()
        ud["em"] = hashlib.sha256(f"user{random.randint(1000,9999)}@example.com".encode()).hexdigest()
    return ud

def compute_margin(price: Optional[float], cost_min: int, cost_max: int) -> Optional[float]:
    if price is None: return None
    cmin = max(0, min(cost_min, 99))
    cmax = max(cmin, min(cost_max, 99))
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
    """We *simulate* browser pixel send on server for demo visibility."""
    # In real browser, pixel fires via JS. Here we just return a stub response.
    return {"ok": True, "kind": "pixel", "echo": payload}

def send_capi(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Send to Meta CAPI if configured; otherwise simulate success."""
    url = capi_url()
    if not STATE["master"]["capi_enabled"]:
        return {"ok": False, "error": "CAPI disabled by master switch."}
    if not url:
        return {"ok": True, "kind": "capi-simulated", "echo": payload, "note": "No PIXEL_ID/ACCESS_TOKEN set; simulating."}
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

def build_event(event_name: str, item: CatalogItem, controls: Dict[str, Any]) -> Dict[str, Any]:
    """Build a Meta event payload with bad-data toggles, margin, PLTV, delay, and match-rate degradation."""
    price = item.get("price")
    currency_sel = controls.get("currency", "Auto")
    bad = controls.get("bad_nulls", {"price": False, "currency": False, "event_id": False})
    cost_min = int(controls.get("cost_pct_min", 20))
    cost_max = int(controls.get("cost_pct_max", 60))
    degrade = int(controls.get("match_rate_degrade_pct", 0))
    delay_ms = int(controls.get("delay_ms", 0))
    pltv = float(controls.get("pltv", 0.0))

    if delay_ms > 0:
        time.sleep(min(delay_ms, 3000)/1000.0)  # cap at 3s

    # Compute margin as value
    margin = compute_margin(price, cost_min, cost_max)

    currency = pick_currency(currency_sel)
    payload_event_id = None if bad.get("event_id") else str(uuid.uuid4())

    event = {
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
                "value": maybe(margin, bad.get("price")),  # sending margin as value
                "currency": maybe(currency, bad.get("currency")),
                "price": maybe(price, bad.get("price")),
                "pltv": pltv,
                "margin": margin,
            }
        }]
    }
    return event

# --------------------------- Routes ---------------------------
@app.route("/")
def index():
    ensure_catalog(DEFAULT_CATALOG_SIZE)
    return render_template("index.html",
        pixel_enabled=STATE["master"]["pixel_enabled"],
        capi_enabled=STATE["master"]["capi_enabled"],
        default_catalog_size=DEFAULT_CATALOG_SIZE,
    )

@app.route("/catalog")
def catalog():
    ensure_catalog(DEFAULT_CATALOG_SIZE)
    with CATALOG_LOCK:
        items = list(STATE["catalog"].values())
    return render_template("catalog.html", items=items)

@app.route("/product/<sku>")
def product(sku):
    ensure_catalog(DEFAULT_CATALOG_SIZE)
    with CATALOG_LOCK:
        item = STATE["catalog"].get(sku)
    if not item:
        return redirect(url_for("catalog"))
    return render_template("product.html", item=item)

# ---- API: master switches & catalog size ----
@app.post("/api/master")
def api_master():
    data = request.json or {}
    STATE["master"]["pixel_enabled"] = bool(data.get("pixel_enabled", STATE["master"]["pixel_enabled"]))
    STATE["master"]["capi_enabled"] = bool(data.get("capi_enabled", STATE["master"]["capi_enabled"]))
    return jsonify({"ok": True, "master": STATE["master"]})

@app.post("/api/catalog/size")
def api_catalog_size():
    data = request.json or {}
    size = int(data.get("size", DEFAULT_CATALOG_SIZE))
    size = max(1, min(size, 500))
    global DEFAULT_CATALOG_SIZE
    DEFAULT_CATALOG_SIZE = size
    ensure_catalog(size)
    return jsonify({"ok": True, "size": size})

# ---- API: manual send ----
@app.post("/api/manual/send")
def api_manual_send():
    """
    Body:
    {
      "channel": "pixel"|"capi"|"both",
      "event": "Purchase"|"AddToCart"|...,
      "sku": "SKU0001",
      "controls": {
         "bad_nulls": {"price":bool,"currency":bool,"event_id":bool},
         "cost_pct_min": 20, "cost_pct_max": 60,
         "currency": "Auto|Null|USD|EUR|...",
         "delay_ms": 0,
         "match_rate_degrade_pct": 0,
         "pltv": number
      }
    }
    """
    data = request.json or {}
    channel = data.get("channel", "both")
    event_name = data.get("event", "Purchase")
    controls = data.get("controls", {})
    sku = data.get("sku")

    ensure_catalog(DEFAULT_CATALOG_SIZE)
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

# ---- API: server auto (CAPI) ----
def _server_auto_loop():
    while not STATE["server_auto"]["stop_flag"]:
        # Compose controls from current server_auto settings
        controls = {
            "bad_nulls": STATE["server_auto"]["bad_nulls"],
            "cost_pct_min": STATE["server_auto"]["cost_pct_min"],
            "cost_pct_max": STATE["server_auto"]["cost_pct_max"],
            "currency": STATE["server_auto"]["currency"],
            "delay_ms": STATE["server_auto"]["delay_ms"],
            "match_rate_degrade_pct": STATE["server_auto"]["match_rate_degrade_pct"],
            "pltv": STATE["server_auto"]["pltv"],
        }
        ensure_catalog(DEFAULT_CATALOG_SIZE)
        with CATALOG_LOCK:
            item = random.choice(list(STATE["catalog"].values()))
        payload = build_event("Purchase", item, controls)
        send_capi(payload)
        time.sleep(max(0.2, STATE["server_auto"]["interval_ms"]/1000.0))
    STATE["server_auto"]["running"] = False

@app.post("/api/server_auto/start")
def api_server_auto_start():
    data = request.json or {}
    STATE["server_auto"]["interval_ms"] = int(data.get("interval_ms", STATE["server_auto"]["interval_ms"]))
    for k in ("bad_nulls","cost_pct_min","cost_pct_max","currency","delay_ms","match_rate_degrade_pct","pltv"):
        if k in data:
            STATE["server_auto"][k] = data[k]
    if STATE["server_auto"]["running"]:
        return jsonify({"ok": True, "running": True})
    STATE["server_auto"]["stop_flag"] = False
    th = threading.Thread(target=_server_auto_loop, daemon=True)
    STATE["server_auto"]["thread"] = th
    STATE["server_auto"]["running"] = True
    th.start()
    return jsonify({"ok": True, "running": True})

@app.post("/api/server_auto/stop")
def api_server_auto_stop():
    STATE["server_auto"]["stop_flag"] = True
    return jsonify({"ok": True, "running": False})

# ---- Health ----
@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "time": now_iso()})

if __name__ == "__main__":
    ensure_catalog(DEFAULT_CATALOG_SIZE)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
