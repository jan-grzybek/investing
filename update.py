"""Production launcher for the page generator.

The GitHub Actions deploy workflow (``.github/workflows/main.yml``)
invokes this file as ``python update.py``. It is the one path that
should ever reach :func:`investing.safe_run._run_main_safely` from
the command line: the wrapper redacts stderr for the duration of
the build and emits a sanitized failure summary, so a deploy that
crashes still surfaces enough to debug without leaking secrets or
nominal portfolio values into the world-readable job log.

The legacy shim that re-exported the package's internals (so tests
could ``import update`` and reach for ``update.<symbol>``) was
removed when the test suite migrated to direct ``from investing.*``
imports. New code should follow the same pattern; nothing in this
file is meant to be imported.
"""
from __future__ import annotations

from investing.safe_run import _run_main_safely

if __name__ == "__main__":
    _run_main_safely()
