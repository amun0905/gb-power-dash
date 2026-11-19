"""Orchestration: incremental updates, resumable backfills, freshness checks."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd

from .registry import DatasetSpec
from .sources import attribution, get_client
from .storage import Store
from .transform import normalise

log = logging.getLogger(__name__)

BOOTSTRAP_DAYS = 7  # first `update` on an empty store fetches this much; use `backfill` for history


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(second=0, microsecond=0)


def _ingest(spec: DatasetSpec, store: Store, client, start: datetime, end: datetime) -> dict:
    """Fetch [start, end), normalise, and upsert. Writes once per month of data to
    limit object-store writes, so an interrupted run keeps everything up to the last month."""
    totals = {"incoming": 0, "inserted": 0, "updated_or_same": 0}
    buffer: list[pd.DataFrame] = []

    def flush():
        if not buffer:
            return
        df = normalise(pd.concat(buffer, ignore_index=True), spec)
        buffer.clear()
        s = store.upsert(spec, df)
        for k in totals:
            totals[k] += s[k]

    current_month = None
    for raw in client.fetch(spec, start, end):
        clean = normalise(raw, spec)  # validate each response early (fail fast on schema drift)
        if clean.empty:
            continue
        month = clean[spec.time_column].max().strftime("%Y-%m")
        if current_month and month != current_month:
            flush()
        current_month = month
        buffer.append(clean)
    flush()
    return totals


def update(spec: DatasetSpec, store: Store, client=None, now: datetime | None = None) -> dict:
    """Bring a dataset up to date, re-fetching `lookback_days` to capture revisions."""
    client = client or get_client(spec.source)
    now = now or utcnow()
    latest = store.latest_time(spec)
    if latest is None:
        start = max(now - timedelta(days=BOOTSTRAP_DAYS),
                    datetime.combine(spec.start, datetime.min.time(), tzinfo=timezone.utc))
        log.info("%s: empty store, bootstrapping from %s", spec.name, start)
    else:
        start = latest.to_pydatetime() - timedelta(days=spec.lookback_days)
    stats = _ingest(spec, store, client, start, now)
    store.write_manifest(spec, attribution(spec.source))
    log.info("%s: %s", spec.name, stats)
    return stats


def _month_looks_complete(spec, store, period, month_start, month_end, tolerance=timedelta(hours=6)) -> bool:
    """A partition counts as done if it spans (almost) the whole month. Guards against
    partitions that only hold a few spill-over rows from a neighbouring month's fetch."""
    rng = store.time_range(spec, period)
    if rng is None:
        return False
    lo, hi = rng
    return lo <= month_start + tolerance and hi >= month_end - tolerance - timedelta(hours=1)


def backfill(spec: DatasetSpec, store: Store, start: datetime, end: datetime | None = None,
             client=None, resume: bool = True) -> dict:
    """Load history month by month. With resume=True, months already complete are skipped,
    so a killed backfill can simply be re-run."""
    client = client or get_client(spec.source)
    end = end or utcnow()
    have = set(store.partitions(spec.name))
    totals = {"incoming": 0, "inserted": 0, "updated_or_same": 0, "skipped_months": 0}
    month_start = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while month_start < end:
        month_end = (month_start + timedelta(days=32)).replace(day=1)
        period = month_start.strftime("%Y-%m")
        settled = month_end <= end - timedelta(days=spec.lookback_days)
        if resume and settled and period in have and _month_looks_complete(spec, store, period, month_start, month_end):
            totals["skipped_months"] += 1
        else:
            s = _ingest(spec, store, client, max(month_start, start), min(month_end, end))
            for k in ("incoming", "inserted", "updated_or_same"):
                totals[k] += s[k]
            log.info("%s backfill %s: %s", spec.name, period, s)
        month_start = month_end
    store.write_manifest(spec, attribution(spec.source))
    return totals


def freshness(spec: DatasetSpec, store: Store, now: datetime | None = None) -> dict:
    now = now or utcnow()
    latest = store.latest_time(spec)
    age_h = None if latest is None else (now - latest.to_pydatetime()).total_seconds() / 3600
    return {
        "dataset": spec.name,
        "latest": None if latest is None else latest.isoformat(),
        "age_hours": None if age_h is None else round(age_h, 2),
        "stale": age_h is None or age_h > spec.max_staleness_hours,
    }
