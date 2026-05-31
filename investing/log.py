"""Module-level logger shared across the package."""
from __future__ import annotations

import logging

# Module-level logger. All progress / diagnostic output goes through
# this rather than ``print()`` so the production entrypoint
# (``_run_main_safely``) can keep stderr redacted while local /
# preview runs can opt back in by configuring a handler (see
# ``_configure_logging``). Logging to stderr (not stdout) keeps these
# messages in the same lane the leak-safe wrapper already polices --
# previously the ``print()`` calls landed on stdout, which the wrapper
# does NOT scrub, so the diagnostics here are now strictly more
# private than what came before.
logger = logging.getLogger("investing.update")
