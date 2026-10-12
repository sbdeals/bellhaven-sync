"""Reviewer-facing web app (FastAPI + Jinja2). Entry point: `create_app`."""

from .main import create_app

__all__ = ["create_app"]
