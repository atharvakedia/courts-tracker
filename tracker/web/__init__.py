"""The web layer.

Deliberately import-free: the dashboard's Vercel function imports
``tracker.web.api`` only, and must not pull the older snapshot-era app
(``tracker.web.app``) and its storage stack into every cold start.
"""
