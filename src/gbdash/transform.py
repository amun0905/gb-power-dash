"""Turn raw API rows into a clean, typed frame that matches the registry schema."""
from __future__ import annotations

import logging

import pandas as pd

from .registry import DatasetSpec

log = logging.getLogger(__name__)


class SchemaError(ValueError):
    pass


def empty_frame(spec: DatasetSpec) -> pd.DataFrame:
    return _cast(pd.DataFrame({c: pd.Series(dtype="object") for c in spec.columns}), spec)


def _cast(df: pd.DataFrame, spec: DatasetSpec) -> pd.DataFrame:
    for col, typ in spec.columns.items():
        s = df[col]
        if typ == "datetime":
            df[col] = pd.to_datetime(s, utc=True, errors="coerce").astype("datetime64[ns, UTC]")
        elif typ == "date":
            df[col] = pd.to_datetime(s, errors="coerce").dt.normalize().astype("datetime64[ns]")
        elif typ == "int":
            df[col] = pd.to_numeric(s, errors="coerce").astype("Int64")
        elif typ == "float":
            df[col] = pd.to_numeric(s, errors="coerce").astype("float64")
        elif typ == "str":
            df[col] = s.astype("string")
    return df


def normalise(raw: pd.DataFrame, spec: DatasetSpec) -> pd.DataFrame:
    """Select declared columns, coerce types, drop rows without time/keys, de-duplicate."""
    if raw is None or raw.empty:
        return empty_frame(spec)

    required = {spec.time_column, *spec.keys}
    if spec.version_column:
        required.add(spec.version_column)
    missing_required = sorted(required - set(raw.columns))
    if missing_required:
        raise SchemaError(
            f"{spec.name}: API response is missing required columns {missing_required}. "
            f"Got: {sorted(raw.columns)}"
        )
    missing_optional = [c for c in spec.columns if c not in raw.columns]
    if missing_optional:
        log.warning("%s: columns %s absent upstream; filled with nulls", spec.name, missing_optional)

    df = raw.reindex(columns=list(spec.columns)).copy()
    df = _cast(df, spec)

    before = len(df)
    df = df.dropna(subset=sorted(required - {spec.version_column} if spec.version_column else required))
    dropped = before - len(df)
    if dropped:
        log.warning("%s: dropped %d rows with null time/key values", spec.name, dropped)

    return dedupe(df, spec)


def dedupe(df: pd.DataFrame, spec: DatasetSpec) -> pd.DataFrame:
    """Keep one row per key. With a version column, the latest version wins;
    otherwise the last row wins (callers append newer data after older data)."""
    if df.empty:
        return df
    if spec.version_column:
        df = df.sort_values(spec.version_column, kind="stable", na_position="first")
    df = df.drop_duplicates(subset=spec.keys, keep="last")
    return df.sort_values([spec.time_column, *[k for k in spec.keys if k != spec.time_column]]).reset_index(drop=True)
