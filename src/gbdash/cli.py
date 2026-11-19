"""Command line entry point.

    gbdash list
    gbdash update [DATASET ...]
    gbdash backfill DATASET [--start 2024-01-01] [--end 2024-06-01] [--no-resume]
    gbdash status [--fail-on-stale]
    gbdash export

Storage location: --store, or env GBDASH_STORE (default ./data).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

from . import pipeline
from .registry import load_registry
from .sources import SourceNotConfigured, attribution, is_configured
from .storage import Store

log = logging.getLogger("gbdash")


def _dt(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _pick(registry, names):
    if not names:
        return list(registry.values())
    unknown = [n for n in names if n not in registry]
    if unknown:
        sys.exit(f"Unknown dataset(s): {unknown}. Known: {sorted(registry)}")
    return [registry[n] for n in names]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="gbdash")
    p.add_argument("--store", default=os.getenv("GBDASH_STORE", "./data"))
    p.add_argument("--config", default=os.getenv("GBDASH_CONFIG"))
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list")
    u = sub.add_parser("update"); u.add_argument("datasets", nargs="*")
    b = sub.add_parser("backfill")
    b.add_argument("datasets", nargs="+")
    b.add_argument("--start", type=_dt, help="default: dataset's configured start")
    b.add_argument("--end", type=_dt)
    b.add_argument("--no-resume", action="store_true")
    s = sub.add_parser("status"); s.add_argument("--fail-on-stale", action="store_true")
    sub.add_parser("export", help="write web/overview.json for the website")

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    registry = load_registry(a.config)

    if a.cmd == "list":
        for spec in registry.values():
            print(f"{spec.name:24s} {spec.source:8s} {spec.description}")
        return 0

    store = Store(a.store)
    failures = []

    if a.cmd == "update":
        for spec in _pick(registry, a.datasets):
            try:
                pipeline.update(spec, store)
            except SourceNotConfigured as e:
                log.warning("%s: %s", spec.name, e)
            except Exception:  # keep going: one broken feed shouldn't stop the others
                log.exception("update failed for %s", spec.name)
                failures.append(spec.name)

    elif a.cmd == "backfill":
        for spec in _pick(registry, a.datasets):
            start = a.start or datetime.combine(spec.start, datetime.min.time(), tzinfo=timezone.utc)
            try:
                print(spec.name, pipeline.backfill(spec, store, start, a.end, resume=not a.no_resume))
            except SourceNotConfigured as e:
                log.warning("%s: %s", spec.name, e)
            except Exception:
                log.exception("backfill failed for %s", spec.name)
                failures.append(spec.name)

    elif a.cmd == "status":
        report = [pipeline.freshness(spec, store) if is_configured(spec.source)
                  else {"dataset": spec.name, "skipped": "source not configured", "stale": False}
                  for spec in registry.values()]
        print(json.dumps(report, indent=2))
        if a.fail_on_stale:
            failures = [r["dataset"] for r in report if r["stale"]]

    elif a.cmd == "export":
        from .export import write_overview
        from .export_prices import write_prices
        for name, fn in (("overview", write_overview), ("prices", write_prices)):
            try:
                fn(store, registry)
            except Exception:
                log.exception("export %s failed", name)
                failures.append(f"export:{name}")

    if a.cmd in ("update", "backfill"):
        manifests = [m for spec in registry.values() if (m := store.manifest(spec))]
        store.write_catalog(manifests)

    if failures:
        log.error("Failed/stale: %s", failures)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
