"""LLM thesis, the I/O part (SPEC §8a): the model client, storage and lookup.

The default provider is Google Gemini (key ``GEMINI_API_KEY``, model name in jobs.yaml, calls
paced by ``core/rate_limiter.py``); a local Ollama server (``THESIS_LLM_URL``) stays optional,
and in development the built-in :class:`FakeModel` stands in. Only the computed report numbers
and reasons are sent (``reports/thesis.py``: ``facts_json``).
:func:`generate` writes a thesis for a stock's latest report: it builds the fact sheet
(``reports/thesis.py``), asks the model, checks the draft, retries with the problems listed,
and stores the outcome in ``report_theses`` under the fact sheet's digest. A thesis is shown
only next to the exact numbers it was written from (:func:`attach`).
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Provider, ThesisConfig
from app.core.rate_limiter import Limiter, RateLimitTimeout
from app.core.settings import Settings
from app.db.models import Instrument, ReportThesis
from app.db.upsert import upsert
from app.reports.dto import StockReport
from app.reports.service import latest_report
from app.reports.thesis import (
    DISCLAIMER,
    PROMPT_VERSION,
    FactSheet,
    build_prompt,
    check,
    clean_draft,
    fact_sheet,
    retry_prompt,
    sheet_summary,
)

Status = Literal["ok", "rejected", "failed", "missing", "disabled"]


logger = logging.getLogger(__name__)
GEMINI_KEY_MISSING = "Add GEMINI_API_KEY to .env"


class ModelUnavailable(Exception):
    """The model server could not be reached or gave no answer."""


class RateLimited(ModelUnavailable):
    """The provider answered HTTP 429, or our own rate limiter had no token in time."""


class TextModel(Protocol):
    name: str

    def generate(self, prompt: str, cfg: ThesisConfig) -> str: ...


class OllamaModel:
    """``POST /api/generate`` on a local Ollama server (non-streaming)."""

    def __init__(self, base_url: str, model: str, http: requests.Session | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.name = model
        self._http = http or requests.Session()

    def generate(self, prompt: str, cfg: ThesisConfig) -> str:
        body = {
            "model": self.name,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": cfg.temperature, "seed": cfg.seed},
        }
        try:
            resp = self._http.post(f"{self.base_url}/api/generate", json=body,
                                   timeout=cfg.timeout_s)  # fmt: skip
        except requests.RequestException as exc:
            raise ModelUnavailable(f"model server unreachable ({type(exc).__name__})") from exc
        if resp.status_code != 200:
            detail = resp.text[:200].replace("\n", " ")
            raise ModelUnavailable(f"model server answered HTTP {resp.status_code}: {detail}")
        try:
            text = resp.json()["response"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ModelUnavailable("model server gave no 'response' field") from exc
        if not isinstance(text, str) or not text.strip():
            raise ModelUnavailable("model returned an empty answer")
        return text


class GeminiModel:
    """Google Gemini ``generateContent`` over HTTPS. The key goes only in the ``x-goog-api-key``
    header (never in the URL) and is scrubbed from every message; each call takes a token from
    the shared rate limiter first."""

    def __init__(
        self,
        api_key: str,
        cfg: ThesisConfig,
        *,
        limiter: Limiter | None = None,
        http: requests.Session | None = None,
    ) -> None:
        self._key = api_key
        self.name = cfg.model
        self._base = cfg.gemini_base_url.rstrip("/")
        self._limiter = limiter
        self._http = http or requests.Session()

    def _scrub(self, text: str) -> str:
        return text.replace(self._key, "***") if self._key else text

    def generate(self, prompt: str, cfg: ThesisConfig) -> str:
        if self._limiter is not None:
            try:
                self._limiter.acquire(Provider.GEMINI, timeout=cfg.rate_limit_timeout_s)
            except RateLimitTimeout as exc:
                raise RateLimited(f"Gemini request pacing: {exc}") from None
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": cfg.temperature,
                "maxOutputTokens": cfg.max_output_tokens,
            },
        }
        url = f"{self._base}/models/{self.name}:generateContent"
        try:
            resp = self._http.post(url, json=body, headers={"x-goog-api-key": self._key},
                                   timeout=cfg.timeout_s)  # fmt: skip
        except requests.RequestException as exc:
            raise ModelUnavailable(f"Gemini unreachable ({type(exc).__name__})") from None
        status = resp.status_code
        if status == 429:
            raise RateLimited("Gemini is rate-limiting requests (HTTP 429): try again in a minute")
        if status in (400, 401, 403):
            raise ModelUnavailable(
                f"Gemini rejected the request (HTTP {status}): check GEMINI_API_KEY"
            )
        if status != 200:
            raise ModelUnavailable(f"Gemini answered HTTP {status}")
        try:
            parts = resp.json()["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts)
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ModelUnavailable("Gemini gave no text (blocked or empty answer)") from None
        if not text.strip():
            raise ModelUnavailable("Gemini returned an empty answer")
        return self._scrub(text)


_FACT_LINE = re.compile(r"^- ([^:]+): (.+)$", re.MULTILINE)


class FakeModel:
    """Development / tests only (``THESIS_LLM_URL=fake``): a deterministic stand-in that writes
    the five parts from the prompt's own JSON facts, so the whole flow runs without a model."""

    name = "fake"

    def generate(self, prompt: str, cfg: ThesisConfig) -> str:
        data, _ = json.JSONDecoder().raw_decode(prompt.split("FACTS_JSON\n", 1)[-1])
        facts: dict[str, str] = data["facts"]
        reasons: list[str] = data["reasons"]

        def f(label: str) -> str:
            return facts.get(label, "not available")

        risk = facts.get("Red flags", "none")
        risk = risk if risk != "none" else (reasons[0] if reasons else "none stated")
        text = (
            f"Business quality: {f('Company')} scores {f('Quality score')} on quality and "
            f"{f('Growth score')} on growth, with health at {f('Health score')} and governance at "
            f"{f('Governance score')}; the total score is {f('Total score')} and the grade is "
            f"{f('Grade')}.\n"
            f"Valuation vs fair value: the price is {f('Current price (CMP)')} against a fair "
            f"value of {f('Fair value')}, a baseline of {f('Baseline (floor)')} and a top band of "
            f"{f('Top band (premium ceiling)')}, which places it in the {f('Zone')} zone.\n"
            f"Technical set-up: the stock is in {f('Technical stage')} with trend {f('Trend')} "
            f"and a relative-strength percentile of {f('Relative-strength percentile')}.\n"
            f"Key risks: {risk}. Red flags: {f('Red flags')}.\n"
            f"What would change the view: a different fair value than {f('Fair value')}, a "
            f"price move away from {f('Current price (CMP)')}, or a change in the scores above; "
            f"the report's action is {f('Action')}."
        )
        extra = [f"{k} {v}" for k, v in facts.items() if k not in text]
        while len(text.split()) < cfg.min_words and extra:
            text += f" {extra.pop(0)}."
        return text


