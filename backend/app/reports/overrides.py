"""User assumptions per stock (``user_overrides``; SPEC §8 ``POST /overrides``).

Each key is stored as its own row (``{"value": ...}``). Three groups:
- DCF assumptions that replace the computed base inputs: g1, ebit_margin, wacc, g_terminal,
  tax_rate, da_pct, capex_pct, nwc_pct (fractions).
- ``sector``: the ``config/sectors.yaml`` entry (and so the valuation model) to use.
- Manual inputs the data sources cannot supply: NAV (real estate), embedded value / VNB and
  P/EV multiples (insurers), SOTP holdings (holding companies), and governance flags
  (related-party-transaction concern, auditor resignations). SPEC §7.1 allows these as
  manual flags.
"""

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

DCF_KEYS = ("g1", "ebit_margin", "wacc", "g_terminal", "tax_rate", "da_pct", "capex_pct", "nwc_pct")


class Overrides(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    g1: float | None = Field(None, ge=-0.5, le=1.0, description="Stage-1 revenue growth")
    ebit_margin: float | None = Field(None, gt=-1.0, lt=1.0)
    wacc: float | None = Field(None, gt=0.0, lt=0.5)
    g_terminal: float | None = Field(None, ge=0.0, lt=0.2)
    tax_rate: float | None = Field(None, ge=0.0, le=1.0)
    da_pct: float | None = Field(None, ge=0.0, lt=1.0)
    capex_pct: float | None = Field(None, ge=0.0, lt=1.0)
    nwc_pct: float | None = Field(None, gt=-1.0, lt=1.0)
    sector: str | None = Field(None, description="sectors.yaml key; selects the model")
    nav_per_share: float | None = Field(None, gt=0)
    embedded_value_per_share: float | None = Field(None, gt=0)
    vnb_per_share: float | None = Field(None, gt=0)
    vnb_multiple: float | None = Field(None, gt=0)
    p_ev_band_median: float | None = Field(None, gt=0, description="Own-history P/EV median")
    peer_p_ev: float | None = Field(None, gt=0, description="Peer median P/EV")
    listed_holdings_value_cr: float | None = Field(None, ge=0)
    standalone_value_cr: float | None = None
    rpt_flagged: bool | None = Field(None, description="Related-party-transaction concern")
    auditor_resignations: list[date] | None = Field(
        None, description="Auditor resignation dates ([] = none on record)"
    )

    def dcf(self) -> dict[str, float]:
        return {k: v for k in DCF_KEYS if (v := getattr(self, k)) is not None}

    def as_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude_none=True)
