"""``investing.__main__`` only builds when run as a script."""

from __future__ import annotations

import runpy

import investing.safe_run as safe_run


def test_importing_the_module_does_not_start_a_build(monkeypatch):
    """Importing ``investing.__main__`` (tooling, doc generators) is inert.

    Without the ``__name__`` guard a plain import would kick off a
    full build -- pulling the spreadsheet and writing the page.
    """
    called: list[str] = []
    monkeypatch.setattr(safe_run, "_run_main_safely", lambda: called.append("main"))
    monkeypatch.setattr(safe_run, "_run_snapshot_safely", lambda: called.append("snapshot"))

    runpy.run_module("investing", run_name="investing.__main__")

    assert called == []
