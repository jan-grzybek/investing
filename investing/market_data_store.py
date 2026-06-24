"""On-disk snapshots of public market data (yfinance).

Archives splits, dividends, FX history, benchmark price series, and a
curated ``info`` subset so builds keep working when Yahoo drops or
revises historical rows. Spreadsheet / portfolio ledger data must
never be written here — only vendor market feeds keyed by ticker or
currency symbol.

Set ``INVESTING_MARKET_DATA_PERSIST=0`` to merge live yfinance with
on-disk archives without writing updates (production deploy default).
The monthly CI cron persists and commits snapshot refreshes.

Merge policy (split-aware):

* **Splits** — union by date; live wins on same-date factor conflicts;
  archived-only rows are kept when Yahoo drops them.
* **Dividends / adj-close history** — when the split inventory is
  unchanged, union by date with archive winning conflicts (preserves
  Yahoo-dropped rows). When new or changed splits appear, live rows
  define the current share frame for overlapping dates; archived-only
  dates are re-based by dividing out every *new* split factor strictly
  after the row date.
* **FX daily rates** — union by date; archive wins on conflicts.
* **``info``** — slow-changing metadata (``sector``, names, …) is
  merged with archive filling gaps when live is blank; ``regularMarketPrice``
  is live-only and is never written to disk.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import os
import tempfile
import threading
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import yfinance as yf

from .formatting import _ts_to_datetime
from .log import logger
from .market_data import MarketDataError, _call_with_retry

SCHEMA_VERSION = 1

_MARKET_DATA_DIR_ENV = "INVESTING_MARKET_DATA_DIR"
_DISABLE_ENV = "INVESTING_MARKET_DATA_DISABLE"
_PERSIST_ENV = "INVESTING_MARKET_DATA_PERSIST"

# Curated ``get_info`` keys the pipeline consumes. Storing the full
# upstream blob would bloat diffs and pull in irrelevant fields.
INFO_KEYS: frozenset[str] = frozenset(
    {
        "currency",
        "exchange",
        "symbol",
        "longName",
        "shortName",
        "regularMarketPrice",
        "sector",
        "website",
        "irWebsite",
    }
)

# ``info`` fields persisted in committed ticker snapshots. Live tape
# price is excluded so intraday deploy runs do not churn ``main``.
PERSISTED_INFO_KEYS: frozenset[str] = frozenset(
    key for key in INFO_KEYS if key != "regularMarketPrice"
)

# Keys that must never appear in committed snapshot JSON — they belong
# to the private spreadsheet ledger, not vendor market data.
FORBIDDEN_SNAPSHOT_KEYS: frozenset[str] = frozenset(
    {
        "quantity",
        "price",
        "regularMarketPrice",
        "flow",
        "amount",
        "action",
        "include",
        "value",
        "ticker",
    }
)

_FLOAT_TOLERANCE = 1e-9


def _persist_enabled() -> bool:
    """Return False when ``INVESTING_MARKET_DATA_PERSIST=0`` (read/merge only)."""
    return os.environ.get(_PERSIST_ENV, "1") != "0"


def _repo_market_data_dir() -> Path:
    from .paths import _REPO_DIR

    return Path(_REPO_DIR) / "market_data"


def market_data_root() -> Path | None:
    """Return the configured snapshot root, or ``None`` when disabled."""
    if os.environ.get(_DISABLE_ENV) == "1":
        return None
    raw = os.environ.get(_MARKET_DATA_DIR_ENV)
    if raw is not None:
        if raw.strip() == "":
            return None
        return Path(raw).expanduser()
    return _repo_market_data_dir()


def _iso_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def _parse_iso_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d")


def _split_map(splits: Iterable[dict[str, Any]]) -> dict[str, float]:
    return {_iso_date(s["date"]): float(s["split"]) for s in splits}


def split_inventory_changed(
    archived_splits: list[dict[str, Any]],
    merged_splits: list[dict[str, Any]],
) -> bool:
    """Return True when ``merged_splits`` adds or revises a split row."""
    archived = _split_map(archived_splits)
    for split in merged_splits:
        key = _iso_date(split["date"])
        factor = float(split["split"])
        if key not in archived:
            return True
        if not math.isclose(archived[key], factor, rel_tol=0.0, abs_tol=_FLOAT_TOLERANCE):
            return True
    return False


def merge_splits(
    archived: list[dict[str, Any]],
    live: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Union split rows by date; live wins on same-date conflicts."""
    by_date: dict[str, dict[str, Any]] = {
        _iso_date(s["date"]): {"date": s["date"], "split": float(s["split"])} for s in archived
    }
    for split in live:
        key = _iso_date(split["date"])
        by_date[key] = {"date": split["date"], "split": float(split["split"])}
    return [by_date[k] for k in sorted(by_date)]


