# Store Simulator v2.1.6

Fixes:
- Title no longer says "Demo".
- Pulsing dots show active status for Pixel Auto (browser) and Server Auto.
- Discrepancy & Chaos panel has a persistent on/off toggle; inputs disabled when off.
- Self-test buttons (Pixel) are present.
- Toggle to "Share event_id to CAPI" (also in Advanced Controls). When enabled, pixel telemetry is forwarded to CAPI using the same event_id for dedup.
- `/chaos/reset` sets all chaos/discrepancy knobs back to neutral.

## Run locally

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export PIXEL_ID=YOUR_PIXEL
export ACCESS_TOKEN=YOUR_TOKEN
python app.py
```

or with Gunicorn:

```
gunicorn app:app -b 0.0.0.0:5000 -w 2 -k gthread --threads 4 --timeout 120
```

## Render

Build Command: `pip install -r requirements.txt`  
Start Command: `gunicorn app:app -b 0.0.0.0:$PORT -w 2 -k gthread --threads 4 --timeout 120`

