"""Backtest: buy 1 MW in the N2EX day-ahead auction for every hour of a block, then exit
either by selling at the Market Index Price (MIP, intraday proxy) or by leaving the position
to settle at the system (imbalance) price. Writes web/strategies.json.

Conventions
  * EFA blocks in UK local time, EFA day starting 23:00 the previous evening:
      1: 23-03, 2: 03-07, 3: 07-11, 4: 11-15, 5: 15-19, 6: 19-23
  * Peak = 07:00-19:00 UK on weekdays (EFA blocks 3-5, Mon-Fri); Baseload = all hours.
  * 1 MW held for a half-hour = 0.5 MWh; P&L per half-hour = 0.5 * (exit price - day-ahead price).
  * Half-hours where either price is missing are skipped and counted against coverage.
  * No fees, bid-ask spread or slippage; MIP is a volume-weighted average, not an executable price.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from .export import _clean
from .registry import DatasetSpec
from .sources import attribution
from .mip import DEFINITION, mip_series
from .storage import Store

log = logging.getLogger(__name__)

TZ = "Europe/London"
BLOCKS = ["EFA 1 (23–03)", "EFA 2 (03–07)", "EFA 3 (07–11)", "EFA 4 (11–15)", "EFA 5 (15–19)", "EFA 6 (19–23)",
          "Peak (07–19 wkdy)", "Baseload"]


def label_half_hours(index: pd.DatetimeIndex) -> pd.DataFrame:
    """For UTC half-hour starts: local hour, EFA block (1-6), EFA date, peak flag."""
    local = index.tz_convert(TZ)
    hour = local.hour
    efa_block = ((hour - 23) % 24) // 4 + 1
    efa_date = (local + pd.Timedelta(hours=1)).normalize().tz_localize(None)  # 23:00 belongs to the next EFA day
    weekday = efa_date.dayofweek < 5
    peak = weekday & (efa_block >= 3) & (efa_block <= 5)
    return pd.DataFrame({"hour": hour, "efa_block": efa_block, "efa_date": efa_date, "peak": peak}, index=index)


def build_frame(store: Store, registry: dict[str, DatasetSpec], start, end) -> pd.DataFrame:
    hh = pd.date_range(start, end, freq="30min", inclusive="left", tz="UTC")
    df = label_half_hours(hh)

    da = store.read(registry["da_gb_n2ex"], start, end)
    da_s = da.set_index("startTime")["price"].sort_index() if not da.empty else pd.Series(dtype=float)
    da_s = da_s.resample("1h").mean() if not da_s.empty else da_s  # in case the source is ever half-hourly
    df["da"] = da_s.reindex(hh.floor("1h")).to_numpy() if not da_s.empty else np.nan

    df["mid"] = mip_series(store.read(registry["market_index_mid"], start, end)).reindex(hh).to_numpy()

    sp = store.read(registry["system_prices"], start, end)
    df["imbalance"] = sp.set_index("startTime")["systemSellPrice"].reindex(hh).to_numpy() if not sp.empty else np.nan
    return df


def daily_pnl(df: pd.DataFrame, exit_col: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Daily P&L (£, 1 MW position), traded MWh and possible MWh per block, indexed by EFA date."""
    ok = df["da"].notna() & df[exit_col].notna()
    pnl = (0.5 * (df[exit_col] - df["da"])).where(ok, 0.0)
    mwh = 0.5 * ok.astype(float)
    possible = pd.Series(0.5, index=df.index)

    masks = {BLOCKS[i]: df["efa_block"] == i + 1 for i in range(6)}
    masks["Peak (07–19 wkdy)"] = df["peak"]
    masks["Baseload"] = pd.Series(True, index=df.index)

    out = {}
    for name, series in (("pnl", pnl), ("mwh", mwh), ("possible", possible)):
        cols = {b: series.where(m, 0.0).groupby(df["efa_date"]).sum() for b, m in masks.items()}
        out[name] = pd.DataFrame(cols)[BLOCKS]
    return out["pnl"], out["mwh"], out["possible"]


def heatmap(df: pd.DataFrame, exit_col: str) -> dict:
    """Average (exit - day-ahead) £/MWh by UK hour of day x month."""
    ok = df["da"].notna() & df[exit_col].notna()
    d = df[ok].assign(spread=lambda x: x[exit_col] - x["da"],
                      month=lambda x: x.index.tz_convert(TZ).strftime("%Y-%m"))
    if d.empty:
        return {"months": [], "hours": list(range(24)), "values": []}
    pivot = d.pivot_table(index="hour", columns="month", values="spread", aggfunc="mean").reindex(range(24))
    return {"months": list(pivot.columns), "hours": list(range(24)),
            "values": [_clean(pivot[m]) for m in pivot.columns]}  # values[monthIdx][hour]


def build_strategies(store: Store, registry: dict[str, DatasetSpec], now: datetime | None = None) -> dict:
    now = pd.Timestamp(now or datetime.now(timezone.utc)).tz_convert("UTC")
    start = pd.Timestamp(registry["da_gb_n2ex"].start, tz=TZ).tz_convert("UTC")
    end = now.floor("30min")  # only periods that have happened (imbalance prices need settlement)
    df = build_frame(store, registry, start, end)

    modes = {}
    dates = None
    for mode, col in (("mid", "mid"), ("imbalance", "imbalance")):
        pnl, mwh, possible = daily_pnl(df, col)
        # drop leading days before both price series exist, and the current (incomplete) EFA day
        traded_any = mwh["Baseload"] > 0
        if traded_any.any():
            first = traded_any.idxmax()
            pnl, mwh, possible = pnl.loc[first:], mwh.loc[first:], possible.loc[first:]
        if dates is None:
            today_efa = label_half_hours(pd.DatetimeIndex([end]))["efa_date"].iloc[0]
            dates = pnl.index[pnl.index < today_efa]  # current EFA day is still incomplete
        pnl, mwh, possible = pnl.reindex(dates, fill_value=0), mwh.reindex(dates, fill_value=0), possible.reindex(dates, fill_value=0)
        modes[mode] = {
            "pnl": {b: _clean(pnl[b]) for b in BLOCKS},
            "mwh": {b: _clean(mwh[b]) for b in BLOCKS},
            "heatmap": heatmap(df, col),
        }
    possible_out = {b: _clean(possible[b]) for b in BLOCKS} if dates is not None else {}

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "blocks": BLOCKS,
        "dates": [d.strftime("%Y-%m-%d") for d in (dates if dates is not None else [])],
        "possible_mwh": possible_out,
        "modes": modes,
        "attribution": [attribution("neso"), attribution("elexon")],
        "notes": [
            "Long 1 MW in the N2EX day-ahead auction for every hour of the block, closed out at the exit price.",
            "MIP exit: sold at the Market Index Price, an intraday proxy rather than an executable price. " + DEFINITION,
            "Imbalance exit: position left open and settled at the system price.",
            "No fees, bid-ask spread or slippage. Half-hours missing either price are skipped (see coverage).",
            "EFA blocks and days in UK time; an EFA day starts at 23:00 the evening before.",
        ],
    }


def write_strategies(store: Store, registry: dict[str, DatasetSpec], now: datetime | None = None) -> dict:
    data = build_strategies(store, registry, now)
    store.write_json(f"{store.root}/web/strategies.json", data, compact=True)
    log.info("wrote web/strategies.json (%d days)", len(data["dates"]))
    return data