def _gemini_key(settings: Settings) -> str | None:
    key = settings.gemini_api_key.get_secret_value().strip() if settings.gemini_api_key else ""
    return key or None


def build_model(
    settings: Settings, cfg: ThesisConfig, limiter: Limiter | None = None
) -> TextModel | None:
    """The configured model, or None when the thesis is off or the provider is not set up."""
    if not cfg.enabled:
        return None
    if settings.thesis_llm_url == "fake":
        return FakeModel()
    if cfg.provider == "ollama":
        if not settings.thesis_llm_url:
            return None
        return OllamaModel(settings.thesis_llm_url, cfg.ollama_model)
    key = _gemini_key(settings)
    return GeminiModel(key, cfg, limiter=limiter) if key else None


def disabled_reason(settings: Settings, cfg: ThesisConfig) -> str | None:
    if not cfg.enabled:
        return "the thesis generator is off (jobs.yaml → thesis.enabled)"
    if settings.thesis_llm_url == "fake":
        return None
    if cfg.provider == "ollama":
        if not settings.thesis_llm_url:
            return "no local model configured (THESIS_LLM_URL, e.g. a local Ollama)"
        return None
    if _gemini_key(settings) is None:
        return GEMINI_KEY_MISSING
    return None


@dataclass(frozen=True)
class ThesisView:
    symbol: str
    as_of: str
    status: Status
    text: str | None = None
    model: str | None = None
    generated_at: datetime | None = None
    attempts: int = 0
    problems: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def _instrument_id(session: Session, symbol: str) -> int | None:
    return session.scalar(select(Instrument.id).where(Instrument.symbol == symbol.upper()))


