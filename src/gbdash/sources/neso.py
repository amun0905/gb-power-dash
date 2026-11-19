"""NESO Data Portal (CKAN) client.

The GB day-ahead (N2EX) price file is small, so we download the whole CSV ("dump") and filter
locally. NESO doesn't document the column formats in the API, so times are detected:
  * a full timestamp column, or
  * a date column + a delivery period, where the period is either a number (1..24/25 hourly,
    1..48/50 half-hourly) or a clock time / range like "23:00" or "23:00 - 00:00".
Pin anything via options.* in config/datasets.yaml if detection is ever wrong.
"""
from __future__ import annotations

import io
import logging
import re
from datetime import datetime
from typing import Iterator

import pandas as pd

from ..http import make_session
from ..registry import DatasetSpec

log = logging.getLogger(__name__)

DUMP_URL = "https://api.neso.energy/datastore/dump/{resource_id}"


def _pick(cols: list[str], pattern: str, exclude: str | None = None) -> str | None:
    for c in cols:
        if re.search(pattern, c, re.I) and not (exclude and re.search(exclude, c, re.I)):
            return c
    return None


def _parse_dates(values: pd.Series) -> pd.Series:
    s = values.astype(str).str.strip()
    dayfirst = bool(s.str.match(r"^\d{1,2}/\d{1,2}/\d{4}").any())
    return pd.to_datetime(s, errors="coerce", dayfirst=dayfirst, format="mixed").dt.normalize()


def _looks_like_timestamp(values: pd.Series) -> bool:
    return bool(values.astype(str).str.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{1,2}:\d{2}").mean() > 0.9)


def _period_offsets(values: pd.Series):
    """Return (offset in hours from local midnight, kind) or (None, None)."""
    s = values.astype(str).str.strip()
    if s.str.fullmatch(r"\d+(\.0+)?").all():
        n = pd.to_numeric(s).astype(int)
        half_hourly = n.max() > 25
        base = 1 if n.min() >= 1 else 0
        step = 0.5 if half_hourly else 1.0
        return (n - base) * step, "index"
    m = s.str.extract(r"^(\d{1,2}):(\d{2})")
    if m.notna().all(axis=None):
        return m[0].astype(int) + m[1].astype(int) / 60, "clock"
    return None, None


def to_utc(values: pd.Series, tz_hint: str) -> pd.Series:
    """Parse full timestamps. Values with an offset are respected; naive ones use tz_hint."""
    ts = pd.to_datetime(values, errors="coerce", format="mixed")
    if getattr(ts.dt, "tz", None) is not None:
        return ts.dt.tz_convert("UTC")
    if tz_hint.upper() in ("UTC", "GMT"):
        return ts.dt.tz_localize("UTC")
    return ts.dt.tz_localize(tz_hint, ambiguous="NaT", nonexistent="shift_forward").dt.tz_convert("UTC")


def _from_date_and_period(dates: pd.Series, offsets: pd.Series, kind: str, tz: str) -> pd.Series:
    if tz.upper() in ("UTC", "GMT"):
        return (dates + pd.to_timedelta(offsets, unit="h")).dt.tz_localize("UTC")
    if kind == "index":
        # Count periods from local midnight in absolute time: handles 23/25-hour clock-change days.
        midnight = dates.dt.tz_localize(tz).dt.tz_convert("UTC")
        return midnight + pd.to_timedelta(offsets, unit="h")
    # clock times: the repeated 01:00 on the autumn change day is first BST, then GMT
    local = dates + pd.to_timedelta(offsets, unit="h")
    first = ~pd.DataFrame({"t": local}).duplicated("t", keep="first").to_numpy()
    return local.dt.tz_localize(tz, ambiguous=first, nonexistent="shift_forward").dt.tz_convert("UTC")


def normalise_price_table(raw: pd.DataFrame, options: dict) -> pd.DataFrame:
    """Map an arbitrary NESO price table to columns startTime (UTC) and price."""
    cols = list(raw.columns)
    price_col = options.get("price_column") or _pick(cols, r"price")
    date_col = options.get("date_column") or _pick(cols, r"^date$|delivery.?date|^day$|settlement.?date", r"upload|publish")
    period_col = options.get("period_column") or _pick(cols, r"period|hour", r"upload|publish")
    time_col = options.get("time_column") or _pick(cols, r"date.?time|timestamp|start.?time", r"upload|publish|created")
    explicit_tz = options.get("timezone")

    def fail(msg):
        raise RuntimeError(f"{msg} Columns: {cols}. First rows: {raw.head(3).to_dict('records')}. "
                           "Pin options.time_column / date_column + period_column / price_column / timezone in config/datasets.yaml.")

    if not price_col:
        fail("No price column found.")

    how = None
    if period_col and not _looks_like_timestamp(raw[period_col]) and date_col:
        offsets, kind = _period_offsets(raw[period_col])
        if offsets is not None:
            tz = explicit_tz or ("UTC" if re.search(r"gmt|utc", period_col, re.I) else "Europe/London")
            start = _from_date_and_period(_parse_dates(raw[date_col]), offsets, kind, tz)
            how = f"date '{date_col}' + period '{period_col}' ({kind}), timezone {tz}"
    if how is None:
        tcol = time_col or (period_col if period_col and _looks_like_timestamp(raw[period_col]) else None)
        if not tcol:
            fail("Could not work out the delivery time.")
        tz = explicit_tz or ("UTC" if re.search(r"gmt|utc", tcol, re.I) else "Europe/London")
        start = to_utc(raw[tcol], tz)
        how = f"timestamp '{tcol}', timezone {tz}"

    out = pd.DataFrame({"startTime": start, "price": pd.to_numeric(raw[price_col], errors="coerce")})
    bad = out.startTime.isna().mean()
    if bad > 0.05:
        fail(f"{bad:.0%} of delivery times could not be parsed ({how}).")
    sample = raw.head(2).assign(_utc=out.startTime.head(2)).to_dict("records")
    log.info("NESO price table: %s, price '%s'. Sample: %s", how, price_col, sample)
    return out.dropna(subset=["startTime"])


class NesoClient:
    def __init__(self, session=None, timeout: float = 120):
        self.session = session or make_session()
        self.timeout = timeout
        self._cache: dict[str, pd.DataFrame] = {}

    def _table(self, resource_id: str) -> pd.DataFrame:
        if resource_id not in self._cache:
            resp = self.session.get(DUMP_URL.format(resource_id=resource_id), timeout=self.timeout)
            if resp.status_code >= 400:
                raise RuntimeError(f"NESO dump {resource_id} -> {resp.status_code}: {resp.text[:300]}")
            raw = pd.read_csv(io.BytesIO(resp.content), dtype=str)
            raw = raw.drop(columns=[c for c in raw.columns if c.startswith("_id")], errors="ignore")
            log.info("NESO resource %s: %d rows, columns %s", resource_id, len(raw), list(raw.columns))
            self._cache[resource_id] = raw
        return self._cache[resource_id]

    def fetch(self, spec: DatasetSpec, start: datetime, end: datetime) -> Iterator[pd.DataFrame]:
        raw = self._table(spec.options["resource_id"])
        df = normalise_price_table(raw, spec.options)
        df = df[(df.startTime >= pd.Timestamp(start)) & (df.startTime < pd.Timestamp(end))]
        log.info("%s %s..%s: %d rows", spec.name, start, end, len(df))
        yield df
