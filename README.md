# E‑commerce Simulator (Light Mode)

A self-contained Flask app to simulate Meta Pixel & Conversions API (CAPI) traffic with rich controls.

## Features
- Three columns:
  1) **Manual Sender** (Pixel / CAPI / Both)
  2) **Pixel Auto (browser)** – simulated loop from client
  3) **CAPI Auto (server)** – background loop on the server
- Per-column **Advanced Controls** and **Discrepancy & Chaos** (bad data toggles).
- **Master** enable/disable switches for Pixel and CAPI.
- **Margin** is sent as the event `value` and also included in `custom_data` along with `price`, `currency`, and `pltv`.
- **Delay**, **match-rate degradation**, **currency (Auto/Null/specific)**.
- Product **catalog** and **product pages** with unique URLs.

## Quickstart
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# Export your environment variables as needed
export PIXEL_ID=YOUR_PIXEL_ID
export ACCESS_TOKEN=YOUR_ACCESS_TOKEN
export TEST_EVENT_CODE=OPTIONAL_TEST_CODE
export GRAPH_VER=v21.0
export BASE_URL=http://127.0.0.1:5000
export DEFAULT_CATALOG_SIZE=24
python app.py
```

Open http://127.0.0.1:5000

> If `PIXEL_ID` or `ACCESS_TOKEN` is missing, CAPI requests are simulated and marked as such; Pixel is always simulated on the server for visibility.

## Deploying to Render
- Set **Start Command**: `python app.py`
- Add Environment:
  - `PIXEL_ID`, `ACCESS_TOKEN`, `TEST_EVENT_CODE` (optional), `GRAPH_VER` (e.g., `v21.0`), `BASE_URL` (your Render URL), `DEFAULT_CATALOG_SIZE`.
- Expose port **5000**.

## Endpoints
- `GET /` — Main UI
- `GET /catalog` — Grid view of products
- `GET /product/<sku>` — Product detail
- `POST /api/master` — Toggle master Pixel/CAPI
- `POST /api/catalog/size` — Set number of unique products
- `POST /api/manual/send` — Send one event (Pixel/CAPI/Both)
- `POST /api/server_auto/start` — Start server loop (CAPI)
- `POST /api/server_auto/stop` — Stop server loop

## Notes
- Button feedback uses **border flash**: green on success; red persists for errors.
- Server auto loop is a daemon thread. Stopping the service also stops the loop.
- Delay is capped at 3000ms per event to keep the UI responsive.
