"""European Central Bank reference exchange rate (GBP per 1 EUR), daily on TARGET business days."""
from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import Iterator

import pandas as pd

from ..http import make_session
from ..registry import DatasetSpec

log = logging.getLogger(__name__)

URL = "https://data-api.ecb.europa.eu/service/data/EXR/D.GBP.EUR.SP00.A"


class EcbClient:
    def __init__(self, session=None, timeout: float = 60):
        self.session = session or make_session()
        self.timeout = timeout

    def fetch(self, spec: DatasetSpec, start: datetime, end: datetime) -> Iterator[pd.DataFrame]:
        params = {"startPeriod": start.date().isoformat(), "endPeriod": end.date().isoformat(), "format": "csvdata"}
        resp = self.session.get(URL, params=params, timeout=self.timeout, headers={"Accept": "text/csv"})
        if resp.status_code == 404 or not resp.text.strip():  # no observations in range (e.g. a weekend)
            yield pd.DataFrame()
            return
        resp.raise_for_status()
        raw = pd.read_csv(io.StringIO(resp.text))
        missing = {"TIME_PERIOD", "OBS_VALUE"} - set(raw.columns)
        if missing:
            raise RuntimeError(f"ECB CSV missing {missing}; got {list(raw.columns)}")
        df = pd.DataFrame({"startTime": pd.to_datetime(raw["TIME_PERIOD"]).dt.tz_localize("UTC"),
                           "rate": raw["OBS_VALUE"]})
        log.info("%s: %d rates", spec.name, len(df))
        yield df