def _row(session: Session, iid: int, digest: str) -> ReportThesis | None:
    return session.scalar(
        select(ReportThesis).where(ReportThesis.instrument_id == iid, ReportThesis.digest == digest)
    )


def _view(report: StockReport, row: ReportThesis | None, reason: str | None) -> ThesisView:
    if row is None:
        status: Status = "disabled" if reason else "missing"
        why = reason or "not written for these numbers yet"
        return ThesisView(report.symbol, report.as_of.isoformat(), status, reasons=[why])
    reasons = {
        "ok": ["every number in the text matches the report's facts"],
        "rejected": [f"no draft passed the check in {row.attempts} attempt(s)"],
        "failed": ["the model could not be reached", *row.problems[:1]],
    }.get(row.status, [])
    if reason:
        reasons.append(reason)
    return ThesisView(
        report.symbol,
        report.as_of.isoformat(),
        row.status,  # type: ignore[arg-type]
        text=row.text if row.status == "ok" else None,
        model=row.model,
        generated_at=row.updated_at,
        attempts=row.attempts,
        problems=list(row.problems),
        reasons=reasons,
    )


def thesis_view(
    session: Session, symbol: str, cfg: ThesisConfig, disabled: str | None
) -> ThesisView | None:
    """The thesis for the latest report's numbers (None if the stock has no report)."""
    report = latest_report(session, symbol)
    iid = _instrument_id(session, symbol)
    if report is None or iid is None:
        return None
    return _view(report, _row(session, iid, fact_sheet(report, cfg).digest()), disabled)


def attach(session: Session, report: StockReport, cfg: ThesisConfig) -> StockReport:
    """``report`` with ``thesis`` set when one was written (and passed) for these numbers."""
    iid = _instrument_id(session, report.symbol)
    if iid is None:
        return report
    row = _row(session, iid, fact_sheet(report, cfg).digest())
    if row is None or row.status != "ok":
        return report
    return report.model_copy(update={"thesis": row.text})


def _draft(
    model: TextModel, sheet: FactSheet, cfg: ThesisConfig
) -> tuple[str, str | None, list[str], int]:
    """(status, text, problems, attempts) after up to ``max_attempts`` drafts."""
    prompt = build_prompt(sheet, cfg)
    problems: list[str] = []
    for attempt in range(1, cfg.max_attempts + 1):
        try:
            draft = clean_draft(model.generate(prompt, cfg))
        except ModelUnavailable as exc:
            return "failed", None, [str(exc)], attempt
        result = check(draft, sheet, cfg)
        if result.ok:
            return "ok", f"{draft}\n\n{DISCLAIMER}", [], attempt
        problems = result.problems
        prompt = retry_prompt(sheet, cfg, draft, problems)
    return "rejected", None, problems, cfg.max_attempts


def generate(
    session: Session,
    symbol: str,
    cfg: ThesisConfig,
    model: TextModel,
    *,
    now: datetime,
    force: bool = False,
) -> ThesisView:
    """Write (or reuse) the thesis for the stock's latest report. A thesis that passed for the
    same digest is reused unless ``force``. Commits. Raises LookupError without a report."""
    report = latest_report(session, symbol)
    iid = _instrument_id(session, symbol)
    if report is None or iid is None:
        raise LookupError(f"no report for {symbol.upper()}")
    sheet = fact_sheet(report, cfg)
    digest = sheet.digest()
    existing = _row(session, iid, digest)
    if existing is not None and existing.status == "ok" and not force:
        return _view(report, existing, None)
    status, text, problems, attempts = _draft(model, sheet, cfg)
    upsert(
        session,
        ReportThesis,
        [{"instrument_id": iid, "digest": digest, "as_of": report.as_of, "status": status,
          "text": text, "model": model.name, "prompt_version": PROMPT_VERSION,
          "attempts": attempts, "problems": problems, "facts": sheet_summary(sheet),
          "updated_at": now}],
    )  # fmt: skip
    session.commit()
    row = _row(session, iid, digest)
    return _view(report, row, None)
