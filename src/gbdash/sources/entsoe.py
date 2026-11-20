"""ENTSO-E Transparency Platform client: day-ahead prices (document type A44).

Needs a free API token in env ENTSOE_API_TOKEN (transparency.entsoe.eu -> My Account -> Web API Access).
Docs: https://transparency.entsoe.eu/content/static_content/Static%20content/web%20api/Guide.html
"""
from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Iterator

import pandas as pd
import requests

from ..http import make_session
from ..registry import DatasetSpec
from . import SourceNotConfigured

log = logging.getLogger(__name__)

BASE_URL = "https://web-api.tp.entsoe.eu/api"

# Bidding-zone EIC codes
ZONES = {
    "FR": "10YFR-RTE------C",
    "BE": "10YBE----------2",
    "NL": "10YNL----------L",
    "DE_LU": "10Y1001A1001A82H",
    "NO2": "10YNO-2--------T",
    "DK1": "10YDK-1--------W",
    "IE_SEM": "10Y1001A1001A59C",
}

_RES = {"PT15M": 15, "PT30M": 30, "PT60M": 60}


class TransientError(RuntimeError):
    """ENTSO-E server-side trouble (5xx/599, timeouts) that a later run will likely not hit."""


def token() -> str | None:
    return os.getenv("ENTSOE_API_TOKEN") or None


def _fmt(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y%m%d%H%M")


def _strip_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def parse_a44(xml_text: str | bytes, zone: str) -> pd.DataFrame:
    """Parse a day-ahead price document into rows (startTime, zone, price, currency, resolution_min).

    Handles: several TimeSeries/Periods, 15/30/60-min resolutions, and curveType A03 where
    repeated prices are omitted (a missing position means 'same as previous').
    When two resolutions cover the same instant, the finer one is kept.
    """
    if isinstance(xml_text, str):
        xml_text = xml_text.encode("utf-8")
    xml_text = xml_text.lstrip(b"\xef\xbb\xbf \r\n\t")  # tolerate a UTF-8 byte-order mark / leading whitespace
    if not xml_text.startswith(b"<"):
        raise RuntimeError(f"ENTSO-E returned non-XML for {zone}: {xml_text[:200]!r}")
    root = _strip_ns(ET.fromstring(xml_text))
    if root.tag == "Acknowledgement_MarketDocument":
        reason = root.findtext(".//Reason/text") or ""
        if "No matching data" in reason or root.findtext(".//Reason/code") == "999":
            return pd.DataFrame()
        raise RuntimeError(f"ENTSO-E error for {zone}: {reason}")

    rows = []
    for ts in root.iter("TimeSeries"):
        currency = ts.findtext("currency_Unit.name") or "EUR"
        for period in ts.iter("Period"):
            start = pd.Timestamp(period.findtext("timeInterval/start"))
            end = pd.Timestamp(period.findtext("timeInterval/end"))
            res = period.findtext("resolution")
            minutes = _RES.get(res)
            if minutes is None:
                m = re.fullmatch(r"PT(\d+)M", res or "")
                if not m:
                    log.warning("Skipping unknown resolution %s for %s", res, zone)
                    continue
                minutes = int(m.group(1))
            n = int((end - start) / pd.Timedelta(minutes=minutes))
            pts = {int(p.findtext("position")): float(p.findtext("price.amount")) for p in period.iter("Point")}
            last = None
            for pos in range(1, n + 1):
                if pos in pts:
                    last = pts[pos]
                if last is None:
                    continue
                rows.append({"startTime": start + pd.Timedelta(minutes=minutes * (pos - 1)), "zone": zone,
                             "price": last, "currency": currency, "resolution_min": minutes})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    # prefer finest resolution where series overlap
    df = df.sort_values(["startTime", "resolution_min"]).drop_duplicates(["startTime", "zone"], keep="first")
    return df


class EntsoeClient:
    def __init__(self, session=None, timeout: float = 60):
        self.session = session or make_session()
        self.timeout = timeout

    def fetch(self, spec: DatasetSpec, start: datetime, end: datetime) -> Iterator[pd.DataFrame]:
        tok = token()
        if not tok:
            raise SourceNotConfigured("ENTSOE_API_TOKEN is not set; skipping ENTSO-E datasets")
        zones = spec.options.get("zones") or list(ZONES)
        step = timedelta(days=spec.chunk_days)
        failed = []
        for zone in zones:
            eic = ZONES.get(zone, zone)
            cur = start
            try:
                while cur < end:
                    nxt = min(cur + step, end)
                    df = self._fetch_one(tok, spec, zone, eic, cur, nxt)
                    log.info("%s %s %s..%s: %d rows", spec.name, zone, _fmt(cur), _fmt(nxt), len(df))
                    yield df
                    cur = nxt
            except TransientError as exc:
                # ENTSO-E has occasional gateway timeouts. Skip this zone for this run; the next run
                # re-fetches the lookback window, and `gbdash status` flags it if it stays missing.
                log.warning("%s: skipping %s this run after retries: %s", spec.name, zone, exc)
                failed.append(zone)
        if failed and len(failed) == len(zones):
            raise RuntimeError(f"ENTSO-E unavailable for every zone this run ({', '.join(failed)}).")

    def _fetch_one(self, tok, spec, zone, eic, cur, nxt) -> pd.DataFrame:
        params = {"securityToken": tok, "documentType": "A44", "in_Domain": eic, "out_Domain": eic,
                  "periodStart": _fmt(cur), "periodEnd": _fmt(nxt)}
        try:
            # Ask for the documented IEC XML format explicitly: the renewed platform answers
            # with its own JSON format when the client prefers JSON.
            resp = self.session.get(BASE_URL, params=params, timeout=self.timeout,
                                    headers={"Accept": "application/xml, text/xml;q=0.9"})
        except requests.RequestException as exc:  # connection errors/timeouts after retries
            raise TransientError(str(exc)) from exc
        if resp.status_code in (401, 403):
            raise RuntimeError(f"ENTSO-E rejected the token ({resp.status_code}). Check the ENTSOE_API_TOKEN "
                               "secret, and that Restful API access is enabled for your account.")
        if resp.status_code >= 400 and b"Acknowledgement" not in resp.content:
            safe_url = resp.url.replace(tok, "***")
            msg = f"GET {safe_url} -> {resp.status_code}: {resp.text[:300]}"
            raise TransientError(msg) if resp.status_code >= 500 else RuntimeError(msg)
        if resp.content.lstrip()[:1] in (b"{", b"["):
            dump = f"entsoe_debug_{zone}.json"
            with open(dump, "wb") as fh:
                fh.write(resp.content)
            raise RuntimeError(f"ENTSO-E answered in JSON even though XML was requested; the response "
                               f"was saved to {dump} so the parser can be adapted.")
        try:
            return parse_a44(resp.content, zone)
        except Exception as exc:
            ctype = resp.headers.get("Content-Type")
            raise RuntimeError(f"Could not parse ENTSO-E response for {zone} (HTTP {resp.status_code}, "
                               f"Content-Type {ctype}): {exc}. Body starts: {resp.content[:200]!r}") from exc
