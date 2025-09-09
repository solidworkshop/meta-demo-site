# E-commerce Simulator — Full Bundle (Additive, No Removals)

This bundle gives you a **single download** you can drop into your project to add password protection
and ensure Gunicorn starts correctly on Render — **without changing your existing `app.py`**.

## What’s inside
- `authwrap.py` — Basic Auth WSGI middleware
- `wsgi.py` — wraps the existing Flask `app`
- `Procfile` — starts Gunicorn with `wsgi:app` (so auth is active)
- `requirements.txt` — includes `gunicorn`, `flask`, `requests`, `python-dotenv`
- `.env.example` — includes all expected env vars (Pixel/CAPI + Basic Auth)
- `.presets/` — placeholder so the folder exists for your preset saves

## How to deploy (Render)
1. Upload/replace these files in your repo root **alongside your current `app.py`**.
2. Render → Settings → Environment:
   - Set your existing values (PIXEL_ID, ACCESS_TOKEN, TEST_EVENT_CODE, etc.).
   - Add:
     - `BASIC_AUTH_ENABLED=true`
     - `BASIC_AUTH_USERNAME=<your user>`
     - `BASIC_AUTH_PASSWORD=<your password>`
     - (optional) `BASIC_AUTH_REALM=Restricted`
     - (optional) `BASIC_AUTH_EXEMPT=/healthz,/version`
3. Start command (or Procfile in repo):
   ```bash
   gunicorn wsgi:app -b 0.0.0.0:$PORT -w 2 -k gthread --threads 4 --timeout 120
   ```
4. Redeploy. You’ll get a Basic Auth prompt on the site. Preset and scenario features remain unchanged.

## Notes
- **No removals**: we did not alter your existing app or UI; these are additive files only.
- To disable password protection later: set `BASIC_AUTH_ENABLED=false` and redeploy.
- Outbound integrations (CAPI, GA4, webhooks) are unaffected by Basic Auth.
