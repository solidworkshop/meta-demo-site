# E-commerce Simulator (v2.2.0)

- Live top toggles (Pixel, CAPI, Test Events) – no Save needed.
- Chaos Generator (renamed) + Reset button.
- AppendValue diagnostic events (CAPI-only) with isolation mode.
- User Data Signals toggles for email (hashed), IP, fbc, fbp.

## Run
```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill PIXEL_ID, ACCESS_TOKEN, etc.
python app.py  # dev
# or:
gunicorn wsgi:app -b 0.0.0.0:5000
```
