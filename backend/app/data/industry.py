"""Industry classification → sector model (pure). ``config/industries.yaml`` maps each source's
label (NSE ``basicIndustry``, Yahoo ``industry``) to a sectors.yaml key; banks / NBFCs /
insurers then use their own valuation models (AGENTS.md rule 10)."""

from dataclasses import dataclass
from typing import Any

from app.core.config import IndustriesConfig, normalise_label
from app.data.providers.base import ProviderError


@dataclass(frozen=True)
class IndustryInfo:
    """One source's classification of a stock. ``label`` is the level the mapping uses:
    NSE's basic industry, or Yahoo's industry."""

    source: str  # "nse" | "yfinance"
    label: str
    macro: str | None = None
    sector: str | None = None
    industry: str | None = None


@dataclass(frozen=True)
class Classification:
    sector: str | None  # sectors.yaml key, None = unmapped
    reason: str


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip()
    return v if v and v != "-" else None


def parse_nse_quote_industry(payload: Any) -> IndustryInfo | None:
    """``industryInfo`` of NSE's quote API: ``{macro, sector, industry, basicIndustry}``.
    None when NSE has no basic industry for the symbol (e.g. a suspended stock)."""
    if not isinstance(payload, dict):
        raise ProviderError(f"NSE quote: unexpected payload type {type(payload).__name__}")
    info = payload.get("industryInfo")
    if info is None:
        return None
    if not isinstance(info, dict):
        raise ProviderError("NSE quote: industryInfo is not an object")
    label = _clean(info.get("basicIndustry"))
    if label is None:
        return None
    return IndustryInfo("nse", label, _clean(info.get("macro")), _clean(info.get("sector")),
                        _clean(info.get("industry")))  # fmt: skip


def parse_yfinance_info(info: Any) -> IndustryInfo | None:
    if not isinstance(info, dict):
        return None
    label = _clean(info.get("industry"))
    if label is None:
        return None
    return IndustryInfo("yfinance", label, None, _clean(info.get("sector")), label)


def classify(info: IndustryInfo, cfg: IndustriesConfig) -> Classification:
    sector = cfg.table(info.source).get(normalise_label(info.label))
    if sector is None:
        why = (f"sector unmapped: {info.source} industry '{info.label}' has no entry in "
               "industries.yaml; using the default model")  # fmt: skip
        return Classification(None, why)
    return Classification(sector, f"{info.source} industry '{info.label}' → {sector}")
