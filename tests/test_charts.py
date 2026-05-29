"""Tests for the chart-generation helpers in update.py.

The ``write_image`` / ``Figure`` interactions are stubbed via monkeypatch
so we don't depend on the ``kaleido`` binary in CI.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

import update
from update import generate_horizontal_bar, generate_return_plot


class TestGenerateHorizontalBar:
    def test_no_data_is_a_noop(self, monkeypatch):
        # If make_subplots is ever called with None data, the test would
        # explode. So we patch it with a sentinel that raises.
        def explode(*args, **kwargs):  # noqa: ARG001
            raise AssertionError("should not be called when data is None")

        monkeypatch.setattr(update, "make_subplots", explode)
        # Must return without raising.
        assert generate_horizontal_bar(None, "anything", "#000000") is None

    def test_writes_svg_with_expected_path(self, monkeypatch, chdir_tmp):
        captured_path = {}

        fake_layout = {
            "annotations": [],
            "margin": {},
            "height": 0,
            "width": 0,
        }

        class FakeSubplots(dict):
            def __init__(self):
                super().__init__()
                self["layout"] = fake_layout
                self.add_trace = MagicMock()

            def write_image(self, path):
                captured_path["path"] = path

        monkeypatch.setattr(update, "make_subplots", lambda **_: FakeSubplots())

        generate_horizontal_bar({"AAA": 60.0, "BBB": 40.0}, "alloc", "#1f4e79")
        assert captured_path["path"] == "assets/alloc.svg"

    def test_traces_are_added_per_data_item(self, monkeypatch, chdir_tmp):
        traces = []

        class FakeSubplots(dict):
            def __init__(self):
                super().__init__()
                self["layout"] = {"annotations": [], "margin": {}, "height": 0, "width": 0}

            def add_trace(self, trace, row, col):  # noqa: ARG002
                traces.append(trace)

            def write_image(self, path):  # noqa: ARG002
                pass

        monkeypatch.setattr(update, "make_subplots", lambda **_: FakeSubplots())
        generate_horizontal_bar({"A": 1.0, "B": 2.0, "C": 3.0}, "x", "#fff")
        assert len(traces) == 3
        # Each trace tags the bar with its formatted % label.
        assert [t["text"][0] for t in traces] == ["1.0%", "2.0%", "3.0%"]


class TestGenerateReturnPlot:
    def test_writes_svg_to_assets(self, monkeypatch, chdir_tmp):
        # Build a minimal total_return with two history points so the
        # interpolator has something to chew on.
        total_return = {
            "history": [
                (datetime(2024, 1, 1), 1.0),
                (datetime(2024, 6, 1), 1.1),
                (datetime(2024, 12, 1), 1.2),
            ],
        }
        benchmarks = [{
            "ticker": "LSE:VUAA.L",
            "history": [
                (datetime(2024, 1, 1), 1.0),
                (datetime(2024, 6, 1), 1.05),
                (datetime(2024, 12, 1), 1.15),
            ],
        }]

        captured = {}

        class AutoDict(dict):
            """Mimics plotly's auto-vivifying nested layout dicts."""

            def __getitem__(self, key):
                if key not in self:
                    super().__setitem__(key, AutoDict())
                return super().__getitem__(key)

        class FakeFig:
            def __init__(self):
                self._layout = AutoDict()

            def add_trace(self, *_args, **_kwargs):
                pass

            def add_hline(self, *_args, **_kwargs):
                pass

            def __setitem__(self, key, value):
                self._layout[key] = value

            def __getitem__(self, key):
                return self._layout[key]

            def write_image(self, path):
                captured["path"] = path

        monkeypatch.setattr(update.go, "Figure", FakeFig)
        # Scatter is constructed inside add_trace; we don't need a real one.
        monkeypatch.setattr(update.go, "Scatter", lambda **kw: kw)

        generate_return_plot(total_return, benchmarks)
        assert captured["path"] == "assets/return.svg"
