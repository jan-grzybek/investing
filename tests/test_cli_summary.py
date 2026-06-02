"""Tests for the redacted build-summary line emitted by ``main``.

The summary lands on stdout (which the leak-safe wrapper leaves
intact) and is deliberately composed only of quantities the
rendered page itself publishes. The tests assert on shape -- a
positive build signal in the GitHub Actions log -- and on the
negative contract (no holding identifiers, no cash, no FX
values, etc.).
"""

from __future__ import annotations

from datetime import datetime

from investing.cli import _format_maintenance_hints, _print_summary
from investing.sector_overrides import MaintenanceHints


def _total_return(twr_pct=12.3, cagr_pct=5.5, start=datetime(2024, 1, 1)):
    return {
        "twr%": twr_pct,
        "cagr%": cagr_pct,
        "start_date": start,
        "history": [(start, 1.0)],
    }


def _holdings(current=3, historical=1):
    return {
        "current": [{"ticker": f"T{i}"} for i in range(current)],
        "historical": [{"ticker": f"H{i}"} for i in range(historical)],
    }


def test_emits_one_line_summary(capsys):
    _print_summary(_total_return(), _holdings(), benchmarks=[])
    captured = capsys.readouterr()
    out = captured.out.strip().splitlines()
    assert len(out) == 1
    assert out[0].startswith("Build OK:")
    assert "TWR 12.3%" in out[0]
    assert "CAGR 5.5%" in out[0]
    assert "3 current / 1 historical holdings" in out[0]


def test_includes_benchmark_delta_when_present(capsys):
    benchmarks = [{"ticker": "LSE:VUAA.L", "cagr%": 4.2}]
    _print_summary(_total_return(cagr_pct=6.7), _holdings(), benchmarks)
    out = capsys.readouterr().out
    # Delta is unrounded percentage points.
    assert "delta +2.5 pp" in out
    assert "benchmark CAGR 4.2%" in out


def test_no_benchmark_section_when_empty(capsys):
    _print_summary(_total_return(), _holdings(), benchmarks=[])
    out = capsys.readouterr().out
    assert "benchmark" not in out
    assert "delta" not in out


def test_does_not_leak_ticker_identifiers_or_values(capsys):
    """The summary line must not surface anything beyond the page's
    public quantities -- no ticker symbols, no cash balances, no FX
    rates. Plant identifiable canaries in the inputs and assert
    they're absent from stdout."""
    total_return = _total_return()
    holdings = {
        "current": [{"ticker": "NMS:CANARY_TICKER_42"}],
        "historical": [],
    }
    benchmarks = [{"ticker": "LSE:CANARY_BENCH_99", "cagr%": 3.0}]
    _print_summary(total_return, holdings, benchmarks)
    out = capsys.readouterr().out
    assert "CANARY_TICKER_42" not in out
    assert "CANARY_BENCH_99" not in out


def test_handles_missing_keys_gracefully(capsys):
    """A partially-populated ``total_return`` must not crash the summary."""
    _print_summary({"twr%": 1.0, "cagr%": 0.5}, _holdings(0, 0), benchmarks=[])
    out = capsys.readouterr().out
    assert "TWR 1.0%" in out
    assert "CAGR 0.5%" in out
    assert "0 current / 0 historical" in out


class TestMaintenanceLine:
    """The build summary emits an optional ``Maintenance: ...`` line
    when the per-build hint registry collected any actionable
    findings (missing sectors, invalid overrides, missing logos).
    The line rides on the same curated-stdout channel as the main
    summary so it survives the leak-safe wrapper's redaction; it is
    composed exclusively of ticker symbols (already public) and
    static category labels.
    """

    def test_no_maintenance_line_when_hints_empty(self, capsys):
        _print_summary(
            _total_return(),
            _holdings(),
            benchmarks=[],
            maintenance=MaintenanceHints(),
        )
        out = capsys.readouterr().out
        assert "Maintenance:" not in out

    def test_no_maintenance_line_when_argument_omitted(self, capsys):
        # The kwarg defaults to ``None`` so existing callers (every
        # legacy test in this file) don't have to thread anything
        # through; the line is also suppressed in that case.
        _print_summary(_total_return(), _holdings(), benchmarks=[])
        out = capsys.readouterr().out
        assert "Maintenance:" not in out

    def test_maintenance_line_lists_missing_sectors(self, capsys):
        hints = MaintenanceHints(missing_sector=["NMS:AAA", "NYQ:BBB"])
        _print_summary(_total_return(), _holdings(), [], maintenance=hints)
        out = capsys.readouterr().out
        assert "Maintenance:" in out
        assert "missing sectors: NMS:AAA, NYQ:BBB" in out

    def test_maintenance_line_lists_missing_logos(self, capsys):
        hints = MaintenanceHints(missing_logos=["NMS:XYZ"])
        _print_summary(_total_return(), _holdings(), [], maintenance=hints)
        out = capsys.readouterr().out
        assert "missing logos: NMS:XYZ" in out

    def test_maintenance_line_lists_invalid_overrides(self, capsys):
        hints = MaintenanceHints(invalid_overrides={"NMS:AAA": "Tech"})
        _print_summary(_total_return(), _holdings(), [], maintenance=hints)
        out = capsys.readouterr().out
        assert "invalid sector overrides:" in out
        assert "NMS:AAA='Tech'" in out


class TestFormatMaintenanceHints:
    def test_empty_hints_returns_empty_string(self):
        assert _format_maintenance_hints(MaintenanceHints()) == ""

    def test_categories_joined_with_pipe_separator(self):
        hints = MaintenanceHints(
            missing_sector=["NMS:AAA"],
            missing_logos=["NMS:BBB"],
        )
        out = _format_maintenance_hints(hints)
        assert "missing sectors: NMS:AAA" in out
        assert "missing logos: NMS:BBB" in out
        # Two categories -> one pipe between them.
        assert out.count(" | ") == 1
