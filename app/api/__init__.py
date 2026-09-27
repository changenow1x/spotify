"""Local dashboard / control API.

Binds to ``127.0.0.1`` by default and serves only the local machine.  It works
with no internet connection at all - every endpoint except the live Spotify
checks reads local state only.
"""

from .server import create_app, run_server
from .routes import router

__all__ = ["create_app", "run_server", "router"]
