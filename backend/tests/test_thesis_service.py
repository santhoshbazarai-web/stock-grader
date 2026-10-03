"""LLM thesis, I/O part (SPEC §8a): Ollama client, generate/retry/reject, storage by digest,
the API and the nightly job. No real model: a scripted one, the built-in fake, and HTTP mocks."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import requests
import responses
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import AppConfig, JobName, Provider, ThesisConfig, get_config, load_config
from app.core.rate_limiter import RateLimitTimeout
from app.core.settings import get_settings
from app.db.models import Instrument, Report, ReportThesis
from app.jobs.registry import REGISTRY
from app.jobs.runner import JobOptions, run_job
from app.reports.service import latest_report, refresh_report
from app.reports.thesis import DISCLAIMER, build_prompt, fact_sheet
from app.reports.thesis_service import (
    FakeModel,
    GeminiModel,
    ModelUnavailable,
    OllamaModel,
    attach,
    build_model,
    disabled_reason,
    generate,
    thesis_view,
)
from tests.api_support import app_client
from tests.conftest import REPO_CONFIG_DIR
from tests.jobs_support import Env, watch
from tests.report_support import seed_company, seed_index

CONFIG = load_config(REPO_CONFIG_DIR)
CFG = CONFIG.jobs.thesis
NOW = datetime(2026, 10, 1, 4, 0, tzinfo=UTC)
URL = "http://127.0.0.1:11434"


class Scripted:
    """Answers with the given drafts in turn (a draft may be an exception to raise)."""

    name = "scripted"

    def __init__(self, *drafts: str | Exception) -> None:
        self.drafts = list(drafts)
        self.prompts: list[str] = []

    def generate(self, prompt: str, cfg: ThesisConfig) -> str:
        self.prompts.append(prompt)
        d = self.drafts.pop(0)
        if isinstance(d, Exception):
            raise d
        return d


@pytest.fixture
def seeded(db: Session) -> Session:
    seed_index(db)
    seed_company(db)
    refresh_report(db, "SYNTH", CONFIG)
    db.commit()
    return db


def shown(draft: str) -> str:
    """What is stored and shown: the checked draft plus the fixed disclaimer."""
    return f"{draft}\n\n{DISCLAIMER}"


def _good(db: Session) -> str:
    """The fake model's paragraph for SYNTH: built from the facts, so it passes the check."""
    report = latest_report(db, "SYNTH")
    assert report is not None
    return FakeModel().generate(build_prompt(fact_sheet(report, CFG), CFG), CFG)


# ───────────────────────── Ollama client ─────────────────────────


@responses.activate
def test_ollama_client_posts_a_deterministic_request() -> None:
    responses.post(f"{URL}/api/generate", json={"response": " A paragraph. ", "done": True})
    assert OllamaModel(URL + "/", CFG.ollama_model).generate("PROMPT", CFG) == " A paragraph. "
    body = responses.calls[0].request.body
    assert body is not None
    sent = json.loads(body)
    assert sent == {"model": "llama3.1:8b", "prompt": "PROMPT", "stream": False,
                    "options": {"temperature": CFG.temperature, "seed": CFG.seed}}  # fmt: skip


@responses.activate
@pytest.mark.parametrize(
    ("reply", "message"),
    [
        ({"status": 404, "json": {"error": "model 'x' not found"}}, "HTTP 404"),
        ({"json": {"done": True}}, "no 'response'"),
        ({"json": {"response": "  "}}, "empty answer"),
        ({"body": requests.ConnectionError("refused")}, "unreachable"),
    ],
)
def test_ollama_client_failures(reply: dict[str, object], message: str) -> None:
    responses.post(f"{URL}/api/generate", **reply)  # type: ignore[arg-type]
    with pytest.raises(ModelUnavailable, match=message):
        OllamaModel(URL, "x").generate("PROMPT", CFG)


# ───────────────────────── generate / store / attach ─────────────────────────


