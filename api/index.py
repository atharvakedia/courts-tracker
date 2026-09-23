"""Vercel entry point: the FastAPI app, served for every /api/* route."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker.web.api import app

__all__ = ["app"]
