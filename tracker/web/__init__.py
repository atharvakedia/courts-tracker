"""The web layer.

Deliberately import-free: the dashboard's Vercel function imports
``tracker.web.api`` only, and the local dev server (``tracker.web.dev``) is
loaded by uvicorn only when ``python -m tracker serve`` runs.
"""
