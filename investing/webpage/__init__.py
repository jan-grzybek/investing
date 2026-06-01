"""``Webpage`` renderer + the ``generate_webpage`` entrypoint.

The renderer used to be a single ~2000-line ``investing/webpage.py``
module. Splitting it into a package lets the self-contained helpers
(``head`` / ``og_image`` / ``sitemap`` / ``anchors`` / ``footer``)
live in their own files so each one can be edited and reviewed
without scrolling past the rest of the renderer. The main ``Webpage``
class and its per-section renderers stay together in
:mod:`investing.webpage._page` because they share enough internal
state to make a finer-grained split add more friction than it
removes.

The public surface is intentionally identical to the old module:
imports of ``Webpage`` / ``generate_webpage`` from
``investing.webpage`` keep working unchanged.
"""

from __future__ import annotations

from ._page import Webpage, generate_webpage

__all__ = ["Webpage", "generate_webpage"]
