"""Repo paths and the small ``_read_asset`` helper used to
load inline CSS / JS payloads from ``assets/``.
"""
from __future__ import annotations

import os

LOGOS_ADDRESS = "https://jan-grzybek.github.io/investing/logos/"


COURAGE_LOGO = LOGOS_ADDRESS + "courage.png"


LOGO_EXTENSIONS = (".svg", ".png", ".jpg")



# Repo root: the directory holding the ``investing/`` package, the
# ``assets/`` source directory, and the ``logos/`` mirror. Resolved
# relative to this file so the CI build and the local preview both
# work regardless of the caller's CWD.
_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Local mirror of ``LOGOS_ADDRESS`` -- the same files served at the URL
# above live next to ``update.py`` in the repo (and ship as part of the
# Pages artifact). The OG image renderer rasterises logos for the
# top-10 strip and reads them straight from disk so it doesn't depend
# on the previous deploy being reachable.
_REPO_LOGOS_DIR = os.path.join(_REPO_DIR, "logos")


# Source-of-truth directory for the inline CSS / JS payloads embedded in
# the rendered page. Each constant below loads its content from a file
# under ``assets/`` so editors can lint / format the real CSS and JS
# rather than the equivalent Python string literal.
_ASSETS_DIR = os.path.join(_REPO_DIR, "assets")




def _read_asset(name: str) -> str:
    """Read an ``assets/<name>`` file as UTF-8 text."""
    with open(os.path.join(_ASSETS_DIR, name), encoding="utf-8") as f:
        return f.read()
