"""The HTTP layer: a JSON API over the analytics functions, plus the dashboard.

``tracker.web:app`` is what uvicorn serves (``make run``). The module-level
``app`` builds the routes and mounts the static dashboard but opens nothing;
the configuration and the database are opened by the application lifespan, so
importing this package has no side effect beyond ensuring the static directory
exists.

Use :func:`create_app` directly to inject an already-open
:class:`~tracker.storage.Storage`, a :class:`~tracker.config.Config` or a
fixed clock -- which is how the tests run the whole API against in-memory
SQLite without touching the filesystem.
"""

from tracker.web.app import app, create_app, router

__all__ = ["app", "create_app", "router"]
