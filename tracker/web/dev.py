"""The local dev server: the Vercel JSON API plus the static dashboard, in one app.

On Vercel the two are served separately: ``public/`` as static files and
``/api/*`` by the function in ``api/index.py``. Locally ``python -m tracker
serve`` runs this module instead, so one process answers both. The static mount
is added last and sits at ``/``, so every ``/api/*`` route still matches first.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.staticfiles import StaticFiles

from tracker.web.api import app

PUBLIC_DIR = Path(__file__).resolve().parents[2] / "public"

app.mount("/", StaticFiles(directory=PUBLIC_DIR, html=True), name="public")