def _new_splits(
    archived_splits: list[dict[str, Any]],
    merged_splits: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    archived_dates = {_iso_date(s["date"]) for s in archived_splits}
    return [s for s in merged_splits if _iso_date(s["date"]) not in archived_dates]


def _rebase_amount(amount: float, div_date: datetime, new_splits: list[dict[str, Any]]) -> float:
    factor = 1.0
    for split in new_splits:
        if split["date"] > div_date:
            factor *= float(split["split"])
    if factor == 1.0:
        return amount
    return amount / factor


def merge_time_series(
    archived: list[dict[str, Any]],
    live: list[dict[str, Any]],
    *,
    value_key: str,
    archived_splits: list[dict[str, Any]],
    merged_splits: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge dated scalar series with split-aware semantics.

    Used for dividends (``value_key="dividend"``) and benchmark
    ``Adj Close`` history (``value_key="adj_close"``). Pass empty
    split lists for FX (simple union with archive winning conflicts).
    """
    archived_by_date = {_iso_date(row["date"]): float(row[value_key]) for row in archived}
    live_by_date = {_iso_date(row["date"]): float(row[value_key]) for row in live}

    if not archived_splits and not merged_splits:
        inventory_changed = False
        new_split_rows: list[dict[str, Any]] = []
    else:
        inventory_changed = split_inventory_changed(archived_splits, merged_splits)
        new_split_rows = _new_splits(archived_splits, merged_splits)

    merged_values: dict[str, float] = {}

    if not inventory_changed:
        merged_values.update(archived_by_date)
        for key, amount in live_by_date.items():
            if key not in merged_values:
                merged_values[key] = amount
    else:
        merged_values.update(live_by_date)
        for key, amount in archived_by_date.items():
            if key in live_by_date:
                live_val = live_by_date[key]
                rebased = _rebase_amount(amount, _parse_iso_date(key), new_split_rows)
                if not math.isclose(
                    rebased,
                    live_val,
                    rel_tol=1e-6,
                    abs_tol=_FLOAT_TOLERANCE,
                ):
                    logger.warning(
                        "market-data merge: %s on %s differs after re-base "
                        "(archive=%s, live=%s, rebased=%s); using live",
                        value_key,
                        key,
                        amount,
                        live_val,
                        rebased,
                    )
                continue
            dt = _parse_iso_date(key)
            merged_values[key] = _rebase_amount(amount, dt, new_split_rows)

    return [
        {"date": _parse_iso_date(key), value_key: merged_values[key]}
        for key in sorted(merged_values)
    ]


def _require_live_regular_market_price(
    info: dict[str, Any],
    *,
    ticker: str,
) -> float:
    """Return live ``regularMarketPrice`` or raise when the tape is unavailable."""
    price = info.get("regularMarketPrice")
    if price is None or (isinstance(price, float) and math.isnan(price)):
        raise MarketDataError(
            f"missing live regularMarketPrice for ticker {ticker!r}",
        )
    return float(price)


def merge_info(
    archived: dict[str, Any],
    live: dict[str, Any],
    *,
    ticker: str,
) -> dict[str, Any]:
    """Merge curated ``info`` dicts; archive fills gaps for persisted metadata only."""
    merged: dict[str, Any] = {}
    for key in PERSISTED_INFO_KEYS:
        live_val = live.get(key)
        if live_val is not None and not (isinstance(live_val, str) and not live_val.strip()):
            merged[key] = live_val
    for key in PERSISTED_INFO_KEYS:
        if key not in merged and key in archived:
            merged[key] = archived[key]
    merged["regularMarketPrice"] = _require_live_regular_market_price(live, ticker=ticker)
    return merged


def _curate_info(raw: dict[str, Any], *, ticker: str) -> dict[str, Any]:
    curated: dict[str, Any] = {}
    for key in INFO_KEYS:
        if key in raw:
            curated[key] = raw[key]
    curated["regularMarketPrice"] = _require_live_regular_market_price(raw, ticker=ticker)
    return curated


def _info_for_persistence(info: dict[str, Any]) -> dict[str, Any]:
    return {key: info[key] for key in PERSISTED_INFO_KEYS if key in info}


def _dividends_from_yfinance(raw: Any) -> list[dict[str, Any]]:
    return [{"date": _ts_to_datetime(ts), "dividend": float(amount)} for ts, amount in raw.items()]


def _splits_from_yfinance(raw: Any) -> list[dict[str, Any]]:
    return [{"date": _ts_to_datetime(ts), "split": float(factor)} for ts, factor in raw.items()]


def _history_rows_from_dataframe(
    history: Any, *, value_key: str = "adj_close"
) -> list[dict[str, Any]]:
    column_name = "Adj Close" if value_key == "adj_close" else value_key
    rows: list[dict[str, Any]] = []
    column = history[column_name]
    for ts, value in column.items():
        if value is None or (isinstance(value, float) and math.isnan(value)):
            continue
        rows.append({"date": _ts_to_datetime(ts), value_key: float(value)})
    return rows


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _content_hash(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def _validate_snapshot_privacy(payload: Any, *, path: str = "") -> None:
    """Raise ``ValueError`` if ``payload`` contains forbidden ledger keys."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in FORBIDDEN_SNAPSHOT_KEYS:
                raise ValueError(
                    f"forbidden snapshot key {key!r} at {path or '<root>'}",
                )
            _validate_snapshot_privacy(value, path=f"{path}.{key}" if path else key)
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            _validate_snapshot_privacy(item, path=f"{path}[{index}]")


def _serialize_ticker_snapshot(
    *,
    info: dict[str, Any],
    splits: list[dict[str, Any]],
    dividends: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "info": _info_for_persistence(info),
        "splits": [{"date": _iso_date(s["date"]), "split": float(s["split"])} for s in splits],
        "dividends": [
            {"date": _iso_date(d["date"]), "dividend": float(d["dividend"])} for d in dividends
        ],
    }


def _deserialize_ticker_snapshot(raw: dict[str, Any]) -> tuple[dict, list, list]:
    info = dict(raw.get("info") or {})
    info.pop("regularMarketPrice", None)
    splits = [
        {"date": _parse_iso_date(s["date"]), "split": float(s["split"])}
        for s in raw.get("splits") or []
    ]
    dividends = [
        {"date": _parse_iso_date(d["date"]), "dividend": float(d["dividend"])}
        for d in raw.get("dividends") or []
    ]
    return info, splits, dividends


def _serialize_history(
    rows: list[dict[str, Any]],
    *,
    splits: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "splits": [{"date": _iso_date(s["date"]), "split": float(s["split"])} for s in splits],
        "adj_close": [
            {"date": _iso_date(r["date"]), "adj_close": float(r["adj_close"])} for r in rows
        ],
    }


def _deserialize_history(
    raw: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = [
        {"date": _parse_iso_date(r["date"]), "adj_close": float(r["adj_close"])}
        for r in raw.get("adj_close") or []
    ]
    splits = [
        {"date": _parse_iso_date(s["date"]), "split": float(s["split"])}
        for s in raw.get("splits") or []
    ]
    return rows, splits


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _atomic_write_json(path: Path, payload: Any) -> None:
    _validate_snapshot_privacy(payload)
    text = json.dumps(payload, indent=2, sort_keys=True)
    text += "\n"
    _atomic_write_bytes(path, text.encode())


class MarketDataStore:
    """Read-through / write-through snapshot of yfinance market feeds."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        persist: bool | None = None,
    ) -> None:
        self._root = root if root is not None else market_data_root()
        self._persist = _persist_enabled() if persist is None else persist
        self._manifest_path = self._root / "manifest.json" if self._root is not None else None
        # ``get_holdings`` constructs ``Holding`` instances in a thread
        # pool; each miss persists a ticker snapshot and read-modify-
        # writes ``manifest.json``. Without a lock, concurrent writers
        # share one ``manifest.json.tmp`` and ``os.replace`` raises
        # ``FileNotFoundError`` when the first writer moves the temp
        # file before the second replaces it.
        self._manifest_lock = threading.Lock()

    @classmethod
    def from_env(cls) -> MarketDataStore:
        return cls(market_data_root())

    @property
    def enabled(self) -> bool:
        return self._root is not None

    @property
    def root(self) -> Path | None:
        return self._root

    @property
    def persist(self) -> bool:
        """True when merged snapshots are written back to disk."""
        return self._persist

    def _ticker_path(self, ticker: str) -> Path:
        assert self._root is not None
        safe = ticker.replace("/", "-")
        return self._root / "tickers" / f"{safe}.json"

    def _history_path(self, ticker: str) -> Path:
        assert self._root is not None
        safe = ticker.replace("/", "-")
        return self._root / "history" / f"{safe}.json"

    def _fx_path(self, currency: str) -> Path:
        assert self._root is not None
        return self._root / "fx" / f"{currency}.npz"

    def _load_json(self, path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            with path.open(encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def _load_ticker_snapshot(self, ticker: str) -> tuple[dict, list, list] | None:
        if self._root is None:
            return None
        raw = self._load_json(self._ticker_path(ticker))
        if raw is None:
            return None
        return _deserialize_ticker_snapshot(raw)

    def _save_ticker_snapshot(
        self,
        ticker: str,
        *,
        info: dict[str, Any],
        splits: list[dict[str, Any]],
        dividends: list[dict[str, Any]],
    ) -> None:
        if self._root is None or not self._persist:
            return
        payload = _serialize_ticker_snapshot(info=info, splits=splits, dividends=dividends)
        _atomic_write_json(self._ticker_path(ticker), payload)
        self._touch_manifest("tickers", ticker, _content_hash(payload))

    def _touch_manifest(self, section: str, key: str, digest: str) -> None:
        if self._manifest_path is None:
            return
        with self._manifest_lock:
            manifest = self._load_json(self._manifest_path) or {
                "schema_version": SCHEMA_VERSION,
                "tickers": {},
                "history": {},
                "fx": {},
            }
            manifest.setdefault(section, {})[key] = {
                "content_hash": digest,
                "updated_at": _iso_date(datetime.today()),
            }
            manifest["schema_version"] = SCHEMA_VERSION
            _atomic_write_json(self._manifest_path, manifest)

    def _fetch_live_ticker(self, ticker: str) -> tuple[dict, list, list]:
        yf_ticker = yf.Ticker(ticker)
        info = _curate_info(
            _call_with_retry(
                yf_ticker.get_info,
                description="yfinance get_info",
            ),
            ticker=ticker,
        )
        splits = _splits_from_yfinance(
            _call_with_retry(
                lambda: yf_ticker.splits,
                description="yfinance splits",
            )
        )
        dividends = _dividends_from_yfinance(
            _call_with_retry(
                yf_ticker.get_dividends,
                description="yfinance get_dividends",
            )
        )
        return info, splits, dividends

    def resolve_ticker(self, ticker: str) -> tuple[dict[str, Any], list, list]:
        """Return merged ``(info, splits, dividends)`` for ``ticker``."""
        archived = self._load_ticker_snapshot(ticker)

        if archived is None:
            info, splits, dividends = self._fetch_live_ticker(ticker)
            self._save_ticker_snapshot(
                ticker,
                info=info,
                splits=splits,
                dividends=dividends,
            )
            return info, splits, dividends

        arch_info, arch_splits, arch_dividends = archived
        live_info, live_splits, live_dividends = self._fetch_live_ticker(ticker)
        merged_splits = merge_splits(arch_splits, live_splits)
        merged_dividends = merge_time_series(
            arch_dividends,
            live_dividends,
            value_key="dividend",
            archived_splits=arch_splits,
            merged_splits=merged_splits,
        )
        merged_info = merge_info(arch_info, live_info, ticker=ticker)
        self._save_ticker_snapshot(
            ticker,
            info=merged_info,
            splits=merged_splits,
            dividends=merged_dividends,
        )
        return merged_info, merged_splits, merged_dividends

    def resolve_price_history(
        self,
        ticker: str,
        start: str,
        fetch_history: Callable[[], Any],
        *,
        merged_splits: list[dict[str, Any]],
    ) -> Any:
        """Return a pandas DataFrame merged with any on-disk history."""
        archived_rows, archived_splits = self._load_history_bundle(ticker)
        live_frame = fetch_history()
        live_rows = _history_rows_from_dataframe(live_frame, value_key="adj_close")
        merged_rows = merge_time_series(
            archived_rows,
            live_rows,
            value_key="adj_close",
            archived_splits=archived_splits,
            merged_splits=merged_splits,
        )
        if self._root is not None and self._persist:
            payload = _serialize_history(merged_rows, splits=merged_splits)
            _atomic_write_json(self._history_path(ticker), payload)
            self._touch_manifest("history", ticker, _content_hash(payload))

        return self._rows_to_history_frame(merged_rows, start)

    def _load_history_bundle(
        self,
        ticker: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if self._root is None:
            return [], []
        raw = self._load_json(self._history_path(ticker))
        if raw is None:
            return [], []
        return _deserialize_history(raw)

    @staticmethod
    def _rows_to_history_frame(rows: list[dict[str, Any]], start: str) -> Any:
        import pandas as pd

        if not rows:
            return pd.DataFrame(columns=["Adj Close"])
        dates = [r["date"] for r in rows]
        values = [r["adj_close"] for r in rows]
        frame = pd.DataFrame({"Adj Close": values}, index=pd.DatetimeIndex(dates, name="Date"))
        start_ts = pd.Timestamp(start)
        return frame.loc[frame.index >= start_ts]

    def load_fx_history(
        self,
        currency: str,
    ) -> tuple[np.ndarray, np.ndarray] | None:
        if self._root is None:
            return None
        path = self._fx_path(currency)
        if not path.is_file():
            return None
        try:
            data = np.load(path)
            return data["dates"], data["rates"]
        except (OSError, ValueError, KeyError):
            return None

    def save_fx_history(
        self,
        currency: str,
        dates: np.ndarray,
        rates: np.ndarray,
    ) -> None:
        if self._root is None or not self._persist:
            return
        try:
            path = self._fx_path(currency)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Serialize to memory first, then commit through a uniquely
            # named temp file in the destination directory. Writing
            # ``np.savez`` to a shared ``<currency>.tmp.npz`` would race
            # the ``os.replace`` if two threads ever persisted the same
            # currency at once -- the manifest path carries a lock for
            # exactly this hazard, and FX should not be the weaker link
            # if a future change moves FX resolution into the holdings
            # thread pool. A per-call temp name keeps the write atomic
            # and collision-free without taking a lock.
            buf = io.BytesIO()
            np.savez(buf, dates=dates, rates=rates)
            fd, tmp_name = tempfile.mkstemp(
                dir=path.parent,
                prefix=f".{currency}-",
                suffix=".npz.tmp",
            )
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(buf.getvalue())
                os.replace(tmp_name, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
                raise
        except OSError as exc:
            logger.debug("FX snapshot write failed for %s: %s", currency, exc)

    def merge_fx_history(
        self,
        currency: str,
        live_dates: list,
        live_rates: list[float],
    ) -> tuple[np.ndarray, np.ndarray]:
        archived = self.load_fx_history(currency)
        archived_rows: list[dict[str, Any]] = []
        if archived is not None:
            date_arr, rate_arr = archived
            for day, rate in zip(date_arr, rate_arr, strict=True):
                archived_rows.append(
                    {
                        "date": datetime.strptime(str(day), "%Y-%m-%d"),
                        "rate": float(rate),
                    }
                )
        live_rows = [
            {
                "date": (
                    d if isinstance(d, datetime) else datetime.combine(d, datetime.min.time())
                ),
                "rate": float(r),
            }
            for d, r in zip(live_dates, live_rates, strict=True)
        ]
        merged = merge_time_series(
            [{"date": r["date"], "rate": r["rate"]} for r in archived_rows],
            [{"date": r["date"], "rate": r["rate"]} for r in live_rows],
            value_key="rate",
            archived_splits=[],
            merged_splits=[],
        )
        out_dates = np.array([_iso_date(r["date"]) for r in merged], dtype="datetime64[D]")
        out_rates = np.array([r["rate"] for r in merged], dtype=float)
        if self._root is not None and out_dates.size > 0 and self._persist:
            self.save_fx_history(currency, out_dates, out_rates)
        return out_dates, out_rates

    def list_archived_tickers(self) -> list[str]:
        if self._root is None:
            return []
        tickers_dir = self._root / "tickers"
        if not tickers_dir.is_dir():
            return []
        return sorted(p.stem for p in tickers_dir.glob("*.json"))

    def refresh_ticker(self, ticker: str) -> None:
        """Fetch, merge, and persist one ticker (no-op when disabled or read-only)."""
        if self._root is None or not self._persist:
            return
        self.resolve_ticker(ticker)

    def refresh_universe(self, tickers: Iterable[str]) -> None:
        seen: set[str] = set()
        for ticker in tickers:
            if ticker in seen:
                continue
            seen.add(ticker)
            self.refresh_ticker(ticker)
        for archived in self.list_archived_tickers():
            if archived not in seen:
                self.refresh_ticker(archived)
