"""Relative valuation (SPEC §5.4). Pure functions.

    adj_multiple = peer_median x (quality / peer_quality)^a x (growth / peer_growth)^b
    value        = adj_multiple x own per-share metric

``quality`` is ROCE for PE / EV-EBITDA and ROE for P/B and P/EV (financials); ``a`` and ``b``
are ``valuation.relative.roce_exponent`` / ``growth_exponent``. The power terms need positive
bases: a peer set or company with non-positive quality or growth has no relative value
(``None`` with the reason) rather than a sign-flipped or undefined one.
"""

import statistics
from dataclasses import dataclass, field

from app.core.config import ValuationConfig


@dataclass(frozen=True)
class RelativeValue:
    value: float | None
    adj_multiple: float | None
    peer_median: float | None
    reasons: list[str] = field(default_factory=list)


def relative_value(
    *,
    peer_multiples: list[float],
    peer_quality: float | None,
    peer_growth: float | None,
    quality: float | None,
    growth: float | None,
    per_share_metric: float | None,
    config: ValuationConfig,
    label: str = "PE",
) -> RelativeValue:
    peers = [m for m in peer_multiples if m is not None and m > 0]
    if not peers:
        return RelativeValue(None, None, None, [f"no peers with a positive {label}"])
    median = float(statistics.median(peers))
    checks = {
        "quality": quality,
        "peer quality": peer_quality,
        "growth": growth,
        "peer growth": peer_growth,
    }
    bad = [k for k, v in checks.items() if v is None or v <= 0]
    if bad:
        reason = f"{', '.join(bad)} missing or not positive"
        return RelativeValue(None, None, median, [reason])
    a, b = config.relative.roce_exponent, config.relative.growth_exponent
    adj = median * (quality / peer_quality) ** a * (growth / peer_growth) ** b  # type: ignore[operator]
    if per_share_metric is None or per_share_metric <= 0:
        return RelativeValue(None, adj, median, [f"own per-share metric for {label} not positive"])
    reasons = [f"peer median {label} {median:.1f}x ({len(peers)} peers), adjusted to {adj:.1f}x"]
    return RelativeValue(adj * per_share_metric, adj, median, reasons)
