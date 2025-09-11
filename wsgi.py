import os
from app import app as flask_app
from authwrap import BasicAuthMiddleware
app = BasicAuthMiddleware(flask_app)
