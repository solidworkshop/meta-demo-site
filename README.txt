E‑commerce Simulator — Quick Start
==================================

What you get
------------
- Light-mode Flask app with three columns:
  - Manual Sender (Pixel / CAPI / Both)
  - Pixel Auto (browser)
  - CAPI Auto (server)
- Per-column controls: delay, currency (Auto/Null/specific), null toggles, margin cost% min/max, PLTV
- Product catalog with unique URLs: /catalog and /product/<sku>
- Button success/failure via **border** (green flash on success, red holds on error)
- Feature flags + Lite mode for fast page loads

Run
---
1) Python 3.9+
2) `pip install flask`
3) `python app.py`
4) Open http://127.0.0.1:5000/?lite=1 for fastest iteration.

Env Vars (optional)
-------------------
- PIXEL_ID, ACCESS_TOKEN, TEST_EVENT_CODE
- GRAPH_VER (default v20.0), BASE_URL (default http://127.0.0.1:5000)
- CATALOG_SIZE (default 20)
- LITE_MODE=1 (default off)
- ENABLE_PIXEL_AUTO=1 / ENABLE_CAPI_AUTO=1 / ENABLE_CATALOG_UI=1

Notes
-----
- No external network calls are made; CAPI is emulated as "ok" if creds exist, otherwise "dry-run".
- Use the Manual Sender buttons to test payloads instantly.
- Add `?lite=1` during development to avoid heavy UI and timers.
