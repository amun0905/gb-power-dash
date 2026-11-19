"""Source clients. Each client exposes `fetch(spec, start, end) -> Iterator[DataFrame]`."""
from __future__ import annotations


def get_client(source: str):
    if source == "elexon":
        from .elexon import ElexonClient
        return ElexonClient()
    # Future: "neso", "entsoe" (needs ENTSOE_API_TOKEN), "carbon_intensity", ...
    raise ValueError(f"No client implemented for source '{source}'")


def attribution(source: str) -> str:
    if source == "elexon":
        from .elexon import ATTRIBUTION
        return ATTRIBUTION
    return ""
