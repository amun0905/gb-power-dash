from datetime import datetime, timezone

import pandas as pd
import pytest

from gbdash.export_strategies import BLOCKS, build_strategies, daily_pnl, label_half_hours
from gbdash.registry import load_registry
from gbdash.storage import Store
from gbdash.transform import normalise

REG = load_registry()
UTC = timezone.utc


def test_efa_labels_winter_and_summer():
    idx = pd.DatetimeIndex(["2026-01-14T23:00Z", "2026-01-15T03:30Z", "2026-01-15T07:00Z", "2026-07-14T22:00Z"])
    lab = label_half_hours(idx)
    assert list(lab.efa_block) == [1, 2, 3, 1]                       # 22:00 UTC in July = 23:00 BST -> EFA 1
    assert lab.efa_date.iloc[0] == pd.Timestamp("2026-01-15")         # 23:00 belongs to next EFA day
    assert lab.efa_date.iloc[3] == pd.Timestamp("2026-07-15")
    assert bool(lab.peak.iloc[2]) is True                             # Thu 07:00 is peak


def _store(tmp_path, days=40):
    store = Store(str(tmp_path))
    start = pd.Timestamp("2026-01-01", tz="Europe/London").tz_convert("UTC")
    hrs = pd.date_range(start, periods=24 * days, freq="1h", tz="UTC")
    hh = pd.date_range(start, periods=48 * days, freq="30min", tz="UTC")
    store.upsert(REG["da_gb_n2ex"], normalise(pd.DataFrame({"startTime": hrs, "price": 100.0}), REG["da_gb_n2ex"]))
    # MID 2 above day-ahead; one missing half-hour (volume 0) to test coverage
    vol = pd.Series(10.0, index=range(len(hh))); vol.iloc[5] = 0
    store.upsert(REG["market_index_mid"], normalise(pd.DataFrame({
        "startTime": hh, "settlementDate": hh.date, "settlementPeriod": 1, "dataProvider": "APXMIDP",
        "price": 102.0, "volume": vol}), REG["market_index_mid"]))
    sp = normalise(pd.DataFrame({"startTime": hh, "settlementDate": hh.date, "settlementPeriod": range(len(hh)),
                                 "systemSellPrice": 95.0, "systemBuyPrice": 95.0, "netImbalanceVolume": 0.0}), REG["system_prices"])
    store.upsert(REG["system_prices"], sp)
    return store


def test_pnl_math_and_coverage(tmp_path):
    reg = dict(REG)
    from dataclasses import replace
    reg["da_gb_n2ex"] = replace(REG["da_gb_n2ex"], start=pd.Timestamp("2026-01-01").date())
    store = _store(tmp_path)
    d = build_strategies(store, reg, now=datetime(2026, 2, 5, 12, tzinfo=UTC))
    mid, imb = d["modes"]["mid"], d["modes"]["imbalance"]
    n = len(d["dates"])
    assert all(len(v) == n for v in mid["pnl"].values())
    i = 3  # a full day
    assert mid["pnl"]["EFA 3 (07–11)"][i] == pytest.approx(8.0)       # 4 MWh x £2
    assert imb["pnl"]["Baseload"][i] == pytest.approx(-120.0)          # 24 MWh x -£5
    mi = d["modes"]["mid_imbalance"]
    assert mi["pnl"]["EFA 3 (07–11)"][i] == pytest.approx(-28.0)      # 4 MWh x (95 - 102)
    # legs add up: (imb - da) = (mid - da) + (imb - mid)
    for b in d["blocks"]:
        assert imb["pnl"][b][i] == pytest.approx(mid["pnl"][b][i] + mi["pnl"][b][i])
    assert d["possible_mwh"]["Baseload"][i] == 24
    # first EFA day starts 23:00 on 31 Dec, before the data (-1 MWh), and misses one MID half-hour (-0.5)
    assert mid["mwh"]["Baseload"][0] == pytest.approx(22.5)
    assert d["dates"][-1] < "2026-02-05"                                # current EFA day excluded
    hm = mid["heatmap"]
    assert len(hm["values"][0]) == 24 and hm["values"][0][10] == pytest.approx(2.0)


def test_peak_is_weekdays_only(tmp_path):
    idx = pd.date_range("2026-01-09T23:00Z", periods=96, freq="30min", tz="UTC")  # EFA Sat 10 Jan + Sun 11 Jan
    df = label_half_hours(idx).assign(da=100.0, mid=110.0)
    pnl, mwh, _ = daily_pnl(df, "mid")
    assert (mwh["Peak (07–19 wkdy)"] == 0).all() and (mwh["Baseload"] == 24).all()
