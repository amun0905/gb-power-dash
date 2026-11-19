from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
import responses

from gbdash import pipeline
from gbdash.cli import main
from gbdash.registry import load_registry
from gbdash.sources.elexon import BASE_URL, ElexonClient
from gbdash.storage import Store
from gbdash.transform import SchemaError, normalise

REG = load_registry()
FUEL = REG["generation_fuelinst"]
PRICES = REG["system_prices"]
UTC = timezone.utc


class FakeFuelClient:
    """Mimics FUELINST: 5-min rows for two fuels. `version` bumps publishTime and values
    so we can check that revisions overwrite earlier publications."""

    def __init__(self, version=0):
        self.version = version
        self.calls = []

    def fetch(self, spec, start, end):
        for a, b in ElexonClient.windows(start, end, spec.chunk_days):
            self.calls.append((a, b))
            t = pd.date_range(a, b, freq="5min", inclusive="left", tz="UTC")
            rows = []
            for ts in t:
                for fuel, base in (("WIND", 10000), ("CCGT", 8000)):
                    rows.append({
                        "dataset": "FUELINST",
                        "startTime": ts.isoformat(),
                        "publishTime": (ts + timedelta(minutes=5 + self.version)).isoformat(),
                        "settlementDate": ts.date().isoformat(),
                        "settlementPeriod": ts.hour * 2 + ts.minute // 30 + 1,
                        "fuelType": fuel,
                        "generation": base + self.version,
                        "unexpectedNewField": "ignored",
                    })
            yield pd.DataFrame(rows)


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "data"))


def test_registry_is_valid():
    assert {"generation_fuelinst", "demand_indo", "market_index_mid", "system_prices"} <= set(REG)


def test_normalise_types_and_extra_columns_dropped():
    raw = next(FakeFuelClient().fetch(FUEL, datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 1, 1, 1, tzinfo=UTC)))
    df = normalise(raw, FUEL)
    assert list(df.columns) == list(FUEL.columns)
    assert str(df["startTime"].dtype) == "datetime64[ns, UTC]"
    assert str(df["settlementPeriod"].dtype) == "Int64"
    assert len(df) == 12 * 2


def test_normalise_missing_key_column_fails_loudly():
    with pytest.raises(SchemaError):
        normalise(pd.DataFrame([{"startTime": "2025-01-01T00:00Z", "generation": 1}]), FUEL)


def test_update_bootstraps_then_is_incremental_and_idempotent(store):
    now = datetime(2025, 3, 2, 12, tzinfo=UTC)
    c1 = FakeFuelClient()
    s1 = pipeline.update(FUEL, store, client=c1, now=now)
    assert s1["inserted"] == 7 * 288 * 2  # bootstrap = 7 days of 5-min x 2 fuels
    assert store.partitions(FUEL.name) == ["2025-02", "2025-03"]  # split by month

    # Second run: starts from latest - lookback, not from scratch, and inserts nothing new.
    c2 = FakeFuelClient()
    s2 = pipeline.update(FUEL, store, client=c2, now=now)
    assert s2["inserted"] == 0
    assert c2.calls[0][0] >= now - timedelta(days=FUEL.lookback_days + 1)
    assert len(store.read(FUEL)) == s1["inserted"]


def test_revisions_overwrite_older_publications(store):
    now = datetime(2025, 3, 2, 12, tzinfo=UTC)
    pipeline.update(FUEL, store, client=FakeFuelClient(version=0), now=now)
    pipeline.update(FUEL, store, client=FakeFuelClient(version=3), now=now)
    df = store.read(FUEL)
    recent = df[df.startTime >= now - timedelta(hours=12)]
    old = df[df.startTime < now - timedelta(days=3)]
    assert (recent.loc[recent.fuelType == "WIND", "generation"] == 10003).all()   # revised
    assert (old.loc[old.fuelType == "WIND", "generation"] == 10000).all()         # outside lookback
    assert not df.duplicated(subset=FUEL.keys).any()


def test_backfill_resumes_and_skips_complete_months(store):
    start, end = datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 3, 15, tzinfo=UTC)
    first = pipeline.backfill(FUEL, store, start, end, client=FakeFuelClient())
    assert first["skipped_months"] == 0
    assert store.partitions(FUEL.name) == ["2025-01", "2025-02", "2025-03"]
    second = pipeline.backfill(FUEL, store, start, end, client=FakeFuelClient())
    assert second["skipped_months"] == 2 and second["inserted"] == 0  # Jan, Feb skipped; March refreshed


