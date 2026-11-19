"""Build web/prices.json for the Prices tab: hourly GB day-ahead vs short-term prices, and
neighbouring day-ahead prices converted to £/MWh with the ECB EUR/GBP rate."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd

from .export import _clean
from .registry import DatasetSpec
from .sources import attribution
from .storage import Store

log = logging.getLogger(__name__)

PAST_DAYS = 30
FUTURE_DAYS = 2
MID_PROVIDER = "APXMIDP"

# ENTSO-E zone -> display name; the first six match the interconnector counterparts on the overview
NEIGHBOURS = {
    "FR": "France", "BE": "Belgium", "NL": "Netherlands", "NO2": "Norway (NO2)",
    "DK1": "Denmark (DK1)", "IE_SEM": "Ireland (SEM)", "DE_LU": "Germany",
}


def _hourly(s: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    if s.empty:
        return pd.Series(index=index, dtype=float)
    return s.sort_index().resample("1h").mean().reindex(index)


def build_prices(store: Store, registry: dict[str, DatasetSpec], now: datetime | None = None) -> dict:
    now = pd.Timestamp(now or datetime.now(timezone.utc)).tz_convert("UTC")
    start = now.floor("1h") - pd.Timedelta(days=PAST_DAYS)
    end = (now + pd.Timedelta(days=FUTURE_DAYS)).normalize() + pd.Timedelta(days=1)
    index = pd.date_range(start, end, freq="1h", inclusive="left", tz="UTC")

    def read(name):
        return store.read(registry[name], start, end) if name in registry else pd.DataFrame()

    gb = read("da_gb_n2ex")
    gb_h = _hourly(gb.set_index("startTime")["price"] if not gb.empty else pd.Series(dtype=float), index)

    mid = read("market_index_mid")
    if not mid.empty:
        mid = mid[(mid.dataProvider == MID_PROVIDER) & (mid.volume > 0)]
    mid_h = _hourly(mid.set_index("startTime")["price"] if not mid.empty else pd.Series(dtype=float), index)

    sp = read("system_prices")
    sp_h = _hourly(sp.set_index("startTime")["systemBuyPrice"] if not sp.empty else pd.Series(dtype=float), index)

    # EUR -> GBP: rate per calendar day, carried forward over weekends/holidays and into tomorrow
    fx = store.read(registry["fx_eurgbp"], start - pd.Timedelta(days=10), end) if "fx_eurgbp" in registry else pd.DataFrame()
    fx_daily = (fx.set_index("startTime")["rate"].sort_index() if not fx.empty else pd.Series(dtype=float))
    fx_h = fx_daily.reindex(fx_daily.index.union(index)).ffill().reindex(index) if not fx_daily.empty else pd.Series(index=index, dtype=float)

    eu = read("da_europe")
    neighbours: dict[str, list] = {}
    for zone, name in NEIGHBOURS.items():
        z = eu[eu.zone == zone] if not eu.empty else eu
        if z.empty:
            continue
        eur = _hourly(z.set_index("startTime")["price"], index)
        is_gbp = (z["currency"] == "GBP").all()
        neighbours[name] = _clean(eur if is_gbp else eur * fx_h)

    last_fx = fx_daily.dropna()
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "now": int(now.timestamp() * 1000),
        "resolution": "1h",
        "t": [int(ts.timestamp() * 1000) for ts in index],
        "gb_da": _clean(gb_h),
        "mid_price": _clean(mid_h),
        "system_price": _clean(sp_h),
        "neighbours": neighbours,
        "fx": None if last_fx.empty else {"date": last_fx.index[-1].date().isoformat(), "gbp_per_eur": round(float(last_fx.iloc[-1]), 5)},
        "attribution": [a for a in (attribution("neso"), attribution("entsoe"), attribution("ecb"), attribution("elexon")) if a],
        "notes": [
            "All prices in £/MWh, hourly averages. Continental prices converted from EUR at the latest ECB reference rate for each day.",
            "GB day-ahead = Nord Pool N2EX hourly auction. Market Index and system price are hourly averages of half-hourly values. Market Index (EPEX) = Elexon's Market Index Data from the EPEX SPOT provider (formerly APX): the volume-weighted price of short-term exchange trades for each half-hour.",
            "SDAC zones clear in 15-minute periods; these are averaged to hourly.",
        ],
    }
    return out


def write_prices(store: Store, registry: dict[str, DatasetSpec], now: datetime | None = None) -> dict:
    data = build_prices(store, registry, now)
    store.write_json(f"{store.root}/web/prices.json", data, compact=True)
    log.info("wrote web/prices.json (%d hours, %d neighbours)", len(data["t"]), len(data["neighbours"]))
    return data
