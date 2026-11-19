"""Elexon Insights Solution API client.

Docs: https://developer.data.elexon.co.uk  (no API key required)
Licence: BMRS Data Licence -- attribute "Contains BMRS data (c) Elexon Limited copyright and database right".
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Iterator

import pandas as pd

from ..http import make_session
from ..registry import DatasetSpec

log = logging.getLogger(__name__)

BASE_URL = "https://data.elexon.co.uk/bmrs/api/v1"
ATTRIBUTION = "Contains BMRS data © Elexon Limited copyright and database right"


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rows(payload) -> list[dict]:
    """Elexon returns either {"data": [...]} or a bare list depending on endpoint."""
    if isinstance(payload, dict):
        return payload.get("data") or []
    if isinstance(payload, list):
        return payload
    raise ValueError(f"Unexpected payload type {type(payload).__name__}")


class ElexonClient:
    def __init__(self, base_url: str = BASE_URL, session=None, timeout: float = 60):
        self.base_url = base_url.rstrip("/")
        self.session = session or make_session()
        self.timeout = timeout

    def _get(self, path: str, params: dict) -> list[dict]:
        url = f"{self.base_url}{path}"
        resp = self.session.get(url, params={**params, "format": "json"}, timeout=self.timeout)
        if resp.status_code >= 400:
            # Surface Elexon's error body: it usually says exactly which parameter is wrong.
            raise RuntimeError(f"GET {resp.url} -> {resp.status_code}: {resp.text[:500]}")
        return _rows(resp.json())

    # --- request planning -------------------------------------------------
    @staticmethod
    def windows(start: datetime, end: datetime, chunk_days: int) -> Iterator[tuple[datetime, datetime]]:
        step = timedelta(days=chunk_days)
        cur = start
        while cur < end:
            nxt = min(cur + step, end)
            yield cur, nxt
            cur = nxt

    @staticmethod
    def dates(start: datetime, end: datetime) -> Iterator[date]:
        d = start.date()
        while d <= end.date():
            yield d
            d += timedelta(days=1)

    # --- public -----------------------------------------------------------
    def fetch(self, spec: DatasetSpec, start: datetime, end: datetime) -> Iterator[pd.DataFrame]:
        """Yield one raw DataFrame per request so callers can persist progressively."""
        if spec.query == "window":
            for a, b in self.windows(start, end, spec.chunk_days):
                params = {spec.params["from"]: _iso(a), spec.params["to"]: _iso(b), **spec.extra_params}
                rows = self._get(spec.path, params)
                log.info("%s %s..%s: %d rows", spec.name, _iso(a), _iso(b), len(rows))
                yield pd.DataFrame(rows)
        elif spec.query == "settlement_date":
            for d in self.dates(start, end):
                rows = self._get(spec.path.format(date=d.isoformat()), dict(spec.extra_params))
                log.info("%s %s: %d rows", spec.name, d, len(rows))
                yield pd.DataFrame(rows)
        else:  # pragma: no cover - guarded by registry validation
            raise ValueError(spec.query)
