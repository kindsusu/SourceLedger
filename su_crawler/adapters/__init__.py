"""Registry of deterministic first-party extraction adapters."""
from __future__ import annotations

from typing import Callable

from ..models import Candidate, FetchResult, Source
from . import funrent, gongcar, jetcar

ADAPTER_VERSION = "1"
_ADAPTERS: dict[str, Callable[[FetchResult, Source], list[Candidate]]] = {
    "jetcar": jetcar.extract,
    "gongcar": gongcar.extract,
    "funrent": funrent.extract,
}
_METADATA = {
    "jetcar": {"version": ADAPTER_VERSION, "requires_rendered_html": False, "hosts": ["jetcar.kr", "www.jetcar.kr"]},
    "gongcar": {"version": ADAPTER_VERSION, "requires_rendered_html": True, "hosts": ["gongcarrent.kr", "www.gongcarrent.kr"]},
    "funrent": {"version": ADAPTER_VERSION, "requires_rendered_html": True, "hosts": ["go.funrentcar.com"]},
}


def available_adapters() -> dict[str, dict[str, object]]:
    return {name: dict(metadata) for name, metadata in _METADATA.items()}


def extract_adapter(result: FetchResult, source: Source) -> list[Candidate]:
    name = source.adapter
    if not name:
        return []
    try:
        extractor = _ADAPTERS[name]
    except KeyError as exc:
        raise ValueError(f"Unknown source adapter: {name}") from exc
    if result.status != "fetched" or "html" not in result.media_type.lower():
        return []
    return extractor(result, source)


__all__ = ["ADAPTER_VERSION", "available_adapters", "extract_adapter"]
