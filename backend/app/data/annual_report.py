"""Annual-report PDF → balance-sheet and cash-flow line items with a confidence score (SPEC
v0.2 §3.6 step 3, the gap filler for years the results XBRL lacks). Deterministic; no DB or
network access.

1. **Locate** the statement pages: a page whose first ``heading_lines`` text lines hold a
   statement title (``pdf_labels.yaml`` → ``headings``; not a notes / contents line). The title
   says consolidated, else the page is standalone. Of several candidate pages for one statement
   and basis (a contents page, a highlights page), the one mapping the most rows wins; the pages
   after it continue the statement while they have no other title and map rows too.
2. **Extract** rows with pdfplumber: words grouped into lines; amounts (right-aligned) clustered
   into value columns; a column of note numbers is dropped; each column's date is read from
   the header above it. When pdfplumber maps fewer than ``camelot_min_rows`` rows on a page,
   camelot (stream mode) is tried and kept if it maps more.
3. **Map** each row label to an item_code with rapidfuzz against the label dictionary, honouring
   sections (non-current / current, operating / investing / financing). Sum items add every
   matching row; the others take the best one.
4. **Score**: confidence = label similarity x label weight x factors (camelot, ambiguous label,
   column date not printed, failed / missing cross-check, fallback sum), all in
   ``providers.yaml`` → ``nse.annual_reports.confidence``. Cross-checks: total assets = total
   equity and liabilities; operating + investing + financing cash flow = net change in cash.

Amounts are scaled to rupees by the unit the page states ("₹ in crore", "Rs. in Lakhs"; factors
from ``nse.results.rounding_levels``). A page that states no unit gives values with
``value_inr = None``: they can only be entered by hand in the review queue.
"""

import calendar
import io
import re
import tempfile
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

import pdfplumber
import pypdfium2 as pdfium
from rapidfuzz import fuzz

from app.core.config import NseAnnualReportsConfig, PdfConfidenceConfig, PdfExtractionConfig
from app.fundamentals.pdf_labels import PdfLabels, PdfStatement, RowSpec, normalise
from app.fundamentals.xbrl_map import XbrlMap

Basis = Literal["consolidated", "standalone"]
Method = Literal["pdfplumber", "camelot"]
PERIOD_TYPE: dict[PdfStatement, Literal["instant", "year"]] = {"bs": "instant", "cf": "year"}
STATEMENT_NAMES: dict[PdfStatement, str] = {"bs": "balance sheet", "cf": "cash flow"}


class AnnualReportError(ValueError):
    """The document cannot be read as an annual report (not a PDF, too long, no text)."""


def unpack(content: bytes, max_bytes: int) -> bytes:
    """The PDF itself: exchanges often serve an annual report as a ZIP holding it (the largest
    PDF inside is taken). Refuses a PDF that would unpack to more than ``max_bytes``."""
    if not content.startswith(b"PK"):
        return content
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            pdfs = [i for i in zf.infolist() if i.filename.lower().endswith(".pdf")]
            if not pdfs:
                raise AnnualReportError("ZIP holds no PDF")
            info = max(pdfs, key=lambda i: i.file_size)
            if info.file_size > max_bytes:
                raise AnnualReportError(f"PDF in ZIP larger than {max_bytes} bytes")
            with zf.open(info) as fh:
                data = fh.read(max_bytes + 1)
    except zipfile.BadZipFile as exc:
        raise AnnualReportError(f"not a readable ZIP: {exc}") from exc
    if len(data) > max_bytes:
        raise AnnualReportError(f"PDF in ZIP larger than {max_bytes} bytes")
    return data


# ───────────────────────── amounts, dates, units ─────────────────────────

