"""Check whether the N2EX day-ahead series is aligned in time with the Market Index price.

For summer (BST) and winter (GMT) separately, shifts the day-ahead series by -2..+2 hours and
reports how closely it tracks the hourly-averaged MID at each shift. The best match should be at
shift 0 in both seasons; a best match at +/-1 h in one season only points to a timezone mix-up.

Usage (from the repo folder):
    python scripts/check_da_alignment.py [--store ./data]
"""
from __future__ import annotations

import argparse
import os

import pandas as pd

from gbdash.registry import load_registry
from gbdash.storage import Store


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default=os.getenv("GBDASH_STORE", "./data"))
    a = ap.parse_args()
    reg, store = load_registry(), Store(a.store)

    da = store.read(reg["da_gb_n2ex"]).set_index("startTime")["price"].sort_index()
    mid = store.read(reg["market_index_mid"])
    mid = mid[(mid.dataProvider == "APXMIDP") & (mid.volume > 0)].set_index("startTime")["price"].sort_index()
    mid_h = mid.resample("1h").mean()
    if da.empty or mid_h.empty:
        raise SystemExit("Need both da_gb_n2ex and market_index_mid in the store (run gbdash backfill first).")

    bst = da.index.tz_convert("Europe/London").map(lambda t: bool(t.dst()))
    print(f"day-ahead rows: {len(da)}  ({da.index.min()} .. {da.index.max()})")
    print(f"MID hours:      {mid_h.notna().sum()}")
    print("\nshift = hours added to the day-ahead timestamps; lower error / higher corr = better fit\n")
    for season, mask in (("Summer (BST)", bst), ("Winter (GMT)", ~bst)):
        sub = da[mask]
        if len(sub) < 48:
            print(f"{season}: not enough data\n")
            continue
        print(f"{season}: {len(sub)} hours")
        rows = []
        for shift in range(-2, 3):
            shifted = sub.copy()
            shifted.index = shifted.index + pd.Timedelta(hours=shift)
            j = pd.concat([shifted.rename("da"), mid_h.rename("mid")], axis=1, join="inner").dropna()
            rows.append((shift, (j.da - j.mid).abs().mean(), j.da.corr(j.mid), len(j)))
        best = min(rows, key=lambda r: r[1])[0]
        for shift, mae, corr, n in rows:
            mark = "  <- best" if shift == best else ""
            print(f"  shift {shift:+d} h: mean |DA - MID| = {mae:6.2f} £/MWh, corr = {corr:.3f}, n = {n}{mark}")
        print()


if __name__ == "__main__":
    main()
