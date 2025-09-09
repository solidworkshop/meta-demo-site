# authwrap.py — WSGI Basic Auth middleware for Flask (additive)
import base64

class BasicAuthMiddleware:
    def __init__(self, app, enabled=False, username="", password="", realm="Restricted",
                 exempt_paths=None):
        self.app = app
        self.enabled = bool(enabled)
        self.username = username or ""
        self.password = password or ""
        self.realm = realm
        self.exempt_paths = set(exempt_paths or [])

    def _is_exempt(self, path):
        if path in self.exempt_paths:
            return True
        for p in self.exempt_paths:
            if p.endswith("/*") and path.startswith(p[:-1]):
                return True
        return False

    def __call__(self, environ, start_response):
        if not self.enabled:
            return self.app(environ, start_response)

        path = environ.get("PATH_INFO", "/") or "/"
        if self._is_exempt(path):
            return self.app(environ, start_response)

        auth_header = environ.get("HTTP_AUTHORIZATION")
        if not auth_header or not auth_header.startswith("Basic "):
            return self._challenge(start_response)

        try:
            decoded = base64.b64decode(auth_header.split(" ", 1)[1]).decode("utf-8")
        except Exception:
            return self._challenge(start_response)

        if ":" not in decoded:
            return self._challenge(start_response)

        user, pw = decoded.split(":", 1)
        if user == self.username and pw == self.password:
            return self.app(environ, start_response)

        return self._challenge(start_response)

    def _challenge(self, start_response):
        headers = [
            ("Content-Type", "text/plain; charset=utf-8"),
            ("WWW-Authenticate", f'Basic realm="{self.realm}", charset="UTF-8"'),
        ]
        start_response("401 Unauthorized", headers)
        return [b"Authentication required."]
