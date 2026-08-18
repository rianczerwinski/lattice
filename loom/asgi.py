"""Production ASGI application."""

from .server import create_app

app = create_app()
