# E-commerce Simulator — Full Build (.env + Gunicorn-ready)

## Quickstart (local)
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env   # set PIXEL_ID, ACCESS_TOKEN, etc.
python app.py
```

## Deploy (Render or similar)
- **Start Command (simple):**
  ```bash
  python app.py
  ```
- **Start Command (gunicorn):**
  ```bash
  gunicorn -w 4 -b 0.0.0.0:5000 app:app
  ```
- **Env Vars:** set `PIXEL_ID`, `ACCESS_TOKEN`, `TEST_EVENT_CODE` (optional), `GRAPH_VER`, `BASE_URL`, `DEFAULT_CATALOG_SIZE`.

## Notes
- If `PIXEL_ID`/`ACCESS_TOKEN` not set, CAPI calls are simulated and marked in the response.
- Catalog size uses app state: `STATE["default_catalog_size"]` (no global mutation).
- Buttons flash green on success; red border persists on error.