_AMOUNT = re.compile(
    r"^\(?-?\(?[0-9]{1,3}(?:,[0-9]{2,3})*(?:\.[0-9]+)?\)?$|^\(?-?[0-9]+(?:\.[0-9]+)?\)?$"
)
_NIL = frozenset({"-", "\u2013", "\u2014", "--", "nil", "-00", "\u2013-"})
_NOTE = re.compile(r"^\d{1,2}(?:\.\d{1,2})?(?:\s?\([a-z]{1,4}\))?[a-z]?$", re.IGNORECASE)
_MONTHS = {
    m: i
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
_MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE_PATTERNS = [
    # 31 March 2024, 31st March, 2024, 31-Mar-2024, 31-Mar-24
    (
        re.compile(
            rf"\b(\d{{1,2}})(?:st|nd|rd|th)?[\s\-/.]*{_MONTH}[\s,\-/.]*(\d{{4}}|\d{{2}})\b", re.I
        ),
        "dmy",
    ),
    # March 31, 2024
    (re.compile(rf"\b{_MONTH}\s*(\d{{1,2}})(?:st|nd|rd|th)?,?\s*(\d{{4}})\b", re.I), "mdy"),
    # 31.03.2024, 31/03/2024, 31-03-2024
    (re.compile(r"\b(\d{1,2})[./\-](\d{1,2})[./\-](\d{4}|\d{2})\b"), "num"),
]  # fmt: skip


def parse_amount(text: str) -> float | None:
    """ "1,23,456.78" → 123456.78; "(450.00)" / "-450" → negative; a dash → 0 (a printed nil);
    anything else → None."""
    t = text.strip().replace("\u2212", "-")
    if t.lower() in _NIL:
        return 0.0
    if not _AMOUNT.match(t) or not any(ch.isdigit() for ch in t):
        return None
    negative = t.startswith("(") or t.startswith("-") or t.startswith("(-")
    digits = t.strip("()-").replace(",", "")
    try:
        value = float(digits)
    except ValueError:
        return None
    return -value if negative else value


def _year(text: str) -> int:
    y = int(text)
    return y + 2000 if y < 100 else y


def find_dates(text: str) -> list[tuple[int, int, date]]:
    """(start, end, date) of every calendar date written in ``text``, left to right."""
    found: list[tuple[int, int, date]] = []
    for pattern, kind in _DATE_PATTERNS:
        for m in pattern.finditer(text):
            if any(s <= m.start() < e for s, e, _ in found):
                continue
            try:
                if kind == "dmy":
                    d = date(_year(m.group(3)), _MONTHS[m.group(2)[:3].lower()], int(m.group(1)))
                elif kind == "mdy":
                    d = date(_year(m.group(3)), _MONTHS[m.group(1)[:3].lower()], int(m.group(2)))
                else:
                    d = date(_year(m.group(3)), int(m.group(2)), int(m.group(1)))
            except (ValueError, KeyError):
                continue
            found.append((m.start(), m.end(), d))
    return sorted(found)


def unit_factor(lines: Iterable[str], levels: dict[str, float]) -> tuple[float | None, str | None]:
    """The rupees per printed unit a page states ("₹ in crore" → 1e7) and the line saying so.
    Lines are the page's lines without amounts. Several different units → ``(None, None)``."""
    found: dict[float, str] = {}
    for line in lines:
        words = re.findall(r"[a-z]+", line.lower())
        for word in words:
            for key, factor in levels.items():
                if word.startswith(key) and len(word) <= len(key) + 2:
                    found.setdefault(factor, line.strip())
    if len(found) > 1:
        found.pop(1.0, None)  # "Indian rupees in crore": the crore counts
    if len(found) != 1:
        return None, None
    return next(iter(found.items()))


# ───────────────────────── page grids ─────────────────────────


@dataclass
class Row:
    label: str  # as printed (value cells and note numbers removed)
    values: list[float | None]  # per value column; None = blank cell
    page: int  # 1-based


@dataclass
class Grid:
    """One statement page read into rows x value columns."""

    page: int
    method: Method
    rows: list[Row]
    column_dates: list[date | None]
    unit: float | None
    unit_text: str | None
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Word:
    text: str
    x0: float
    x1: float
    top: float


def _lines(words: Sequence[_Word], tolerance: float) -> list[list[_Word]]:
    lines: list[list[_Word]] = []
    for w in sorted(words, key=lambda w: (w.top, w.x0)):
        if lines and abs(lines[-1][0].top - w.top) <= tolerance:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(line, key=lambda w: w.x0) for line in lines]


def _clusters(edges: list[float], gap: float) -> list[list[float]]:
    groups: list[list[float]] = []
    for x in sorted(edges):
        if groups and x - groups[-1][-1] <= gap:
            groups[-1].append(x)
        else:
            groups.append([x])
    return groups