def test_generate_stores_a_checked_thesis_and_reuses_it(seeded: Session) -> None:
    good = _good(seeded)
    model = Scripted(good)
    view = generate(seeded, "SYNTH", CFG, model, now=NOW)
    assert view.status == "ok" and view.text == shown(good) and view.attempts == 1
    row = seeded.scalar(select(ReportThesis))
    assert row is not None and row.model == "scripted" and row.facts["symbol"] == "SYNTH"
    # same numbers: reused without calling the model again
    assert generate(seeded, "SYNTH", CFG, Scripted(), now=NOW).text == shown(good)
    report = latest_report(seeded, "SYNTH")
    assert report is not None and attach(seeded, report, CFG).thesis == shown(good)
    # force: written again
    assert generate(seeded, "SYNTH", CFG, Scripted(good), now=NOW, force=True).status == "ok"


def test_a_rejected_draft_is_retried_with_its_problems(seeded: Session) -> None:
    good = _good(seeded)
    model = Scripted(good + " Its price could double to 9,999 rupees.", good)
    view = generate(seeded, "SYNTH", CFG, model, now=NOW)
    assert view.status == "ok" and view.attempts == 2 and view.text == shown(good)
    assert "numbers not in the facts: 9,999" in model.prompts[1]


def test_every_draft_rejected(seeded: Session) -> None:
    bad = _good(seeded) + " Expect a target price of 12,345."
    view = generate(seeded, "SYNTH", CFG, Scripted(*[bad] * CFG.max_attempts), now=NOW)
    assert view.status == "rejected" and view.text is None
    assert any("numbers not in the facts" in p for p in view.problems)
    assert any("forbidden phrases: target price" in p for p in view.problems)
    report = latest_report(seeded, "SYNTH")
    assert report is not None and attach(seeded, report, CFG).thesis is None


def test_model_unreachable(seeded: Session) -> None:
    view = generate(seeded, "SYNTH", CFG, Scripted(ModelUnavailable("model server unreachable")),
                    now=NOW)  # fmt: skip
    assert view.status == "failed" and view.problems == ["model server unreachable"]


def test_thesis_is_not_shown_against_new_numbers(seeded: Session) -> None:
    generate(seeded, "SYNTH", CFG, Scripted(_good(seeded)), now=NOW)
    assert thesis_view(seeded, "SYNTH", CFG, None).status == "ok"  # type: ignore[union-attr]
    # a newer report with a different price: the stored thesis no longer applies
    old = seeded.scalars(select(Report).order_by(Report.as_of.desc())).first()
    assert old is not None
    payload = {**old.payload, "cmp": old.payload["cmp"] * 1.05,
               "as_of": (old.as_of + timedelta(days=1)).isoformat()}  # fmt: skip
    seeded.add(Report(instrument_id=old.instrument_id, as_of=old.as_of + timedelta(days=1),
                      payload=payload, sources=old.sources, computed_at=NOW))  # fmt: skip
    seeded.flush()
    view = thesis_view(seeded, "SYNTH", CFG, None)
    assert view is not None and view.status == "missing" and view.text is None
    report = latest_report(seeded, "SYNTH")
    assert report is not None and attach(seeded, report, CFG).thesis is None


def test_no_report(db: Session) -> None:
    with pytest.raises(LookupError):
        generate(db, "NOSUCH", CFG, Scripted(), now=NOW)
    assert thesis_view(db, "NOSUCH", CFG, None) is None


# ───────────────────────── API ─────────────────────────


def _enabled() -> AppConfig:
    thesis = CONFIG.jobs.thesis.model_copy(update={"enabled": True})
    return CONFIG.model_copy(update={"jobs": CONFIG.jobs.model_copy(update={"thesis": thesis})})


@pytest.fixture
def client(seeded: Session, redis_client: Redis) -> Iterator[TestClient]:
    redis_client.delete("auth:fail:testclient")
    yield from app_client(seeded)


def test_api_when_off(client: TestClient) -> None:
    res = client.get("/api/stocks/SYNTH/thesis")
    assert res.status_code == 200
    body = res.json()
    assert body["enabled"] is False and body["status"] == "disabled" and body["text"] is None
    assert body["reasons"][0] == "Add GEMINI_API_KEY to .env"
    res = client.post("/api/stocks/SYNTH/thesis")
    assert res.status_code == 409 and res.json()["detail"] == "Add GEMINI_API_KEY to .env"
    assert client.get("/api/stocks/NOSUCH/thesis").status_code == 404


