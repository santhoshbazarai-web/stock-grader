"""Loads and validates ``pdf_labels.yaml``: annual-report statement row label → canonical
item_code (SPEC v0.2 §3.6 step 3). See the YAML header for the schema. Fails fast
(``PdfLabelsError``) on an invalid dictionary."""

import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.fundamentals.xbrl_map import XbrlMap, get_xbrl_map

LABELS_PATH = Path(__file__).with_name("pdf_labels.yaml")

PdfStatement = Literal["bs", "cf"]
CHECK_NAMES = frozenset(
    {"total_equity_and_liabilities", "net_cash_investing", "net_cash_financing",
     "net_change_in_cash"}
)  # fmt: skip


class PdfLabelsError(ValueError):
    pass


_ROMAN = r"(?:i{1,3}|iv|vi{0,3}|ix|x{1,3})"
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"&"), " and "),
    (re.compile(r"₹|\brs\b\.?|\binr\b"), " "),
    # list markers and note references: "(i)", "(a)", "a)", "(A+B+C)", "note 3", "[refer note 5]"
    (re.compile(rf"\((?:{_ROMAN}|[a-z]|[a-z](?:\s*[+-]\s*[a-z])+)\)"), " "),
    (re.compile(rf"^\s*(?:{_ROMAN}|[a-z])\s*[).]\s+"), " "),
    (re.compile(r"\[?\(?\s*refer(?:\s+to)?\s+note\s*[\w.()]*\s*\)?\]?"), " "),
    (re.compile(r"\bnote\s*(?:no\.?\s*)?\d[\w.()]*"), " "),
    (re.compile(r"[^a-z0-9 ]+"), " "),
    (re.compile(r"\b\d+\b"), " "),
    (re.compile(r"\s+"), " "),
]


def normalise(label: str) -> str:
    """The comparison form of a label (see pdf_labels.yaml)."""
    text = unicodedata.normalize("NFKC", label).lower().replace("\u2019", "'").replace("'", "")
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text.strip()


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Label(_Strict):
    text: str
    weight: float = Field(default=1.0, gt=0, le=1)


def _labels(value: object) -> object:
    if isinstance(value, list):
        return [{"text": v} if isinstance(v, str) else v for v in value]
    return value


class RowSpec(_Strict):
    statement: PdfStatement
    labels: list[Label] = Field(min_length=1)
    sections: list[str] = Field(default_factory=list)
    sum: bool = False
    fallback_sum: list[str] = Field(default_factory=list)

    _normalise_labels = field_validator("labels", mode="before")(_labels)

    @field_validator("labels")
    @classmethod
    def _normalised(cls, labels: list[Label]) -> list[Label]:
        for label in labels:
            if normalise(label.text) != label.text:
                raise ValueError(f"label {label.text!r} is not normalised "
                                 f"(expected {normalise(label.text)!r})")  # fmt: skip
        return labels


class Headings(_Strict):
    bs: list[str] = Field(min_length=1)
    cf: list[str] = Field(min_length=1)
    consolidated: list[str] = Field(min_length=1)
    exclude: list[str] = Field(default_factory=list)


class PdfLabels(_Strict):
    version: int = Field(ge=1)
    headings: Headings
    sections: dict[str, list[str]]
    items: dict[str, RowSpec]
    checks: dict[str, RowSpec]
    ignore: dict[PdfStatement, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> "PdfLabels":
        texts = [t for group in (*self.sections.values(), *self.ignore.values()) for t in group]
        texts += [t for h in (self.headings.bs, self.headings.cf, self.headings.consolidated,
                              self.headings.exclude) for t in h]  # fmt: skip
        for t in texts:
            if normalise(t) != t:
                raise ValueError(f"{t!r} is not normalised (expected {normalise(t)!r})")
        for code, spec in (*self.items.items(), *self.checks.items()):
            unknown = set(spec.sections) - set(self.sections)
            if unknown:
                raise ValueError(f"{code}: unknown sections {sorted(unknown)}")
            missing = [c for c in spec.fallback_sum if c not in self.items]
            if missing or code in spec.fallback_sum:
                raise ValueError(f"{code}: fallback_sum must name other items ({missing})")
        unknown_checks = set(self.checks) - CHECK_NAMES
        if unknown_checks:
            raise ValueError(f"unknown checks {sorted(unknown_checks)}; known: "
                             f"{sorted(CHECK_NAMES)}")  # fmt: skip
        return self

    def check_against(self, xmap: XbrlMap) -> None:
        """Every item must be an amount item of the same statement in xbrl_map.yaml, so PDF
        figures land in the same line items and wide columns as XBRL ones."""
        for code, spec in self.items.items():
            x = xmap.items.get(code)
            if x is None or x.statement != spec.statement or x.unit != "amount":
                raise PdfLabelsError(f"{code}: not a {spec.statement} amount item of xbrl_map.yaml")

    def magnitude(self, code: str, xmap: XbrlMap) -> bool:
        return xmap.items[code].magnitude


def load_pdf_labels(path: Path = LABELS_PATH, xmap: XbrlMap | None = None) -> PdfLabels:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        labels = PdfLabels.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise PdfLabelsError(f"{path.name}: {exc}") from exc
    labels.check_against(xmap or get_xbrl_map())
    return labels


@lru_cache(maxsize=1)
def get_pdf_labels() -> PdfLabels:
    return load_pdf_labels()