def _grid_from_words(
    words: Sequence[_Word], page: int, cfg: PdfExtractionConfig, levels: dict[str, float]
) -> Grid:
    lines = _lines(words, cfg.line_tolerance_pt)
    # value columns: right edges of amounts, clustered; a cluster of note numbers is dropped
    numeric = [w for line in lines for w in line if parse_amount(w.text) is not None]
    groups = [
        g for g in _clusters([w.x1 for w in numeric], cfg.column_gap_pt)
        if len(g) >= cfg.min_column_rows
    ]  # fmt: skip
    columns: list[tuple[float, float]] = []
    for g in groups:
        lo, hi = min(g) - cfg.column_gap_pt / 2, max(g) + cfg.column_gap_pt / 2
        members = [w for w in numeric if lo <= w.x1 <= hi]
        if all(_NOTE.match(w.text) and "," not in w.text for w in members):
            continue  # note numbers
        columns.append((lo, hi))
    grid = Grid(page, "pdfplumber", [], [], None, None)
    if not columns:
        grid.warnings.append(f"page {page}: no value columns found")
        return grid
    left_edge = min(lo for lo, _ in columns)

    def column_of(w: _Word) -> int | None:
        if parse_amount(w.text) is None:
            return None
        centre = (w.x0 + w.x1) / 2
        for i, (lo, hi) in enumerate(columns):
            if lo <= w.x1 <= hi or (w.text.strip().lower() in _NIL and lo <= centre <= hi):
                return i
        return None

    header_done = False
    text_lines: list[str] = []
    header: list[list[_Word]] = []
    for line in lines:
        values: list[float | None] = [None] * len(columns)
        label_words = []
        hit = False
        for w in line:
            col = column_of(w)
            if col is not None and w.x0 >= left_edge - cfg.column_gap_pt:
                values[col] = parse_amount(w.text)
                hit = True
            elif not (_NOTE.match(w.text) and w.x1 < left_edge and w.x0 > line[0].x1):
                label_words.append(w.text)
        label = " ".join(label_words)
        if hit and not find_dates(" ".join(w.text for w in line)):
            header_done = True
            grid.rows.append(Row(label, values, page))
        else:
            text_lines.append(" ".join(w.text for w in line))
            if not header_done:
                header.append(line)
            grid.rows.append(Row(label, [None] * len(columns), page))  # a heading, maybe
    grid.unit, grid.unit_text = unit_factor(text_lines, levels)
    grid.column_dates = _column_dates(header, columns, cfg.column_gap_pt)
    return grid


def _column_dates(
    header: list[list[_Word]], columns: list[tuple[float, float]], gap: float
) -> list[date | None]:
    """For each value column, the date printed nearest above it (in the header zone)."""
    out: list[date | None] = [None] * len(columns)
    for line in header:  # top to bottom: a lower line overrides (closest to the figures)
        text, spans, pos = "", [], 0
        for w in line:
            if text:
                text += " "
            spans.append((len(text), len(text) + len(w.text), w))
            text += w.text
            pos = len(text)
        del pos
        for start, end, d in find_dates(text):
            ws = [w for s, e, w in spans if s < end and e > start]
            x0, x1 = min(w.x0 for w in ws), max(w.x1 for w in ws)
            centre = (x0 + x1) / 2
            for i, (lo, hi) in enumerate(columns):
                if lo - gap <= centre <= hi + gap or lo - gap <= x1 <= hi + gap:
                    out[i] = d
                    break
    return out


def _plumber_grids(
    pdf: Any, pages: Sequence[int], cfg: PdfExtractionConfig, levels: dict[str, float]
) -> dict[int, Grid]:
    out = {}
    for p in pages:
        raw = pdf.pages[p - 1].extract_words(keep_blank_chars=False, use_text_flow=False)
        words = [_Word(str(w["text"]), float(w["x0"]), float(w["x1"]), float(w["top"]))
                 for w in raw]  # fmt: skip
        out[p] = _grid_from_words(words, p, cfg, levels)
    return out


def _camelot_grid(
    content: bytes, page: int, cfg: PdfExtractionConfig, levels: dict[str, float]
) -> Grid | None:
    """camelot (stream mode) on one page; ``None`` when camelot is not installed or finds no
    table. Its cells become rows: value columns are the rightmost columns holding amounts."""
    try:
        from camelot.io import read_pdf  # optional extra (pyproject: camelot)
    except ImportError:
        return None
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp.write(content)
        tmp.flush()
        try:
            tables = read_pdf(tmp.name, pages=str(page), flavor="stream")
        except Exception:  # camelot raises many exception types on odd pages
            return None
    best: Grid | None = None
    for table in tables:
        cells = [[" ".join(str(c).split()) for c in row] for row in table.df.values.tolist()]
        grid = _grid_from_cells(cells, page, cfg, levels)
        if best is None or sum(any(v is not None for v in r.values) for r in grid.rows) > sum(
            any(v is not None for v in r.values) for r in best.rows
        ):
            best = grid
    return best


