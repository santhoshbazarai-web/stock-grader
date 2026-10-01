"""``config/indianapi_map.yaml``: Indian API fields → canonical line items (loaded and validated
here; read by ``app/data/indianapi_parse.py``). See the YAML for the rules."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, ValidationError, model_validator

from app.fundamentals.xbrl_map import get_xbrl_map

Section = Literal["INC", "BAL", "CAS"]
Stats = Literal["yoy_results", "balancesheet", "cashflow"]
Unit = Literal["amount", "per_share", "shares"]
Model = Literal["general", "bank"]
UNIT_CRORE = {"crore": 1.0, "million": 0.1, "lakh": 0.01}  # crore per unit
STATEMENT_STATS = {"pl": "yoy_results", "bs": "balancesheet", "cf": "cashflow"}


class IndianApiMapError(ValueError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StockSource(_Strict):
    section: Section
    keys: list[str] = Field(default_factory=list)
    sum_of: list[str] = Field(default_factory=list)
    sign: Literal[1, -1] = 1
    magnitude: bool = False

    @model_validator(mode="after")
    def _check(self) -> "StockSource":
        if bool(self.keys) == bool(self.sum_of):
            raise ValueError("give exactly one of keys / sum_of")
        return self


class HistSource(_Strict):
    stats: Stats
    labels: list[str] = Field(default_factory=list)
    sum_of: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "HistSource":
        if bool(self.labels) == bool(self.sum_of):
            raise ValueError("give exactly one of labels / sum_of")
        return self


class VendorItem(_Strict):
    statement: Literal["pl", "bs", "cf"]
    unit: Unit
    stock: StockSource | None = None
    hist: HistSource | None = None
    compare: bool = True

    @model_validator(mode="after")
    def _check(self) -> "VendorItem":
        if self.stock is None and self.hist is None:
            raise ValueError("needs a stock or a hist source")
        if self.hist is not None and STATEMENT_STATS[self.statement] != self.hist.stats:
            raise ValueError(f"a {self.statement} item reads stats={self.hist.stats}")
        return self


class Units(_Strict):
    stock_financials: Literal["crore", "million", "lakh"]
    historical_stats: Literal["crore", "million", "lakh"]
    shares: Literal["crore", "million", "lakh"]
    key_metrics_amounts: Literal["crore", "million", "lakh"]
    unit_check_tolerance: PositiveFloat
    consistency_tolerance: PositiveFloat
    quarter_sum_tolerance: PositiveFloat


class BankMarkers(_Strict):
    stock: list[str] = Field(min_length=1)
    hist: list[str] = Field(min_length=1)


class KeyMetric(_Strict):
    group: str
    key: str


class Checks(_Strict):
    key_metric_tolerance: PositiveFloat  # keyMetrics vs our derived values (relative)
    corporate_action_window_days: int = Field(ge=0)  # ex-dates this close are the same action


class IndianApiMap(_Strict):
    version: int = Field(ge=1)
    units: Units
    bank_markers: BankMarkers
    zero_means_missing: list[str] = Field(default_factory=list)
    models: dict[Model, dict[str, VendorItem]]
    key_metrics: dict[str, KeyMetric] = Field(default_factory=dict)
    checks: Checks
    fallbacks: dict[str, str] = Field(default_factory=dict)  # item → item used when missing

    @model_validator(mode="after")
    def _check(self) -> "IndianApiMap":
        if set(self.models) != {"general", "bank"}:
            raise ValueError("models must define general and bank")
        xmap = get_xbrl_map()
        for model, items in self.models.items():
            for code, item in items.items():
                spec = xmap.items.get(code)
                if spec is not None and spec.unit != item.unit:
                    raise ValueError(f"{model}.{code}: unit {item.unit}, xbrl_map says {spec.unit}")
        return self


def load_indianapi_map(path: Path) -> IndianApiMap:
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise IndianApiMapError(f"{path}: {exc}") from exc
    try:
        return IndianApiMap.model_validate(raw)
    except ValidationError as exc:
        raise IndianApiMapError(f"invalid {path.name}: {exc}") from exc


@lru_cache
def get_indianapi_map(config_dir: Path) -> IndianApiMap:
    return load_indianapi_map(config_dir / "indianapi_map.yaml")
