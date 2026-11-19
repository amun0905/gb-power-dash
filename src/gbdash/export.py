"""Build the compact JSON the website reads (web/overview.json).

The website never touches the raw Parquet: after each update we pre-aggregate the last
N days to half-hourly arrays aligned on one time axis, which keeps the page fast and simple.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

import pandas as pd

from .registry import DatasetSpec
from .sources.elexon import ATTRIBUTION
from .mip import DEFINITION, mip_series
from .storage import Store

log = logging.getLogger(__name__)

WINDOW_DAYS = 30

# FUELINST fuel codes -> display groups (stack order, bottom to top)
GEN_GROUPS: dict[str, list[str]] = {
    "Nuclear": ["NUCLEAR"],
    "Biomass": ["BIOMASS"],
    "Hydro": ["NPSHYD"],
    "Wind": ["WIND"],
    "Gas": ["CCGT", "OCGT"],
    "Pumped storage": ["PS"],
    "Coal, oil & other": ["COAL", "OIL", "OTHER"],
}

# Interconnector codes -> counterpart market. Positive = import into GB.
IC_GROUPS: dict[str, list[str]] = {
    "France": ["INTFR", "INTIFA2", "INTELEC"],
    "Belgium": ["INTNEM"],
    "Netherlands": ["INTNED"],
    "Norway": ["INTNSL"],
    "Denmark": ["INTVKL"],
    "Ireland & NI": ["INTIRL", "INTEW", "INTGRNL"],
}



def _code_to_group(groups: dict[str, list[str]]) -> dict[str, str]:
    return {code: g for g, codes in groups.items() for code in codes}


def _clean(values) -> list:
    """Floats rounded for size; NaN -> null so the JSON stays valid."""
    out = []
    for v in values:
        if v is None or (isinstance(v, float) and math.isnan(v)) or pd.isna(v):
            out.append(None)
        else:
            out.append(round(float(v), 1))
    return out


def _pivot_groups(df: pd.DataFrame, groups: dict[str, list[str]], index: pd.DatetimeIndex,
                  extra_group: str | None = None) -> dict[str, list]:
    if df.empty:
        return {g: [None] * len(index) for g in groups}
    mapping = _code_to_group(groups)
    df = df.assign(group=df["fuelType"].map(mapping))
    if extra_group:
        df["group"] = df["group"].fillna(extra_group)
    df = df.dropna(subset=["group"])
    # Sum fuels within a group per 5-min stamp, then average to half-hours.
    per_stamp = df.groupby(["startTime", "group"])["generation"].sum().unstack("group")
    hh = per_stamp.resample("30min").mean().reindex(index)
    names = list(groups)
    if extra_group and extra_group not in groups and extra_group in hh.columns:
        names.append(extra_group)
    return {g: _clean(hh[g]) if g in hh.columns else [None] * len(index) for g in names}


def build_overview(store: Store, registry: dict[str, DatasetSpec], now: datetime | None = None,
                   days: int = WINDOW_DAYS) -> dict:
    now = now or datetime.now(timezone.utc)
    end = pd.Timestamp(now).tz_convert("UTC").ceil("30min")
    start = end - pd.Timedelta(days=days)
    index = pd.date_range(start, end, freq="30min", inclusive="left", tz="UTC")

    fuel = store.read(registry["generation_fuelinst"], start, end)
    is_ic = fuel["fuelType"].str.startswith("INT") if not fuel.empty else pd.Series(dtype=bool)
    gen_df, ic_df = (fuel[~is_ic], fuel[is_ic]) if not fuel.empty else (fuel, fuel)

    demand = store.read(registry["demand_indo"], start, end)
    demand_hh = (demand.set_index("startTime")["demand"].reindex(index) if not demand.empty
                 else pd.Series(index=index, dtype=float))

    mid_hh = mip_series(store.read(registry["market_index_mid"], start, end)).reindex(index)

    sp = store.read(registry["system_prices"], start, end)
    sp = sp.set_index("startTime").reindex(index) if not sp.empty else pd.DataFrame(index=index, columns=["systemBuyPrice", "netImbalanceVolume"])

    latest = {}
    if not fuel.empty:
        t_last = fuel["startTime"].max()
        snap = fuel[fuel["startTime"] == t_last]
        gmap, imap = _code_to_group(GEN_GROUPS), _code_to_group(IC_GROUPS)
        mix = snap[~snap.fuelType.str.startswith("INT")].assign(g=lambda d: d.fuelType.map(gmap).fillna("Coal, oil & other"))
        ics = snap[snap.fuelType.str.startswith("INT")].assign(g=lambda d: d.fuelType.map(imap).fillna("Other"))
        latest = {
            "time": t_last.isoformat(),
            "generation": {k: round(float(v), 1) for k, v in mix.groupby("g")["generation"].sum().items()},
            "interconnectors": {k: round(float(v), 1) for k, v in ics.groupby("g")["generation"].sum().items()},
        }

    def last_valid(s: pd.Series):
        s = s.dropna()
        return None if s.empty else {"time": s.index[-1].isoformat(), "value": round(float(s.iloc[-1]), 2)}

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "attribution": ATTRIBUTION,
        "resolution": "30min",
        "t": [int(ts.timestamp() * 1000) for ts in index],
        "generation": _pivot_groups(gen_df, GEN_GROUPS, index, extra_group="Coal, oil & other"),
        "interconnectors": _pivot_groups(ic_df, IC_GROUPS, index, extra_group="Other"),
        "demand": _clean(demand_hh),
        "mid_price": _clean(mid_hh),
        "system_price": _clean(sp["systemBuyPrice"]),
        "niv": _clean(sp["netImbalanceVolume"]),
        "latest": {
            **latest,
            "demand": last_valid(demand_hh),
            "mid_price": last_valid(mid_hh),
            "system_price": last_valid(pd.to_numeric(sp["systemBuyPrice"], errors="coerce")),
            "niv": last_valid(pd.to_numeric(sp["netImbalanceVolume"], errors="coerce")),
        },
        "notes": [
            "Generation is transmission-metered (FUELINST); embedded solar and small wind are not included.",
            "Interconnector flows: positive = import into GB, negative = export.",
            DEFINITION,
        ],
    }
    # Unmapped fuels would silently vanish from the stack; make that visible in the logs.
    if not gen_df.empty:
        unknown = sorted(set(gen_df.fuelType) - set(_code_to_group(GEN_GROUPS)))
        if unknown:
            log.warning("Unmapped fuel types (counted in 'Coal, oil & other'): %s", unknown)
    return out


def write_overview(store: Store, registry: dict[str, DatasetSpec], now: datetime | None = None) -> dict:
    data = build_overview(store, registry, now)
    store.write_json(f"{store.root}/web/overview.json", data, compact=True)
    log.info("wrote web/overview.json (%d half-hours)", len(data["t"]))
    return data
