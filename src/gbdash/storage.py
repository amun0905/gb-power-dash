"""Parquet store: one file per dataset per month, on local disk or any S3-compatible bucket.

Layout
    <root>/catalog.json                       summary of every dataset (for the website)
    <root>/<dataset>/_manifest.json           rows, time range, schema, last update
    <root>/<dataset>/<YYYY-MM>.parquet        data, partitioned by month of time_column (UTC)

Root can be a local path (./data) or e.g. s3://my-bucket/gb  (Cloudflare R2, Backblaze B2, AWS).
For S3-compatible stores set AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY and GBDASH_S3_ENDPOINT.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone

import fsspec
import pandas as pd
import pyarrow.parquet as pq

from .registry import DatasetSpec
from .transform import dedupe, empty_frame

log = logging.getLogger(__name__)


def _utc(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _storage_options(url: str) -> dict:
    if url.startswith("s3://"):
        opts: dict = {"client_kwargs": {}}
        if os.getenv("GBDASH_S3_ENDPOINT"):
            opts["client_kwargs"]["endpoint_url"] = os.environ["GBDASH_S3_ENDPOINT"]
            opts["client_kwargs"]["region_name"] = os.getenv("GBDASH_S3_REGION", "auto")
        return opts
    return {}


class Store:
    def __init__(self, url: str):
        self.url = url
        self.fs, self.root = fsspec.core.url_to_fs(url, **_storage_options(url))
        self.root = self.root.rstrip("/")
        protocols = self.fs.protocol if isinstance(self.fs.protocol, (tuple, list)) else (self.fs.protocol,)
        self.is_local = bool({"file", "local"} & set(protocols))

    # --- paths ------------------------------------------------------------
    def _dir(self, dataset: str) -> str:
        return f"{self.root}/{dataset}"

    def _part(self, dataset: str, period: str) -> str:
        return f"{self._dir(dataset)}/{period}.parquet"

    def partitions(self, dataset: str) -> list[str]:
        """Sorted list of 'YYYY-MM' periods present for a dataset."""
        d = self._dir(dataset)
        if not self.fs.exists(d):
            return []
        names = [p.rsplit("/", 1)[-1] for p in self.fs.ls(d, detail=False)]
        return sorted(n[:-8] for n in names if n.endswith(".parquet") and not n.startswith("_"))

    # --- low-level io -----------------------------------------------------
    def _read_part(self, spec: DatasetSpec, period: str) -> pd.DataFrame:
        path = self._part(spec.name, period)
        if not self.fs.exists(path):
            return empty_frame(spec)
        with self.fs.open(path, "rb") as f:
            return pd.read_parquet(f)

    def _write_bytes_atomic(self, path: str, write_fn) -> None:
        """Local: write to temp file then rename, so readers never see half a file.
        Object stores: a single PUT is already atomic."""
        if self.is_local:
            self.fs.makedirs(path.rsplit("/", 1)[0], exist_ok=True)
            tmp = f"{path}.{uuid.uuid4().hex}.tmp"
            with self.fs.open(tmp, "wb") as f:
                write_fn(f)
            os.replace(tmp, path)
        else:
            with self.fs.open(path, "wb") as f:
                write_fn(f)

    def _write_part(self, spec: DatasetSpec, period: str, df: pd.DataFrame) -> None:
        self._write_bytes_atomic(
            self._part(spec.name, period),
            lambda f: df.to_parquet(f, index=False, compression="zstd"),
        )

    def write_json(self, path: str, obj: dict, compact: bool = False) -> None:
        """Write JSON (NaN is not valid JSON, so callers must convert NaN to None)."""
        kw = {"separators": (",", ":")} if compact else {"indent": 2}
        data = json.dumps(obj, default=str, allow_nan=False, **kw).encode()
        self._write_bytes_atomic(path, lambda f: f.write(data))

    # --- public -----------------------------------------------------------
    def upsert(self, spec: DatasetSpec, df: pd.DataFrame) -> dict:
        """Merge new rows into the monthly partitions they belong to. Idempotent:
        re-running with the same data changes nothing."""
        stats = {"incoming": len(df), "inserted": 0, "updated_or_same": 0, "partitions": []}
        if df.empty:
            return stats
        periods = df[spec.time_column].dt.strftime("%Y-%m")
        for period, chunk in df.groupby(periods, sort=True):
            existing = self._read_part(spec, period)
            combined = dedupe(pd.concat([existing, chunk], ignore_index=True), spec)
            stats["inserted"] += len(combined) - len(existing)
            stats["updated_or_same"] += len(chunk) - (len(combined) - len(existing))
            self._write_part(spec, period, combined)
            stats["partitions"].append(period)
        return stats

    def read(self, spec: DatasetSpec, start=None, end=None) -> pd.DataFrame:
        periods = self.partitions(spec.name)
        if start is not None:
            periods = [p for p in periods if p >= pd.Timestamp(start).strftime("%Y-%m")]
        if end is not None:
            periods = [p for p in periods if p <= pd.Timestamp(end).strftime("%Y-%m")]
        if not periods:
            return empty_frame(spec)
        df = pd.concat([self._read_part(spec, p) for p in periods], ignore_index=True)
        if start is not None:
            df = df[df[spec.time_column] >= _utc(start)]
        if end is not None:
            df = df[df[spec.time_column] < _utc(end)]
        return df.reset_index(drop=True)

    def time_range(self, spec: DatasetSpec, period: str):
        df = self._read_part(spec, period)
        if df.empty:
            return None
        t = df[spec.time_column]
        return t.min().to_pydatetime(), t.max().to_pydatetime()

    def latest_time(self, spec: DatasetSpec) -> pd.Timestamp | None:
        for period in reversed(self.partitions(spec.name)):
            df = self._read_part(spec, period)
            if not df.empty:
                return df[spec.time_column].max()
        return None

    def manifest(self, spec: DatasetSpec) -> dict | None:
        path = f"{self._dir(spec.name)}/_manifest.json"
        if not self.fs.exists(path):
            return None
        with self.fs.open(path, "rb") as f:
            return json.loads(f.read())

    def write_manifest(self, spec: DatasetSpec, attribution: str = "") -> dict:
        periods = self.partitions(spec.name)
        rows, tmin, tmax = 0, None, None
        for p in periods:
            # Parquet metadata gives row counts cheaply; min/max only needs first & last file.
            with self.fs.open(self._part(spec.name, p), "rb") as f:
                rows += pq.ParquetFile(f).metadata.num_rows
        if periods:
            tmin = self._read_part(spec, periods[0])[spec.time_column].min()
            tmax = self._read_part(spec, periods[-1])[spec.time_column].max()
        m = {
            "name": spec.name,
            "description": spec.description,
            "source": spec.source,
            "attribution": attribution,
            "time_column": spec.time_column,
            "keys": spec.keys,
            "columns": spec.columns,
            "rows": rows,
            "min_time": tmin.isoformat() if tmin is not None else None,
            "max_time": tmax.isoformat() if tmax is not None else None,
            "partitions": periods,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.write_json(f"{self._dir(spec.name)}/_manifest.json", m)
        return m

    def write_catalog(self, manifests: list[dict]) -> None:
        self.write_json(
            f"{self.root}/catalog.json",
            {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "datasets": {m["name"]: m for m in manifests}},
        )