def _grid_from_cells(
    cells: list[list[str]], page: int, cfg: PdfExtractionConfig, levels: dict[str, float]
) -> Grid:
    width = max((len(r) for r in cells), default=0)
    counts = [sum(1 for r in cells if i < len(r) and parse_amount(r[i]) is not None
                  and r[i].strip()) for i in range(width)]  # fmt: skip
    value_cols = [
        i for i in range(width)
        if counts[i] >= cfg.min_column_rows
        and not all(_NOTE.match(r[i]) and "," not in r[i] for r in cells
                    if i < len(r) and parse_amount(r[i]) is not None and r[i].strip())
    ]  # fmt: skip
    grid = Grid(page, "camelot", [], [None] * len(value_cols), None, None)
    if not value_cols:
        return grid
    first = min(value_cols)
    header_done = False
    text_lines = []
    for r in cells:
        r = r + [""] * (width - len(r))
        values = [parse_amount(r[i]) if r[i].strip() else None for i in value_cols]
        label = " ".join(c for i, c in enumerate(r[:first]) if c and not _NOTE.match(c))
        line_text = " ".join(c for c in r if c)
        if any(v is not None for v in values) and not find_dates(line_text):
            header_done = True
            grid.rows.append(Row(label, values, page))
            continue
        text_lines.append(line_text)
        if not header_done:
            for k, i in enumerate(value_cols):
                dates = find_dates(r[i])
                if dates:
                    grid.column_dates[k] = dates[-1][2]
        grid.rows.append(Row(label, [None] * len(value_cols), page))  # a heading, maybe
    grid.unit, grid.unit_text = unit_factor(text_lines, levels)
    return grid


# ───────────────────────── locating statement pages ─────────────────────────


@dataclass(frozen=True)
class Candidate:
    statement: PdfStatement
    basis: Basis
    page: int  # 1-based
    title: str


def _contains(text: str, phrases: Iterable[str]) -> bool:
    padded = f" {text} "
    return any(f" {p} " in padded for p in phrases)


