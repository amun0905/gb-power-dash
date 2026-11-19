"""Load and validate the dataset registry (config/datasets.yaml)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

VALID_TYPES = {"datetime", "date", "int", "float", "str"}
VALID_QUERIES = {"window", "settlement_date"}

CONFIG_RELPATH = Path("config") / "datasets.yaml"


def find_config() -> Path:
    """Locate datasets.yaml: env GBDASH_CONFIG, else ./config/ in the working directory
    (how CI runs it), else relative to the source tree (editable installs)."""
    import os
    candidates = []
    if os.getenv("GBDASH_CONFIG"):
        candidates.append(Path(os.environ["GBDASH_CONFIG"]))
    candidates += [Path.cwd() / CONFIG_RELPATH, Path(__file__).resolve().parents[2] / CONFIG_RELPATH]
    for c in candidates:
        if c.is_file():
            return c
    raise FileNotFoundError(
        "Could not find config/datasets.yaml. Run gbdash from the repo folder or set GBDASH_CONFIG. "
        f"Looked in: {[str(c) for c in candidates]}"
    )


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    source: str
    path: str
    query: str
    time_column: str
    keys: list[str]
    columns: dict[str, str]
    start: date
    description: str = ""
    params: dict[str, str] = field(default_factory=dict)
    extra_params: dict[str, str] = field(default_factory=dict)
    version_column: str | None = None
    chunk_days: int = 1
    lookback_days: int = 2
    max_staleness_hours: float = 6
    horizon_days: int = 0                      # fetch this far into the future (day-ahead data)
    options: dict = field(default_factory=dict)  # source-specific settings (zones, resource ids, ...)

    def validate(self) -> None:
        problems = []
        if self.query not in VALID_QUERIES:
            problems.append(f"query must be one of {VALID_QUERIES}")
        if self.source == "elexon" and self.query == "window" and not {"from", "to"} <= set(self.params):
            problems.append("window datasets need params.from and params.to")
        if self.query == "settlement_date" and "{date}" not in self.path:
            problems.append("settlement_date datasets need {date} in path")
        bad = {c: t for c, t in self.columns.items() if t not in VALID_TYPES}
        if bad:
            problems.append(f"unknown column types {bad}")
        for col in [self.time_column, *self.keys] + ([self.version_column] if self.version_column else []):
            if col not in self.columns:
                problems.append(f"column '{col}' used as time/key/version but not declared in columns")
        if self.columns.get(self.time_column) != "datetime":
            problems.append("time_column must be of type datetime")
        if self.chunk_days < 1 or self.lookback_days < 0:
            problems.append("chunk_days must be >=1 and lookback_days >=0")
        if problems:
            raise ValueError(f"Dataset '{self.name}': " + "; ".join(problems))


def load_registry(path: str | Path | None = None) -> dict[str, DatasetSpec]:
    path = Path(path) if path else find_config()
    raw = yaml.safe_load(path.read_text())
    specs: dict[str, DatasetSpec] = {}
    for name, cfg in (raw.get("datasets") or {}).items():
        cfg = dict(cfg)
        cfg["start"] = date.fromisoformat(str(cfg["start"]))
        spec = DatasetSpec(name=name, **cfg)
        spec.validate()
        specs[name] = spec
    if not specs:
        raise ValueError(f"No datasets defined in {path}")
    return specs