def test_partial_month_is_not_treated_as_complete(store):
    # Write only one day of January, then backfill: January must be re-fetched.
    pipeline._ingest(FUEL, store, FakeFuelClient(), datetime(2025, 1, 31, tzinfo=UTC), datetime(2025, 2, 1, tzinfo=UTC))
    r = pipeline.backfill(FUEL, store, datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 2, 10, tzinfo=UTC),
                          client=FakeFuelClient())
    assert r["skipped_months"] == 0
    assert store.read(FUEL, "2025-01-01", "2025-02-01")["startTime"].min() == pd.Timestamp("2025-01-01", tz="UTC")


def test_manifest_and_freshness(store):
    now = datetime(2025, 3, 2, 12, tzinfo=UTC)
    pipeline.update(FUEL, store, client=FakeFuelClient(), now=now)
    m = store.manifest(FUEL)
    assert m["rows"] == 7 * 288 * 2 and m["attribution"].startswith("Contains BMRS data")
    assert pipeline.freshness(FUEL, store, now=now)["stale"] is False
    assert pipeline.freshness(FUEL, store, now=now + timedelta(hours=5))["stale"] is True


@responses.activate
def test_elexon_client_settlement_date_mode_and_retry():
    url = f"{BASE_URL}/balancing/settlement/system-prices/2025-01-01"
    responses.add(responses.GET, url, status=503)  # transient failure -> retried
    responses.add(responses.GET, url, json={"data": [
        {"settlementDate": "2025-01-01", "settlementPeriod": 1, "startTime": "2025-01-01T00:00:00Z",
         "systemSellPrice": 80.5, "systemBuyPrice": 80.5, "netImbalanceVolume": -120.0},
    ]})
    from gbdash.http import make_session
    client = ElexonClient(session=make_session(backoff=0))
    frames = list(client.fetch(PRICES, datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 1, 1, 6, tzinfo=UTC)))
    df = normalise(frames[0], PRICES)
    assert df.loc[0, "systemBuyPrice"] == 80.5
    assert responses.calls[-1].request.params["format"] == "json"


@responses.activate
def test_elexon_client_window_params():
    responses.add(responses.GET, f"{BASE_URL}/datasets/FUELINST", json={"data": []})
    client = ElexonClient()
    list(client.fetch(FUEL, datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 1, 3, tzinfo=UTC)))
    assert len(responses.calls) == 2  # chunk_days=1
    p = responses.calls[0].request.params
    assert p["publishDateTimeFrom"] == "2025-01-01T00:00:00Z" and p["publishDateTimeTo"] == "2025-01-02T00:00:00Z"


def test_cli_list_and_status(tmp_path, capsys):
    assert main(["--store", str(tmp_path), "list"]) == 0
    assert "generation_fuelinst" in capsys.readouterr().out
    assert main(["--store", str(tmp_path), "status", "--fail-on-stale"]) == 1  # empty store is stale


def test_config_found_from_working_directory_and_env(tmp_path, monkeypatch):
    from gbdash.registry import find_config
    repo_cfg = find_config()
    # Simulate a non-editable install: package lives elsewhere, CWD is the repo.
    monkeypatch.chdir(repo_cfg.parents[1])
    assert find_config() == repo_cfg.parents[1] / "config" / "datasets.yaml"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GBDASH_CONFIG", str(repo_cfg))
    assert find_config() == repo_cfg


def test_overview_export_is_aligned_and_valid_json(store):
    import json
    from gbdash.export import build_overview, write_overview
    now = datetime(2025, 3, 2, 12, tzinfo=UTC)
    pipeline.update(FUEL, store, client=FakeFuelClient(), now=now)
    d = build_overview(store, REG, now=now)  # other datasets empty: must still work
    n = len(d["t"])
    assert n == 30 * 48
    assert all(len(v) == n for v in d["generation"].values())
    assert all(len(d[k]) == n for k in ("demand", "mid_price", "system_price", "niv"))
    assert d["latest"]["generation"]["Wind"] == 10000 and d["latest"]["system_price"] is None
    write_overview(store, REG, now=now)
    with store.fs.open(f"{store.root}/web/overview.json") as f:
        json.loads(f.read())  # strict JSON (no NaN)