def test_api_writes_and_shows_the_thesis(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.app.dependency_overrides[get_config] = _enabled  # type: ignore[attr-defined]
    res = client.post("/api/stocks/SYNTH/thesis")  # enabled but no key
    assert res.status_code == 409 and "GEMINI_API_KEY" in res.json()["detail"]
    monkeypatch.setenv("THESIS_LLM_URL", "fake")
    get_settings.cache_clear()
    assert client.get("/api/stocks/SYNTH/thesis").json()["status"] == "missing"
    res = client.post("/api/stocks/SYNTH/thesis")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "ok" and body["model"] == "fake" and "SYNTH" in body["text"]
    assert client.get("/api/stocks/SYNTH/thesis").json()["text"] == body["text"]
    assert client.get("/api/stocks/SYNTH/report").json()["thesis"] == body["text"]


# ───────────────────────── job ─────────────────────────


def test_thesis_job(env: Env) -> None:
    with env.session() as s:
        seed_index(s)
        seed_company(s)
        refresh_report(s, "SYNTH", env.ctx.config)
        watch(s, s.scalar(select(Instrument.id).where(Instrument.symbol == "SYNTH")))
        s.commit()
    spec = REGISTRY[JobName.THESIS]
    # off by default: skipped, no model built
    off = run_job(spec, env.ctx, JobOptions()).outcome
    assert off is not None and off.skipped_reason is not None
    assert "GEMINI_API_KEY" in off.skipped_reason
    env.ctx.thesis_model = FakeModel()
    done = run_job(spec, env.ctx, JobOptions(symbols=["SYNTH"])).outcome
    assert done is not None and done.rows_written == 1
    assert done.details["outcomes"] == {"ok": 1}
    # nothing changed: the stored thesis is reused (still ok, no new text)
    again = run_job(spec, env.ctx, JobOptions(symbols=["SYNTH"])).outcome
    assert again is not None and again.details["outcomes"] == {"ok": 1}
    with env.session() as s:
        assert s.scalar(select(ReportThesis.status)) == "ok"


# ───────────────────────── Gemini ─────────────────────────

GEMINI_URL = f"{CFG.gemini_base_url}/models/{CFG.model}:generateContent"
KEY = "AIza-test-key-0123456789"


class FakeLimiter:
    def __init__(self, refuse: bool = False) -> None:
        self.calls: list[object] = []
        self.refuse = refuse

    def acquire(self, provider: object, *, timeout: float) -> None:
        self.calls.append(provider)
        if self.refuse:
            raise RateLimitTimeout("no token")


def reply(text: str) -> dict[str, object]:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def gemini(limiter: FakeLimiter | None = None) -> GeminiModel:
    return GeminiModel(KEY, CFG, limiter=limiter)


@responses.activate
def test_gemini_success_sends_only_the_json_facts_and_paces_the_call(seeded: Session) -> None:
    good = _good(seeded)
    responses.post(GEMINI_URL, json=reply("**Business quality:** " + good))
    limiter = FakeLimiter()
    view = generate(seeded, "SYNTH", CFG, gemini(limiter), now=NOW)
    assert view.status == "ok" and view.model == CFG.model and view.text.endswith(DISCLAIMER)  # type: ignore[union-attr]
    assert limiter.calls == [Provider.GEMINI]
    req = responses.calls[0].request
    assert req.headers["x-goog-api-key"] == KEY and KEY not in req.url  # header only, never the URL
    sent = json.loads(req.body)["contents"][0]["parts"][0]["text"]  # type: ignore[arg-type]
    assert "FACTS_JSON" in sent and '"facts"' in sent and '"reasons"' in sent
    assert "Business quality:" in sent and "150 to 250 words" in sent
    assert json.loads(req.body)["generationConfig"]["maxOutputTokens"] == CFG.max_output_tokens  # type: ignore[arg-type]
    # cached per (symbol, report hash): the same numbers are not sent again
    generate(seeded, "SYNTH", CFG, gemini(limiter), now=NOW)
    assert len(responses.calls) == 1
    # Regenerate (force) asks again
    generate(seeded, "SYNTH", CFG, gemini(limiter), now=NOW, force=True)
    assert len(responses.calls) == 2


@responses.activate
def test_gemini_429_is_reported_and_not_cached_as_a_thesis(seeded: Session) -> None:
    responses.post(GEMINI_URL, status=429, json={"error": {"message": "quota"}})
    view = generate(seeded, "SYNTH", CFG, gemini(), now=NOW)
    assert view.status == "failed" and view.text is None
    assert "HTTP 429" in view.problems[0] and view.attempts == 1  # no retry loop on a 429
    # a later success replaces it
    responses.replace(responses.POST, GEMINI_URL, json=reply(_good(seeded)))
    assert generate(seeded, "SYNTH", CFG, gemini(), now=NOW).status == "ok"


@responses.activate
def test_gemini_bad_number_is_retried_once_then_unavailable(seeded: Session) -> None:
    bad = _good(seeded) + " Revenue could reach 98765 crore."
    responses.post(GEMINI_URL, json=reply(bad))
    view = generate(seeded, "SYNTH", CFG, gemini(), now=NOW)
    assert (
        view.status == "rejected" and view.text is None and view.attempts == CFG.max_attempts == 2
    )
    assert len(responses.calls) == 2 and any("98765" in p for p in view.problems)
    retry = json.loads(responses.calls[1].request.body)["contents"][0]["parts"][0]["text"]  # type: ignore[arg-type]
    assert "numbers not in the facts: 98765" in retry
    # one good retry rescues it
    responses.replace(responses.POST, GEMINI_URL, json=reply(_good(seeded)))
    assert generate(seeded, "SYNTH", CFG, gemini(), now=NOW, force=True).status == "ok"


@responses.activate
@pytest.mark.parametrize(
    ("status", "body", "detail"),
    [(403, {}, "HTTP 403"), (500, {}, "HTTP 500"), (200, {"candidates": []}, "no text"),
     (200, {"promptFeedback": {"blockReason": "SAFETY"}}, "no text")],
)  # fmt: skip
def test_gemini_other_failures(
    seeded: Session, status: int, body: dict[str, object], detail: str
) -> None:
    responses.post(GEMINI_URL, status=status, json=body)
    view = generate(seeded, "SYNTH", CFG, gemini(), now=NOW)
    assert view.status == "failed" and detail in view.problems[0] and KEY not in str(view)


def test_gemini_our_rate_limiter_can_refuse(seeded: Session) -> None:
    view = generate(seeded, "SYNTH", CFG, gemini(FakeLimiter(refuse=True)), now=NOW)
    assert view.status == "failed" and "pacing" in view.problems[0]


def test_gemini_network_error_hides_the_key(seeded: Session) -> None:
    with responses.RequestsMock() as rsps:
        rsps.post(GEMINI_URL, body=requests.ConnectionError(f"boom {KEY}"))
        view = generate(seeded, "SYNTH", CFG, gemini(), now=NOW)
    assert view.status == "failed" and KEY not in " ".join(view.problems)


def test_missing_key_is_a_clear_reason_and_no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("THESIS_LLM_URL", raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    assert disabled_reason(settings, CFG) == "Add GEMINI_API_KEY to .env"
    assert build_model(settings, CFG) is None
    monkeypatch.setenv("GEMINI_API_KEY", "  ")  # blank is missing too
    get_settings.cache_clear()
    assert disabled_reason(get_settings(), CFG) == "Add GEMINI_API_KEY to .env"
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    get_settings.cache_clear()
    settings = get_settings()
    assert disabled_reason(settings, CFG) is None
    assert isinstance(build_model(settings, CFG), GeminiModel)
    assert KEY not in repr(settings)  # a SecretStr: never printed or logged
    ollama = CFG.model_copy(update={"provider": "ollama"})
    assert disabled_reason(settings, ollama) is not None and build_model(settings, ollama) is None


def test_api_card_says_add_the_key_then_writes_with_gemini(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, seeded: Session
) -> None:
    client.app.dependency_overrides[get_config] = _enabled  # type: ignore[attr-defined]
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    get_settings.cache_clear()
    body = client.get("/api/stocks/SYNTH/thesis").json()
    assert body["status"] == "disabled" and body["reasons"][0] == "Add GEMINI_API_KEY to .env"
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    get_settings.cache_clear()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add_passthru("http://testserver")
        rsps.post(GEMINI_URL, json=reply(_good(seeded)))
        res = client.post("/api/stocks/SYNTH/thesis")
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "ok" and res.json()["text"].endswith(DISCLAIMER)
        again = client.post("/api/stocks/SYNTH/thesis?force=true")
        assert again.status_code == 200 and len(rsps.calls) == 2
    assert KEY not in res.text
