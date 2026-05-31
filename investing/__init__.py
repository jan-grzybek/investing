"""``investing`` package: the page generator behind the JG Investing portfolio page.

The public surface is intentionally tiny -- this is an application, not
a library. Consumers should reach for one of the entrypoints below; the
internal modules (``investing.holdings``, ``investing.webpage``, ...)
are subject to change without notice.

Entrypoints:
    * :func:`main` -- production data pipeline + render
    * :func:`generate_webpage` -- render an already-built data bundle
    * ``python -m investing`` -- launch :func:`main` through the
      leak-safe wrapper (:mod:`investing.__main__`)
"""
from __future__ import annotations

from .cli import main
from .webpage import generate_webpage

__all__ = ["generate_webpage", "main"]
