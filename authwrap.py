import base64, os
class BasicAuthMiddleware:
    def __init__(self, app):
        self.app = app
        self.user = os.getenv("BASIC_AUTH_USER") or ""
        self.pw = os.getenv("BASIC_AUTH_PASS") or ""
    def __call__(self, environ, start_response):
        if not (self.user and self.pw):
            return self.app(environ, start_response)
        auth = environ.get("HTTP_AUTHORIZATION", "")
        if auth.startswith("Basic "):
            try:
                raw = base64.b64decode(auth.split(" ",1)[1]).decode("utf-8")
                u, p = raw.split(":",1)
                if u == self.user and p == self.pw:
                    return self.app(environ, start_response)
            except Exception:
                pass
        start_response('401 Unauthorized', [('WWW-Authenticate','Basic realm="Restricted"'),('Content-Type','text/plain')])
        return [b'Unauthorized']
