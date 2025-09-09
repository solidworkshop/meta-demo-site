# wsgi.py — wraps your Flask app with BasicAuthMiddleware; app.py remains untouched.
import os
from app import app as flask_app
from authwrap import BasicAuthMiddleware

def _truthy(v: str) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "t", "yes", "on")

ENABLED  = _truthy(os.getenv("BASIC_AUTH_ENABLED", "false"))
USER     = os.getenv("BASIC_AUTH_USERNAME", "")
PASSWORD = os.getenv("BASIC_AUTH_PASSWORD", "")
REALM    = os.getenv("BASIC_AUTH_REALM", "Restricted")
EXEMPT_RAW = os.getenv("BASIC_AUTH_EXEMPT", "/healthz,/version")
EXEMPT = [p.strip() for p in EXEMPT_RAW.split(",") if p.strip()]

app = BasicAuthMiddleware(
    flask_app,
    enabled=ENABLED,
    username=USER,
    password=PASSWORD,
    realm=REALM,
    exempt_paths=EXEMPT,
)
