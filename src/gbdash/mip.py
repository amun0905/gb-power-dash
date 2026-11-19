"""Market Index Price (MIP), calculated from Elexon's Market Index Data (MID).

Elexon doesn't publish the MIP as its own series in the Insights API, so we apply the BSC rule:
for each settlement period, the MIP is the volume-weighted average of the prices submitted by all
Market Index Data Providers with volume above zero (providers report zero below the liquidity
threshold). In practice EPEX SPOT (APXMIDP) carries the volume and N2EX (N2EXMIDP) reports zero,
so MIP equals EPEX's price, but this stays correct if that changes.
"""
from __future__ import annotations

import pandas as pd

LABEL = "Market Index Price (MIP)"
DEFINITION = ("MIP = Market Index Price, calculated as in the BSC: the volume-weighted average of all Market Index "
              "Data providers' prices for each half-hour (in practice EPEX SPOT; N2EX reports no volume).")


def mip_series(mid: pd.DataFrame) -> pd.Series:
    """Half-hourly MIP indexed by startTime (UTC). Periods with no traded volume are absent."""
    if mid is None or mid.empty:
        return pd.Series(dtype=float, name="mip")
    m = mid[(mid["volume"] > 0) & mid["price"].notna()]
    if m.empty:
        return pd.Series(dtype=float, name="mip")
    g = m.assign(pv=m["price"] * m["volume"]).groupby("startTime")[["pv", "volume"]].sum()
    return (g["pv"] / g["volume"]).rename("mip").sort_index()
