import json
from datetime import datetime, timezone

import pandas as pd
import pytest
import responses

from gbdash.registry import load_registry
from gbdash.sources import SourceNotConfigured
from gbdash.sources.ecb import URL as ECB_URL, EcbClient
from gbdash.sources.entsoe import EntsoeClient, parse_a44
from gbdash.sources.neso import normalise_price_table
from gbdash.storage import Store
from gbdash.transform import normalise

REG = load_registry()
UTC = timezone.utc

A44 = """<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">
  <TimeSeries><currency_Unit.name>EUR</currency_Unit.name><curveType>A03</curveType>
    <Period><timeInterval><start>2026-11-18T23:00Z</start><end>2026-11-19T00:00Z</end></timeInterval>
      <resolution>PT15M</resolution>
      <Point><position>1</position><price.amount>80.0</price.amount></Point>
      <Point><position>3</position><price.amount>90.5</price.amount></Point>
    </Period></TimeSeries>
  <TimeSeries><currency_Unit.name>EUR</currency_Unit.name>
    <Period><timeInterval><start>2026-11-18T23:00Z</start><end>2026-11-19T01:00Z</end></timeInterval>
      <resolution>PT60M</resolution>
      <Point><position>1</position><price.amount>999</price.amount></Point>
      <Point><position>2</position><price.amount>70</price.amount></Point>
    </Period></TimeSeries>
</Publication_MarketDocument>"""

ACK = """<Acknowledgement_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-1:acknowledgementdocument:7:0">
<Reason><code>999</code><text>No matching data found for Data item Energy Prices</text></Reason></Acknowledgement_MarketDocument>"""


def test_a44_fills_a03_gaps_and_prefers_finer_resolution():
    df = parse_a44(A44, "FR")
    first_hour = df[df.startTime < pd.Timestamp("2026-11-19T00:00Z")]
    assert list(first_hour.price) == [80.0, 80.0, 90.5, 90.5]          # positions 2 & 4 carried forward
    assert (first_hour.resolution_min == 15).all()                       # 60-min '999' discarded
    assert df[df.startTime == pd.Timestamp("2026-11-19T00:00Z")].price.item() == 70


def test_a44_no_data_is_empty():
    assert parse_a44(ACK, "FR").empty


def test_entsoe_without_token_is_skipped(monkeypatch):
    monkeypatch.delenv("ENTSOE_API_TOKEN", raising=False)
    with pytest.raises(SourceNotConfigured):
        next(EntsoeClient().fetch(REG["da_europe"], datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 2, tzinfo=UTC)))


def test_neso_detects_datetime_column_and_london_time():
    raw = pd.DataFrame({"Delivery Date Time": ["2026-07-01 00:00", "2026-07-01 01:00"], "Price (GBP/MWh)": ["70", "72"]})
    df = normalise_price_table(raw, {})
    assert df.startTime.iloc[0] == pd.Timestamp("2026-06-30T23:00Z")  # BST -> UTC
    raw_gmt = raw.rename(columns={"Delivery Date Time": "Start Time (GMT)"})
    assert normalise_price_table(raw_gmt, {}).startTime.iloc[0] == pd.Timestamp("2026-07-01T00:00Z")


def test_neso_date_plus_delivery_period_numbers_handles_clock_change():
    # 26 Oct 2025: UK clocks go back, 25 hourly periods
    raw = pd.DataFrame({"Date": ["2025-10-26"] * 25 + ["2025-10-27"], "Delivery Period": [str(i) for i in range(1, 26)] + ["1"],
                        "Price": ["50"] * 26})
    df = normalise_price_table(raw, {})
    assert df.startTime.iloc[0] == pd.Timestamp("2025-10-25T23:00Z")     # 00:00 BST
    assert df.startTime.iloc[24] == pd.Timestamp("2025-10-26T23:00Z")    # last hour of the long day
    assert df.startTime.iloc[25] == pd.Timestamp("2025-10-27T00:00Z")    # next day 00:00 GMT
    assert df.startTime.is_unique


def test_neso_date_plus_clock_range_dayfirst():
    raw = pd.DataFrame({"Date": ["01/07/2026", "01/07/2026"], "Delivery Period": ["00:00 - 01:00", "01:00 - 02:00"], "Price": ["70", "71"]})
    df = normalise_price_table(raw, {})
    assert list(df.startTime) == [pd.Timestamp("2026-06-30T23:00Z"), pd.Timestamp("2026-07-01T00:00Z")]


def test_entsoe_bom_is_tolerated():
    assert not parse_a44(b"\xef\xbb\xbf" + A44.encode(), "FR").empty


def test_neso_date_plus_hour_ending():
    raw = pd.DataFrame({"Date": ["2026-01-05", "2026-01-05"], "Hour": ["1", "2"], "Price": ["60", "61"]})
    df = normalise_price_table(raw, {})
    assert list(df.startTime) == [pd.Timestamp("2026-01-05T00:00Z"), pd.Timestamp("2026-01-05T01:00Z")]


