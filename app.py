#!/usr/bin/env python3
"""
E-commerce Simulator — Full Final v4
- Complete simulator (Meta Pixel, CAPI, Manual, Catalog)
- Green banner if no .env file (assume Render env vars)
- Orange banner if CAPI creds missing
"""
import os, json, time, uuid, random, hashlib, threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional
import requests
from flask import Flask, request, jsonify, render_template, redirect, url_for
from dotenv import load_dotenv

ENV_FILE_EXISTS = os.path.exists(".env")
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

STATE: Dict[str, Any] = {
    "catalog": {},
    "pixel_auto": {"running": False,"interval_ms": 2000,"bad_nulls": {"price": False,"currency": False,"event_id": False},
        "cost_pct_min": 20,"cost_pct_max": 60,"currency": "Auto","delay_ms": 0,"match_rate_degrade_pct": 0,"pltv": 0.0,"count": 0},
    "server_auto": {"running": False,"interval_ms": 2000,"thread": None,"stop_flag": False,
        "bad_nulls": {"price": False,"currency": False,"event_id": False},
        "cost_pct_min": 20,"cost_pct_max": 60,"currency": "Auto","delay_ms": 0,"match_rate_degrade_pct": 0,"pltv": 0.0,"count": 0},
    "default_catalog_size": DEFAULT_CATALOG_SIZE,
    "last_capi_error": None,
}

CATALOG_LOCK = threading.Lock()

def ensure_catalog(size: int) -> None:
    with CATALOG_LOCK:
        if len(STATE["catalog"]) == size: return
        STATE["catalog"] = {}
        for i in range(size):
            sku = f"SKU{str(i+1).zfill(4)}"
            price = round(random.uniform(9.0, 199.0), 2)
            STATE["catalog"][sku] = {"sku": sku,"name": f"Demo Product {i+1}","price": price,
                "url": f"{BASE_URL}/product/{sku}","image": f"https://picsum.photos/seed/{sku}/600/400"}

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

# Health check
@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "time": now_iso()})

# Index page
@app.get("/")
def index():
    ensure_catalog(STATE["default_catalog_size"])
    creds_missing = not (PIXEL_ID and ACCESS_TOKEN)
    return render_template("index.html",
        default_catalog_size=STATE["default_catalog_size"],
        pixel_auto=STATE["pixel_auto"],
        server_auto={k:v for k,v in STATE["server_auto"].items() if k != "thread"},
        creds_missing=creds_missing,
        graph_ver=GRAPH_VER,
        env_file_exists=ENV_FILE_EXISTS,
    )

# Catalog page
@app.get("/catalog")
def catalog():
    ensure_catalog(STATE["default_catalog_size"])
    items = list(STATE["catalog"].values())
    return render_template("catalog.html", items=items)

# Product detail page
@app.get("/product/<sku>")
def product(sku):
    ensure_catalog(STATE["default_catalog_size"])
    item = STATE["catalog"].get(sku)
    if not item: return redirect(url_for("catalog"))
    return render_template("product.html", item=item)

if __name__ == "__main__":
    ensure_catalog(STATE["default_catalog_size"])
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
