"""Loads and validates ``xbrl_map.yaml``: exchange results XBRL element → canonical item_code
(SPEC v0.2 §3.6 step 1). The YAML is the only place XBRL element names live; see its header
for the schema. Fails fast (``XbrlMapError``) on an invalid map."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.data.canonical import CANONICAL_FIELDS, Table, fields_for

MAP_PATH = Path(__file__).with_name("xbrl_map.yaml")

Statement = Literal["pl", "bs", "cf", "ratio"]
Unit = Literal["amount", "per_share", "pct", "shares"]
Target = Literal["canonical", "extra", "line"]
TagGroups = dict[str, list[str]]  # ordered: group name → element names

# Canonical fields the parser computes from other items rather than reading from a tag
# (formulas documented in CANONICAL_FIELDS[...].derived["nse"]).
DERIVED_IN_CODE = frozenset({"ebit", "ebitda", "shares_diluted_cr", "book_value_per_share"})
REQUIRED_INFO = frozenset(
    {"period_start", "period_end", "fy_start", "nature", "audited", "board_meeting", "rounding",
     "company", "symbol", "scrip_code", "isin"}
)  # fmt: skip
_CANONICAL_UNIT = {"cr": "amount", "rs": "per_share", "pct": "pct", "cr_shares": "shares"}


class XbrlMapError(ValueError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ItemSpec(_Strict):
    statement: Statement
    unit: Unit
    target: Target
    tags: TagGroups = Field(default_factory=dict)
    sum_of: TagGroups = Field(default_factory=dict)
    magnitude: bool = False
    carry_to_year: bool = False

    @model_validator(mode="after")
    def _check(self) -> "ItemSpec":
        if not self.tags and not self.sum_of:
            raise ValueError("needs tags or sum_of")
        for groups in (self.tags, self.sum_of):
            for group, names in groups.items():
                if not names or any(not n or not n.isidentifier() for n in names):
                    raise ValueError(f"group {group!r}: element names must be non-empty "
                                     "identifiers")  # fmt: skip
        return self

    def tag_candidates(self) -> list[tuple[str, str]]:
        """(group, element) in priority order."""
        return [(g, n) for g, names in self.tags.items() for n in names]

    def sum_groups(self) -> list[tuple[str, list[str]]]:
        return list(self.sum_of.items())


class XbrlMap(_Strict):
    version: int = Field(ge=1)
    items: dict[str, ItemSpec]
    bank_marker: str
    info: dict[str, list[str]]
    # dimension axes that only label a context consolidated / standalone (not a segment)
    basis_axes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "XbrlMap":
        for code, spec in self.items.items():
            if spec.target == "canonical":
                field = CANONICAL_FIELDS.get(code)
                if field is None or field.tables == ("shareholding",):
                    raise ValueError(f"{code}: target canonical but not a fin-table field")
                expected = _CANONICAL_UNIT[field.unit]
                if spec.unit != expected:
                    raise ValueError(f"{code}: unit {spec.unit} but the canonical field is "
                                     f"{expected}")  # fmt: skip
                if code in DERIVED_IN_CODE:
                    raise ValueError(f"{code}: computed by the parser; do not map it")
            elif code in CANONICAL_FIELDS:
                raise ValueError(f"{code}: is a canonical field; target must be canonical")
        if missing := REQUIRED_INFO - set(self.info):
            raise ValueError(f"info is missing {sorted(missing)}")
        return self

    def canonical_items(self) -> dict[str, ItemSpec]:
        return {c: s for c, s in self.items.items() if s.target == "canonical"}

    def unmapped(self, table: Table) -> list[str]:
        """Canonical fields of ``table`` the results filings cannot supply (→ data gaps)."""
        mapped = set(self.canonical_items()) | DERIVED_IN_CODE
        return [f for f in fields_for(table) if f not in mapped]

    def all_elements(self) -> set[str]:
        names = {n for s in self.items.values() for g in (s.tags, s.sum_of)
                 for group in g.values() for n in group}  # fmt: skip
        names.update(n for group in self.info.values() for n in group)
        return names


def load_xbrl_map(path: Path = MAP_PATH) -> XbrlMap:
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise XbrlMapError(f"{path}: {exc}") from exc
    try:
        return XbrlMap.model_validate(raw)
    except ValidationError as exc:
        raise XbrlMapError(f"invalid {path.name}: {exc}") from exc


@lru_cache
def get_xbrl_map() -> XbrlMap:
    return load_xbrl_map()