def test_neso_unknown_columns_error_lists_them():
    with pytest.raises(RuntimeError, match="Columns"):
        normalise_price_table(pd.DataFrame({"a": [1], "b": [2]}), {})


@responses.activate
def test_ecb_csv():
    responses.add(responses.GET, ECB_URL, body="KEY,TIME_PERIOD,OBS_VALUE\nEXR.D.GBP.EUR.SP00.A,2026-11-18,0.8421\n")
    df = normalise(next(EcbClient().fetch(REG["fx_eurgbp"], datetime(2026, 11, 18, tzinfo=UTC), datetime(2026, 11, 19, tzinfo=UTC))), REG["fx_eurgbp"])
    assert df.rate.item() == 0.8421


def test_prices_export_converts_eur_and_aligns(tmp_path):
    from gbdash.export_prices import build_prices
    store = Store(str(tmp_path))
    now = datetime(2026, 11, 19, 12, tzinfo=UTC)
    hrs = pd.date_range("2026-11-18", "2026-11-21", freq="1h", inclusive="left", tz="UTC")
    store.upsert(REG["da_gb_n2ex"], normalise(pd.DataFrame({"startTime": hrs, "price": 100.0}), REG["da_gb_n2ex"]))
    q = pd.date_range("2026-11-18", "2026-11-21", freq="15min", inclusive="left", tz="UTC")
    store.upsert(REG["da_europe"], normalise(pd.DataFrame({"startTime": q, "zone": "FR", "price": 100.0, "currency": "EUR", "resolution_min": 15}), REG["da_europe"]))
    store.upsert(REG["fx_eurgbp"], normalise(pd.DataFrame({"startTime": [pd.Timestamp("2026-11-18", tz="UTC")], "rate": [0.85]}), REG["fx_eurgbp"]))
    d = build_prices(store, REG, now)
    n = len(d["t"])
    assert all(len(v) == n for v in [d["gb_da"], d["mid_price"], d["system_price"], *d["neighbours"].values()])
    i = d["t"].index(int(pd.Timestamp("2026-11-20T10:00Z").timestamp() * 1000))  # tomorrow, rate carried forward
    assert d["neighbours"]["France"][i] == 85.0 and d["gb_da"][i] == 100.0
    json.dumps(d, allow_nan=False)


@responses.activate
def test_entsoe_requests_xml(monkeypatch):
    from gbdash.sources.entsoe import BASE_URL
    monkeypatch.setenv("ENTSOE_API_TOKEN", "t")
    responses.add(responses.GET, BASE_URL, body=A44, content_type="text/xml")
    spec = REG["da_europe"]
    from dataclasses import replace
    spec = replace(spec, options={"zones": ["FR"]})
    df = next(EntsoeClient().fetch(spec, datetime(2026, 11, 18, 23, tzinfo=UTC), datetime(2026, 11, 19, 1, tzinfo=UTC)))
    assert "xml" in responses.calls[0].request.headers["Accept"] and not df.empty


@responses.activate
def test_entsoe_skips_zone_on_gateway_timeout(monkeypatch):
    from dataclasses import replace
    from gbdash.http import make_session
    from gbdash.sources.entsoe import BASE_URL
    monkeypatch.setenv("ENTSOE_API_TOKEN", "t")
    timeout_body = '{"uuAppErrorMap":{"uu-gateway-router/connectTimeout":{"message":"Unable to access service within time limit."}}}'

    def reply(request):
        if "10YFR-RTE------C" in request.url:
            return (599, {}, timeout_body)
        return (200, {"Content-Type": "text/xml"}, A44)
    responses.add_callback(responses.GET, BASE_URL, callback=reply)
    spec = replace(REG["da_europe"], options={"zones": ["FR", "BE"]})
    client = EntsoeClient(session=make_session(total_retries=1, backoff=0))
    frames = list(client.fetch(spec, datetime(2026, 11, 18, 23, tzinfo=UTC), datetime(2026, 11, 19, 1, tzinfo=UTC)))
    assert len(frames) == 1 and set(frames[0].zone) == {"BE"}   # FR skipped, BE still fetched


@responses.activate
def test_entsoe_all_zones_down_is_an_error(monkeypatch):
    from dataclasses import replace
    from gbdash.http import make_session
    from gbdash.sources.entsoe import BASE_URL
    monkeypatch.setenv("ENTSOE_API_TOKEN", "t")
    responses.add(responses.GET, BASE_URL, status=599, body="{}")
    spec = replace(REG["da_europe"], options={"zones": ["FR", "BE"]})
    client = EntsoeClient(session=make_session(total_retries=0, backoff=0))
    with pytest.raises(RuntimeError, match="every zone"):
        list(client.fetch(spec, datetime(2026, 11, 18, 23, tzinfo=UTC), datetime(2026, 11, 19, 1, tzinfo=UTC)))
