"""Phase 13 - route blueprints package.

Each module owns one slice of the old app.py. They import shared
helpers from core.py and config.py - never from app.py itself - so
there are no circular imports.
"""