def page_title(
    text: str, labels: PdfLabels, heading_lines: int
) -> tuple[PdfStatement, Basis, str] | None:
    """The statement a page is titled with, from its first ``heading_lines`` text lines."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()][:heading_lines]
    for line in lines:
        norm = normalise(line)
        for statement in ("bs", "cf"):
            phrases = labels.headings.bs if statement == "bs" else labels.headings.cf
            if not _contains(norm, phrases) or _contains(norm, labels.headings.exclude):
                continue
            top = " ".join(normalise(x) for x in lines)
            basis: Basis = (
                "consolidated" if _contains(top, labels.headings.consolidated) else "standalone"
            )
            return statement, basis, line
    return None


def page_texts(content: bytes) -> list[str]:
    """Plain text per page (pdfium: fast enough to scan a 400-page report)."""
    try:
        doc = pdfium.PdfDocument(content)
    except pdfium.PdfiumError as exc:
        raise AnnualReportError(f"not a readable PDF: {exc}") from exc
    try:
        return [doc[i].get_textpage().get_text_range() for i in range(len(doc))]
    finally:
        doc.close()


# ───────────────────────── mapping ─────────────────────────


@dataclass
class _Match:
    code: str
    kind: Literal["item", "check", "ignore"]
    score: float  # similarity x weight, 0-1
    similarity: float
    label: str  # dictionary label matched


def _score(label: str, specs: dict[str, RowSpec]) -> dict[str, tuple[float, float, str]]:
    """code → (similarity x weight, similarity, label) for the best label of each spec."""
    out: dict[str, tuple[float, float, str]] = {}
    for code, spec in specs.items():
        best = (0.0, 0.0, "")
        for lbl in spec.labels:
            sim = fuzz.ratio(label, lbl.text) / 100
            if sim * lbl.weight > best[0]:
                best = (sim * lbl.weight, sim, lbl.text)
        out[code] = best
    return out


@dataclass
class _RowMatches:
    row: Row
    label: str  # normalised label used
    section: str | None
    matches: list[_Match]  # eligible, best first


@dataclass
class ExtractedValue:
    """One item for one period of one statement, as read from the report."""

    statement: PdfStatement
    basis: Basis
    period_end: date
    period_type: Literal["instant", "year"]
    item_code: str
    value_inr: float | None  # rupees; None when the page states no unit
    raw_value: float  # as printed (sum of the rows for a sum item), sign as printed
    raw_label: str  # printed label(s), " + " between the rows of a sum
    pages: list[int]
    method: Method
    confidence: float  # 0-1
    reasons: list[str]


@dataclass
class StatementFound:
    statement: PdfStatement
    basis: Basis
    pages: list[int]
    method: Method
    unit_text: str | None
    column_dates: list[date | None]
    checks: list[str]  # per column: "passed" / "failed" / "none"


@dataclass
class AnnualReportExtraction:
    values: list[ExtractedValue]
    statements: list[StatementFound]
    warnings: list[str]
    labels_version: int
    page_count: int


def _match_rows(
    rows: list[Row], statement: PdfStatement, labels: PdfLabels, min_score: float
) -> list[_RowMatches]:
    """Every row with values, its normalised label and the specs it may be (best first).
    Section headings (rows without values) set the section; a label wrapping over two lines
    (the first without values) is joined when that matches better."""
    items = {c: s for c, s in labels.items.items() if s.statement == statement}
    checks = {c: s for c, s in labels.checks.items() if s.statement == statement}
    ignore = labels.ignore.get(statement, [])
    sections = {t: name for name, texts in labels.sections.items() for t in texts}
    out: list[_RowMatches] = []
    section: str | None = None
    pending = ""
    for row in rows:
        norm = normalise(row.label)
        if all(v is None for v in row.values):
            if norm in sections:
                section, pending = sections[norm], ""
            else:
                pending = norm
            continue
        best_label, best_matches, best_top = norm, [], -1.0
        for candidate in (norm, f"{pending} {norm}".strip()) if pending else (norm,):
            matches = []
            for kind, specs in (("item", items), ("check", checks)):
                for code, (score, sim, lbl) in _score(candidate, specs).items():
                    spec = specs[code]
                    if spec.sections and section not in spec.sections:
                        continue
                    matches.append(_Match(code, kind, score, sim, lbl))  # type: ignore[arg-type]
            for text in ignore:
                sim = fuzz.ratio(candidate, text) / 100
                matches.append(_Match("", "ignore", sim, sim, text))
            matches.sort(key=lambda m: -m.score)
            top = matches[0].score if matches else 0.0
            if top > best_top:
                best_label, best_matches, best_top = candidate, matches, top
        pending = ""
        kept = [m for m in best_matches if m.similarity >= min_score]
        out.append(_RowMatches(row, best_label, section, kept))
    return out


def _ambiguous(rm: _RowMatches, code: str, margin: float) -> str | None:
    """Another item (or an ignore label) matched this row nearly as well."""
    mine = next(m for m in rm.matches if m.code == code)
    for m in rm.matches:
        if m.code != code and m.kind != "check" and mine.score - m.score <= margin:
            return m.code or f"'{m.label}' (not an item)"
    return None


@dataclass
class _Picked:
    value: float
    rows: list[_RowMatches]
    confidence: float
    reasons: list[str]


def _assign(
    matched: list[_RowMatches], col: int, labels: PdfLabels, statement: PdfStatement,
    conf: PdfConfidenceConfig,
) -> tuple[dict[str, _Picked], dict[str, float]]:  # fmt: skip
    """Per item: the value for column ``col``; per check: its value."""
    specs = {**{c: s for c, s in labels.items.items() if s.statement == statement},
             **{c: s for c, s in labels.checks.items() if s.statement == statement}}  # fmt: skip
    claimed: set[int] = set()
    picked: dict[str, _Picked] = {}
    checks: dict[str, float] = {}

    def is_best(rm: _RowMatches, code: str) -> bool:
        if not rm.matches:
            return False
        top = rm.matches[0].score
        mine = next((m for m in rm.matches if m.code == code), None)
        return mine is not None and top - mine.score <= 1e-9

    def open_rows(code: str) -> list[tuple[int, _RowMatches]]:
        return [
            (i, rm)
            for i, rm in enumerate(matched)
            if i not in claimed and rm.row.values[col] is not None and is_best(rm, code)
        ]

    def row_conf(rm: _RowMatches, code: str) -> tuple[float, list[str]]:
        m = next(m for m in rm.matches if m.code == code)
        c = m.score
        why = [f"'{rm.row.label or rm.label}' matched '{m.label}' ({m.similarity:.0%}"
               + (f", label weight {m.score / m.similarity:.2f})" if m.similarity and
                  m.score < m.similarity - 1e-9 else ")") + f" on page {rm.row.page}"]  # fmt: skip
        other = _ambiguous(rm, code, conf.ambiguity_margin)
        if other:
            c *= conf.ambiguous_factor
            why.append(f"ambiguous: also close to {other}")
        return c, why

    for code, spec in specs.items():
        if spec.sum:
            continue
        rows = open_rows(code)
        if not rows:
            continue
        rows.sort(key=lambda t: -next(m.score for m in t[1].matches if m.code == code))
        i, rm = rows[0]
        claimed.add(i)
        value = rm.row.values[col]
        assert value is not None
        if code in labels.checks:
            checks[code] = value
            continue
        c, why = row_conf(rm, code)
        picked[code] = _Picked(value, [rm], c, why)

    for code, spec in specs.items():
        if not spec.sum:
            continue
        rows = open_rows(code)
        if not rows:
            continue
        total, confs, why = 0.0, [], []
        for i, rm in rows:
            claimed.add(i)
            total += rm.row.values[col] or 0.0
            c, w = row_conf(rm, code)
            confs.append(c)
            why += w
        if len(rows) > 1:
            why.append(f"sum of {len(rows)} rows")
        picked[code] = _Picked(total, [rm for _, rm in rows], min(confs), why)

    for code, spec in labels.items.items():
        if spec.statement != statement or code in picked or not spec.fallback_sum:
            continue
        parts = [picked.get(p) for p in spec.fallback_sum]
        if all(p is not None for p in parts):
            ps = [p for p in parts if p is not None]
            picked[code] = _Picked(
                sum(p.value for p in ps), [r for p in ps for r in p.rows],
                min(p.confidence for p in ps) * conf.fallback_sum_factor,
                [f"no '{code}' row: sum of {' + '.join(spec.fallback_sum)}"],
            )  # fmt: skip
    return picked, checks


def _cross_check(
    statement: PdfStatement, picked: dict[str, _Picked], checks: dict[str, float], tol: float
) -> tuple[str, str]:
    """("passed" | "failed" | "none", explanation)."""

    def close(a: float, b: float, scale: float) -> bool:
        return abs(a - b) <= tol * max(abs(scale), 1e-9)

    if statement == "bs":
        ta, tel = picked.get("total_assets"), checks.get("total_equity_and_liabilities")
        if ta is None or tel is None:
            return "none", "no total assets / total equity and liabilities pair to check"
        ok = close(ta.value, tel, max(abs(ta.value), abs(tel)))
        return ("passed" if ok else "failed"), (
            f"total assets {ta.value:,.2f} {'=' if ok else '≠'} total equity and liabilities "
            f"{tel:,.2f}"
        )
    cfo = picked.get("cfo")
    parts = [checks.get(k) for k in ("net_cash_investing", "net_cash_financing")]
    net = checks.get("net_change_in_cash")
    if cfo is None or net is None or any(p is None for p in parts):
        return "none", "no operating + investing + financing = net change rows to check"
    total = cfo.value + sum(p for p in parts if p is not None)
    scale = max(abs(cfo.value), *(abs(p) for p in parts if p is not None), abs(net))
    ok = close(total, net, scale)
    return ("passed" if ok else "failed"), (
        f"operating + investing + financing {total:,.2f} {'=' if ok else '≠'} net change in cash "
        f"{net:,.2f}"
    )


# ───────────────────────── driver ─────────────────────────


def _mapped_count(grids: Sequence[Grid], statement: PdfStatement, labels: PdfLabels,
                  min_score: float) -> int:  # fmt: skip
    rows = [r for g in grids for r in g.rows]
    return sum(1 for rm in _match_rows(rows, statement, labels, min_score)
               if rm.matches and rm.matches[0].kind != "ignore")  # fmt: skip


def extract_annual_report(
    content: bytes,
    *,
    cfg: NseAnnualReportsConfig,
    rounding_levels: dict[str, float],
    labels: PdfLabels,
    xmap: XbrlMap,
    fiscal_year_end: date | None = None,
) -> AnnualReportExtraction:
    """Read the balance sheets and cash flow statements (standalone and consolidated) of an
    annual report. ``fiscal_year_end`` (the report's year end, from the exchange listing) dates
    the columns when the header prints no dates: first column that year, second the one
    before."""
    content = unpack(content, cfg.max_bytes)
    if not content.startswith(b"%PDF"):
        raise AnnualReportError("not a PDF document")
    ex, conf = cfg.extraction, cfg.confidence
    texts = page_texts(content)
    if len(texts) > ex.max_pages:
        raise AnnualReportError(f"{len(texts)} pages; more than max_pages {ex.max_pages}")
    if not any(t.strip() for t in texts):
        raise AnnualReportError("the PDF has no text layer (a scan); it needs OCR, which is "
                                "not supported: enter the figures by hand")  # fmt: skip
    warnings: list[str] = []
    candidates = [
        Candidate(s, b, i + 1, title)
        for i, text in enumerate(texts)
        if (found := page_title(text, labels, ex.heading_lines)) is not None
        for s, b, title in [found]
    ]
    out = AnnualReportExtraction([], [], warnings, labels.version, len(texts))
    if not candidates:
        warnings.append("no balance sheet or cash flow statement title found")
        return out

    titles = {c.page: (c.statement, c.basis) for c in candidates}
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        grids: dict[int, Grid] = {}

        def grid(page: int) -> Grid:
            if page not in grids:
                grids.update(_plumber_grids(pdf, [page], ex, rounding_levels))
            return grids[page]

        for statement in ("bs", "cf"):
            for basis in ("standalone", "consolidated"):
                cands = [c for c in candidates if (c.statement, c.basis) == (statement, basis)]
                if not cands:
                    continue
                scored = [(_mapped_count([grid(c.page)], statement, labels, ex.min_label_score),
                           c) for c in cands]  # fmt: skip
                best_count, best = max(scored, key=lambda t: (t[0], -t[1].page))
                pages = [best.page]
                nxt = best.page + 1
                while nxt <= len(texts):
                    title = titles.get(nxt)
                    if title is not None and title != (statement, basis):
                        break  # another statement starts
                    if title is None and _is_excluded(texts[nxt - 1], labels, ex.heading_lines):
                        break  # notes, auditor's report ...
                    if _mapped_count([grid(nxt)], statement, labels,
                                     ex.min_label_score) < ex.continuation_min_rows:  # fmt: skip
                        break
                    pages.append(nxt)
                    nxt += 1
                chosen = [grid(p) for p in pages]
                method: Method = "pdfplumber"
                if best_count < ex.camelot_min_rows:
                    alt = [_camelot_grid(content, p, ex, rounding_levels) for p in pages]
                    alt_grids = [g for g in alt if g is not None]
                    if len(alt_grids) == len(alt) and _mapped_count(
                        alt_grids, statement, labels, ex.min_label_score
                    ) > _mapped_count(chosen, statement, labels, ex.min_label_score):
                        chosen, method = alt_grids, "camelot"
                if _mapped_count(chosen, statement, labels, ex.min_label_score) == 0:
                    warnings.append(f"{basis} {STATEMENT_NAMES[statement]} (page {best.page}): "
                                    "no rows matched the label dictionary")  # fmt: skip
                    continue
                _statement_values(out, chosen, statement, basis, method, labels, xmap, conf,
                                  ex.min_label_score, fiscal_year_end)  # fmt: skip
    return out


def _is_excluded(text: str, labels: PdfLabels, heading_lines: int) -> bool:
    lines = [normalise(ln) for ln in text.splitlines() if ln.strip()][:heading_lines]
    return any(_contains(ln, labels.headings.exclude) for ln in lines[:3])


def _statement_values(
    out: AnnualReportExtraction,
    grids: list[Grid],
    statement: PdfStatement,
    basis: Basis,
    method: Method,
    labels: PdfLabels,
    xmap: XbrlMap,
    conf: PdfConfidenceConfig,
    min_score: float,
    fiscal_year_end: date | None,
) -> None:
    first = grids[0]
    ncols = len(first.column_dates)
    pages = [g.page for g in grids]
    unit, unit_text = first.unit, first.unit_text
    for g in grids[1:]:
        if len(g.column_dates) != ncols:
            out.warnings.append(f"page {g.page}: {len(g.column_dates)} value columns, page "
                                f"{first.page} has {ncols}; page skipped")  # fmt: skip
            grids = [x for x in grids if x is not g]
        elif unit is None and g.unit is not None:
            unit, unit_text = g.unit, g.unit_text
    rows = [r for g in grids for r in g.rows]
    matched = _match_rows(rows, statement, labels, min_score)
    found = StatementFound(statement, basis, pages, method, unit_text, [], [])
    name = f"{basis} {STATEMENT_NAMES[statement]}"
    if unit is None:
        out.warnings.append(f"{name} (page {first.page}): no unit stated (crore / lakh / "
                            "million ...); values need entering by hand")  # fmt: skip
    for col in range(ncols):
        dated = first.column_dates[col]
        why_date = f"column dated {dated:%d %b %Y} in the header" if dated else ""
        factor = 1.0
        if dated is None:
            if fiscal_year_end is None:
                out.warnings.append(f"{name}: column {col + 1} has no date and the report's "
                                    "fiscal year is unknown; skipped")  # fmt: skip
                found.column_dates.append(None)
                found.checks.append("none")
                continue
            dated = _month_end(fiscal_year_end.year - col, fiscal_year_end.month)
            factor *= conf.no_header_dates_factor
            why_date = f"column {col + 1} not dated: taken as FY ending {dated:%d %b %Y}"
        found.column_dates.append(dated)
        picked, checks = _assign(matched, col, labels, statement, conf)
        status, why_check = _cross_check(statement, picked, checks, conf.check_tolerance_rel)
        found.checks.append(status)
        check_factor = {"passed": 1.0, "failed": conf.check_failed_factor,
                        "none": conf.no_check_factor}[status]  # fmt: skip
        for code, p in picked.items():
            reasons = [*p.reasons, why_date, f"cross-check {status}: {why_check}"]
            c = p.confidence * factor * check_factor
            if method == "camelot":
                c *= conf.camelot_factor
                reasons.append("read by the camelot fallback")
            value = abs(p.value) if xmap.items[code].magnitude else p.value
            if unit is not None:
                reasons.append(f"unit: {unit_text!r} → x{unit:g}")
            else:
                reasons.append("no unit stated: enter the value by hand")
            out.values.append(ExtractedValue(
                statement=statement, basis=basis, period_end=dated,
                period_type=PERIOD_TYPE[statement], item_code=code,
                value_inr=None if unit is None else value * unit, raw_value=p.value,
                raw_label=" + ".join(rm.row.label or rm.label for rm in p.rows)[:512],
                pages=sorted({rm.row.page for rm in p.rows}), method=method,
                confidence=round(min(max(c, 0.0), 1.0), 4), reasons=[r for r in reasons if r],
            ))  # fmt: skip
    out.statements.append(found)


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def describe(out: AnnualReportExtraction, auto_accept: float) -> str:
    """Human-readable summary of an extraction (``python -m app.jobs pdf-inspect``)."""
    lines = [f"{out.page_count} pages; pdf_labels.yaml version {out.labels_version}"]
    for s in out.statements:
        dates = ", ".join(d.isoformat() if d else "?" for d in s.column_dates)
        lines.append(f"\n{s.basis} {STATEMENT_NAMES[s.statement]}: pages {s.pages} "
                     f"({s.method}); unit {s.unit_text!r}; columns {dates}; "
                     f"cross-checks {s.checks}")  # fmt: skip
        for v in out.values:
            if (v.statement, v.basis) != (s.statement, s.basis):
                continue
            mark = "  " if v.confidence >= auto_accept and v.value_inr is not None else "? "
            crore = "—" if v.value_inr is None else f"{v.value_inr / 1e7:,.2f} cr"
            lines.append(f"  {mark}{v.period_end} {v.item_code:<26} {crore:>16} "
                         f"{v.confidence:.2f}  {v.raw_label[:60]}")  # fmt: skip
    if out.warnings:
        lines.append("\nwarnings:")
        lines += [f"  - {w}" for w in out.warnings]
    lines.append("\n'?' = below auto_accept or without a unit: goes to the review queue")
    return "\n".join(lines)
