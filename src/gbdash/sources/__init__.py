"""Source clients. Each client exposes `fetch(spec, start, end) -> Iterator[DataFrame]`."""
from __future__ import annotations


class SourceNotConfigured(RuntimeError):
    """Raised when a source needs credentials that aren't set (e.g. ENTSOE_API_TOKEN).
    The CLI treats this as 'skip with a warning', not as a failed run."""


def get_client(source: str):
    if source == "elexon":
        from .elexon import ElexonClient
        return ElexonClient()
    if source == "entsoe":
        from .entsoe import EntsoeClient
        return EntsoeClient()
    if source == "neso":
        from .neso import NesoClient
        return NesoClient()
    if source == "ecb":
        from .ecb import EcbClient
        return EcbClient()
    raise ValueError(f"No client implemented for source '{source}'")


def is_configured(source: str) -> bool:
    if source == "entsoe":
        from .entsoe import token
        return bool(token())
    return True


ATTRIBUTIONS = {
    "elexon": "Contains BMRS data © Elexon Limited copyright and database right",
    "entsoe": "Day-ahead prices: ENTSO-E Transparency Platform",
    "neso": "GB day-ahead (N2EX) prices: NESO Data Portal, NESO Open Data Licence",
    "ecb": "EUR/GBP reference rate: European Central Bank",
}


def attribution(source: str) -> str:
    return ATTRIBUTIONS.get(source, "")
