# E-commerce Simulator v2.1.3

## Setup
1. Create a `.env` file next to `app.py` (Render: Dashboard → Environment):
```
PIXEL_ID=your_pixel_id
ACCESS_TOKEN=your_capi_token
TEST_EVENT_CODE=optional_test_id
BASE_URL=https://your-domain
GRAPH_VER=v20.0
FILE_SINK_PATH=events.ndjson
GA4_MEASUREMENT_ID=
GA4_API_SECRET=
WEBHOOK_URL=
WEBHOOK_HEADERS={}
```
2. Install deps: `pip install -r requirements.txt`
3. Run locally: `python app.py`  (or `gunicorn app:app` in prod)

## Notes
- Discrepancy & Chaos has a master toggle. OFF disables the UI and all chaos effects at runtime.
- Self Test button hits `/selftest/run` and attempts a tiny CAPI event (uses TEST_EVENT_CODE if set).
