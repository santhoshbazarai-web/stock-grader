"""SPEC §8 endpoints (Postgres + Redis): schemas, status codes, persistence, OpenAPI."""

import shutil
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_config
from app.core.settings import get_settings
from app.db.enums import AliasKind, FilingStatus, SymbolStatus
from app.db.models import (
    Backtest,
    FinAnnual,
    Instrument,
    JobRun,
    ResultFiling,
    Symbol,
    SymbolAlias,
    UserOverride,
)
from app.main import create_app
from tests.api_support import app_client
from tests.conftest import REPO_CONFIG_DIR
from tests.report_support import (
    ensure_instrument,
    seed_company,
    seed_index,
    store_prices,
    synthetic_daily,
)

SCREENER_FIXTURE = Path(__file__).parent / "fixtures" / "screener" / "sample_export.xlsx"
XBRL_FIX = Path(__file__).parent / "fixtures" / "xbrl"


@pytest.fixture
def client(db: Session, redis_client: Redis) -> Iterator[TestClient]:
    redis_client.delete("auth:fail:testclient")
    yield from app_client(db)


@pytest.fixture
def seeded(db: Session) -> Session:
    seed_index(db)
    seed_company(db, "SYNTH")
    seed_company(db, "BANKCO", sector="banks", seed=13)
    return db


# ───────────────────────── OpenAPI ─────────────────────────

SPEC_PATHS = {
    ("get", "/api/stocks/search"),
    ("get", "/api/stocks/{symbol}/report"),
    ("post", "/api/stocks/{symbol}/refresh"),
    ("get", "/api/stocks/{symbol}/valuation/sensitivity"),
    ("post", "/api/stocks/{symbol}/overrides"),
    ("get", "/api/screener"),
    ("get", "/api/watchlist"),
    ("post", "/api/watchlist"),
    ("delete", "/api/watchlist/{symbol}"),
    ("get", "/api/alerts"),
    ("post", "/api/alerts"),
    ("delete", "/api/alerts/{alert_id}"),
    ("post", "/api/uploads/screener"),
    ("post", "/api/uploads/xbrl"),
    ("get", "/api/filings"),
    ("get", "/api/filings/summary"),
    ("post", "/api/filings/{filing_id}/retry"),
    ("get", "/api/brokers/status"),
    ("get", "/api/brokers/fyers/login"),
    ("get", "/api/brokers/fyers/callback"),
    ("get", "/api/brokers/kite/login"),
    ("get", "/api/brokers/kite/callback"),
    ("get", "/api/config"),
    ("put", "/api/config"),
    ("post", "/api/backtests"),
    ("get", "/api/backtests"),
    ("get", "/api/backtests/{backtest_id}"),
    ("get", "/api/jobs"),
    # SPEC v0.2 §3.6 steps 3-4: annual-report PDFs, review queue, coverage grid
    ("post", "/api/uploads/annual-report"),
    ("get", "/api/annual-reports"),
    ("post", "/api/annual-reports/{report_id}/reparse"),
    ("get", "/api/annual-reports/{report_id}/document"),
    ("get", "/api/review/annual-reports"),
    ("get", "/api/review/annual-reports/summary"),
    ("post", "/api/review/annual-reports/{candidate_id}"),
    ("get", "/api/stocks/{symbol}/coverage"),
    # SPEC v0.2 §3.5: aliases the search finds a stock by
    ("get", "/api/stocks/{symbol}/aliases"),
    # SPEC v0.2 §3.7: on-demand pipeline
    ("post", "/api/pipeline"),
    ("get", "/api/pipeline"),
    ("get", "/api/pipeline/{run_id}"),
    ("get", "/api/pipeline/{run_id}/events"),
    ("post", "/api/stocks/{symbol}/aliases"),
    ("delete", "/api/stocks/{symbol}/aliases/{alias_id}"),
}


def test_openapi_documents_every_spec_endpoint() -> None:
    schema = create_app().openapi()
    have = {(m, p) for p, ops in schema["paths"].items() for m in ops}
    assert SPEC_PATHS <= have
    schemes = schema["components"]["securitySchemes"]
    assert {"APIKeyCookie", "HTTPBearer"} <= set(schemes)
    report = schema["paths"]["/api/stocks/{symbol}/report"]["get"]
    ref = report["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("/StockReport")
    for field in ("levels", "zone", "buy_zone", "scores", "grade", "action", "reasons",
                  "red_flags", "data_gaps", "thesis", "valuation", "earned_premium"):  # fmt: skip
        assert field in schema["components"]["schemas"]["StockReport"]["properties"], field
    # Every protected operation declares the security schemes and a 401.
    for path, ops in schema["paths"].items():
        for method, op in ops.items():
            public = path in (
                "/api/health",
                "/api/auth/login",
                "/api/auth/logout",
            ) or path.endswith("/callback")
            if not public:
                assert op.get("security"), (method, path)


def test_docs_are_served(client: TestClient) -> None:
    assert client.get("/api/docs").status_code == 200
    assert client.get("/api/openapi.json").json()["info"]["title"] == "Stock Grader API"


# ───────────────────────── stocks ─────────────────────────


def test_search(client: TestClient, seeded: Session) -> None:
    res = client.get("/api/stocks/search", params={"q": "syn"}).json()
    assert [r["symbol"] for r in res] == ["SYNTH"]
    assert res[0]["match"] in ("symbol", "name") and not res[0]["exact"]
    exact = client.get("/api/stocks/search", params={"q": "synth"}).json()[0]
    assert (exact["symbol"], exact["exact"], exact["match"]) == ("SYNTH", True, "symbol")
    by_name = client.get("/api/stocks/search", params={"q": "Ltd"}).json()
    assert {r["symbol"] for r in by_name} == {"SYNTH", "BANKCO"}  # indices excluded
    with_idx = client.get("/api/stocks/search", params={"q": "NIFTY", "include_indices": True})
    assert [r["symbol"] for r in with_idx.json()] == ["NIFTY500"]
    assert client.get("/api/stocks/search", params={"q": ""}).status_code == 422
    assert client.get("/api/stocks/search", params={"q": "%"}).json() == []


def test_aliases(client: TestClient, seeded: Session) -> None:
    iid = seeded.scalar(select(Instrument.id).where(Instrument.symbol == "SYNTH"))
    assert client.get("/api/stocks/SYNTH/aliases").status_code == 404  # not in the master yet
    seeded.add(Symbol(isin="INE000S01010", name="Synthetic Ltd", nse_symbol="SYNTH",
                      instrument_id=iid, status=SymbolStatus.ACTIVE, sources=["nse"]))  # fmt: skip
    seeded.commit()
    made = client.post("/api/stocks/synth/aliases", json={"alias": "  Syntho   Corp "})
    assert made.status_code == 201 and made.json()["alias"] == "Syntho Corp"
    assert made.json()["kind"] == "user"
    again = client.post("/api/stocks/SYNTH/aliases", json={"alias": "Syntho Corp"})
    assert again.json()["id"] == made.json()["id"]  # idempotent
    hit = client.get("/api/stocks/search", params={"q": "syntho corp"}).json()[0]
    assert (hit["symbol"], hit["match"], hit["matched"]) == ("SYNTH", "user", "Syntho Corp")
    listed = client.get("/api/stocks/SYNTH/aliases").json()
    assert [a["alias"] for a in listed] == ["Syntho Corp"]
    sid = seeded.scalar(select(Symbol.id))
    seeded.add(SymbolAlias(symbol_id=sid, alias="Old Synth", kind=AliasKind.FORMER_NAME,
                           source="nse"))  # fmt: skip
    seeded.commit()
    old = seeded.scalar(select(SymbolAlias.id).where(SymbolAlias.alias == "Old Synth"))
    assert client.delete(f"/api/stocks/SYNTH/aliases/{old}").status_code == 409
    assert client.delete(f"/api/stocks/SYNTH/aliases/{made.json()['id']}").status_code == 204
    assert client.delete(f"/api/stocks/SYNTH/aliases/{made.json()['id']}").status_code == 404


def test_report_builds_once_then_serves_stored(client: TestClient, seeded: Session) -> None:
    first = client.get("/api/stocks/synth/report")
    assert first.status_code == 200
    body = first.json()
    assert body["symbol"] == "SYNTH" and body["levels"]["fair_value"] > 0
    assert body["grade"] in ("A_plus", "A", "B", "C", "D")
    again = client.get("/api/stocks/SYNTH/report").json()
    assert again == body
    assert client.get("/api/stocks/SYNTH/report", params={"rebuild": True}).status_code == 200


def test_report_errors(client: TestClient, db: Session) -> None:
    assert client.get("/api/stocks/NOPE/report").status_code == 404
    assert client.get("/api/stocks/bad$sym/report").status_code == 422
    store_prices(
        db, ensure_instrument(db, "RAW", None), synthetic_daily(n_days=300), adjusted=False
    )
    res = client.get("/api/stocks/RAW/report")
    assert res.status_code == 409 and "adjusted" in res.json()["detail"]


def test_refresh_starts_one_pipeline_run_per_symbol(client: TestClient) -> None:
    a = client.post("/api/stocks/tcs/refresh")
    assert a.status_code == 202
    first = a.json()
    assert (first["symbol"], first["queued"], first["queue_length"]) == ("TCS", True, 1)
    b = client.post("/api/stocks/TCS/refresh").json()
    assert (b["queued"], b["queue_length"], b["run_id"]) == (False, 1, first["run_id"])
    client.post("/api/stocks/INFY/refresh")
    assert client.get("/api/jobs").json()["refresh_queue"] == ["TCS", "INFY"]
    run = client.get(f"/api/pipeline/{first['run_id']}").json()
    assert (run["status"], run["trigger"]) == ("queued", "refresh")


def test_sensitivity(client: TestClient, seeded: Session) -> None:
    assert client.get("/api/stocks/SYNTH/valuation/sensitivity").status_code == 404  # no run yet
    client.get("/api/stocks/SYNTH/report")
    s = client.get("/api/stocks/SYNTH/valuation/sensitivity").json()
    assert s["terminal_growths"] == [0.04, 0.05, 0.06, 0.07]
    assert len(s["waccs"]) == 9 and len(s["values"]) == 9 and len(s["values"][0]) == 4
    assert s["waccs"][4] == pytest.approx(s["base_wacc"])
    # Value falls as WACC rises (same terminal growth).
    col = [row[1] for row in s["values"]]
    assert col == sorted(col, reverse=True)
    client.get("/api/stocks/BANKCO/report")
    bank = client.get("/api/stocks/BANKCO/valuation/sensitivity")
    assert bank.status_code == 404 and "no DCF" in bank.json()["detail"]


def test_overrides_save_recompute_clear(client: TestClient, seeded: Session, db: Session) -> None:
    res = client.post("/api/stocks/SYNTH/overrides", json={"wacc": 0.1, "g1": 0.18})
    assert res.status_code == 200
    body = res.json()
    assert body["overrides"] == {**empty_overrides(), "wacc": 0.1, "g1": 0.18}
    assert body["report"]["valuation"]["wacc"] == 0.1
    assert body["reasons"] == ["saved: g1, wacc"]
    # null clears one key, omitted keys stay
    res2 = client.post("/api/stocks/SYNTH/overrides", json={"g1": None}).json()
    assert res2["overrides"]["g1"] is None and res2["overrides"]["wacc"] == 0.1
    assert res2["reasons"] == ["saved: nothing", "cleared: g1"]
    assert client.get("/api/stocks/SYNTH/overrides").json()["wacc"] == 0.1
    assert client.delete("/api/stocks/SYNTH/overrides").status_code == 204
    assert db.scalars(select(UserOverride)).all() == []


def empty_overrides() -> dict[str, None]:
    from app.reports.overrides import Overrides

    return dict.fromkeys(Overrides.model_fields)


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"wacc": 0.9}, "less than 0.5"),
        ({"sector": "crypto"}, "unknown sector"),
        ({"surprise": 1}, "Extra inputs"),
    ],
)
def test_overrides_validation(
    client: TestClient, seeded: Session, body: dict[str, object], fragment: str
) -> None:
    res = client.post("/api/stocks/SYNTH/overrides", json=body)
    assert res.status_code == 422 and fragment in res.text
    assert client.post("/api/stocks/NOPE/overrides", json={"wacc": 0.1}).status_code == 404


# ───────────────────────── screener ─────────────────────────


def test_screener_filters_and_sorts(client: TestClient, seeded: Session) -> None:
    for sym in ("SYNTH", "BANKCO"):
        client.get(f"/api/stocks/{sym}/report")
    rows = client.get("/api/screener").json()
    assert {r["symbol"] for r in rows} == {"SYNTH", "BANKCO"}
    scores = [r["total_score"] for r in rows if r["total_score"] is not None]
    assert scores == sorted(scores, reverse=True)
    it = client.get("/api/screener", params={"sector": "it_services"}).json()
    assert [r["symbol"] for r in it] == ["SYNTH"]
    synth = it[0]
    assert synth["market_cap_cr"] == pytest.approx(synth["cmp"] * 10)  # 10 cr shares
    by_grade = client.get("/api/screener", params=[("grade", synth["grade"])]).json()
    assert "SYNTH" in {r["symbol"] for r in by_grade}
    assert client.get("/api/screener", params={"min_mcap_cr": 1e9}).json() == []
    asc = client.get("/api/screener", params={"sort": "symbol", "order": "asc"}).json()
    assert [r["symbol"] for r in asc] == ["BANKCO", "SYNTH"]
    assert client.get("/api/screener", params={"grade": "Z"}).status_code == 422


def test_distance_to_buy_zone() -> None:
    from app.api.screener import distance_to_buy_zone

    assert distance_to_buy_zone(110, 90, 100) == pytest.approx(10 / 110)  # above: 9.1% fall
    assert distance_to_buy_zone(95, 90, 100) == 0.0
    assert distance_to_buy_zone(80, 90, 100) == pytest.approx(-10 / 80)
    assert distance_to_buy_zone(95, None, None) is None


# ───────────────────────── watchlist & alerts ─────────────────────────


def test_watchlist_crud(client: TestClient, seeded: Session) -> None:
    client.get("/api/stocks/SYNTH/report")
    res = client.post("/api/watchlist", json={"symbol": "synth", "notes": "quality compounder"})
    assert res.status_code == 201
    item = res.json()
    assert item["symbol"] == "SYNTH" and item["notes"] == "quality compounder"
    assert item["grade"] is not None and item["cmp"] > 0  # joined from the latest report
    new = client.post("/api/watchlist", json={"symbol": "NEWCO"}).json()  # unknown → created
    assert new["grade"] is None
    assert [w["symbol"] for w in client.get("/api/watchlist").json()] == ["NEWCO", "SYNTH"]
    assert client.delete("/api/watchlist/NEWCO").status_code == 204
    assert client.delete("/api/watchlist/NEWCO").status_code == 404


def test_alerts_crud(client: TestClient, seeded: Session) -> None:
    res = client.post("/api/alerts", json={"symbol": "SYNTH", "alert_type": "enters_buy_zone"})
    assert res.status_code == 201
    a = res.json()
    assert a["is_active"] and a["last_triggered_at"] is None
    again = client.post(
        "/api/alerts", json={"symbol": "SYNTH", "alert_type": "enters_buy_zone", "is_active": False}
    ).json()
    assert again["id"] == a["id"] and again["is_active"] is False  # one per stock + type
    assert (
        client.post("/api/alerts", json={"symbol": "SYNTH", "alert_type": "buy_now"}).status_code
        == 422
    )
    assert len(client.get("/api/alerts").json()) == 1
    assert client.delete(f"/api/alerts/{a['id']}").status_code == 204
    assert client.delete(f"/api/alerts/{a['id']}").status_code == 404


# ───────────────────────── uploads ─────────────────────────


def test_screener_upload(client: TestClient) -> None:
    with SCREENER_FIXTURE.open("rb") as fh:
        res = client.post(
            "/api/uploads/screener",
            files={"file": ("export.xlsx", fh, "application/vnd.ms-excel")},
            data={"symbol": "sampleind", "statement_type": "consolidated"},
        )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["symbol"] == "SAMPLEIND" and body["annual_rows"] == 10
    assert "fin_annual: announcement_date" in body["data_gaps"]


def test_screener_upload_rejections(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    bad_ext = client.post(
        "/api/uploads/screener",
        files={"file": ("export.csv", b"a,b", "text/csv")},
        data={"symbol": "X", "statement_type": "consolidated"},
    )
    assert bad_ext.status_code == 422
    not_screener = (
        Path(__file__).parent / "fixtures" / "screener" / "not_screener.xlsx"
    ).read_bytes()
    res = client.post(
        "/api/uploads/screener",
        files={"file": ("x.xlsx", not_screener, "application/octet-stream")},
        data={"symbol": "X", "statement_type": "standalone"},
    )
    assert res.status_code == 422
    missing_basis = client.post(
        "/api/uploads/screener",
        files={"file": ("x.xlsx", not_screener, "application/octet-stream")},
        data={"symbol": "X"},
    )
    assert missing_basis.status_code == 422
    monkeypatch.setenv("UPLOAD_MAX_BYTES", "1024")
    get_settings.cache_clear()
    big = client.post(
        "/api/uploads/screener",
        files={"file": ("x.xlsx", b"0" * 2048, "application/octet-stream")},
        data={"symbol": "X", "statement_type": "standalone"},
    )
    assert big.status_code == 413


# ───────────────────────── config ─────────────────────────


@pytest.fixture
def config_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "config"
    shutil.copytree(REPO_CONFIG_DIR, target)
    monkeypatch.setenv("CONFIG_DIR", str(target))
    get_settings.cache_clear()
    get_config.cache_clear()
    return target


def test_config_view(client: TestClient) -> None:
    body = client.get("/api/config").json()
    assert [f["name"] for f in body["files"]] == [
        "providers", "valuation", "sectors", "scoring", "technical", "jobs", "industries",
        "structural_events",
    ]  # fmt: skip
    assert body["parsed"]["scoring"]["weights"]["quality"] == 25


def test_config_update_validates_before_saving(config_copy: Path, db: Session) -> None:
    for c in app_client(db):
        original = (config_copy / "scoring.yaml").read_text()
        broken = original.replace("quality: 25", "quality: 30")  # weights no longer sum to 100
        res = c.put("/api/config", json={"name": "scoring", "yaml": broken})
        assert res.status_code == 422 and "sum to 100" in res.json()["detail"]
        assert res.json()["detail"].startswith("scoring.yaml is invalid:")
        assert "/tmp" not in res.json()["detail"]
        assert (config_copy / "scoring.yaml").read_text() == original  # nothing written
        bad_yaml = c.put("/api/config", json={"name": "scoring", "yaml": "scoring: [unclosed"})
        assert bad_yaml.status_code == 422 and "YAML error" in bad_yaml.json()["detail"]
        assert c.put("/api/config", json={"name": "../etc", "yaml": "x: 1"}).status_code == 422

        tuned = original.replace("momentum_entry_min: 6", "momentum_entry_min: 7")
        (config_copy / "scoring.yaml").chmod(0o640)
        ok = c.put("/api/config", json={"name": "scoring", "yaml": tuned})
        assert ok.status_code == 200 and ok.json()["saved"] is True
        assert (config_copy / "scoring.yaml").read_text() == tuned
        assert (config_copy / "scoring.yaml").stat().st_mode & 0o777 == 0o640  # mode kept
        view = c.get("/api/config").json()
        assert view["parsed"]["scoring"]["earned_premium"]["momentum_entry_min"] == 7
        assert not list(config_copy.glob(".scoring.*"))  # no staging leftovers


# ───────────────────────── backtests & jobs ─────────────────────────


def test_backtests_queue_and_fetch(client: TestClient, db: Session) -> None:
    req = {
        "grades": ["A_plus", "A"],
        "zones": ["discount", "deep_discount"],
        "holding_days": 250,
        "start": "2015-01-01",
        "end": "2024-01-01",
    }
    res = client.post("/api/backtests", json=req)
    assert res.status_code == 202
    b = res.json()
    assert b["status"] == "queued" and b["params"] == req and b["results"] is None
    got = client.get(f"/api/backtests/{b['id']}").json()
    assert got["id"] == b["id"] and got["status"] == "queued"
    assert db.scalars(select(Backtest)).one().params == req
    assert client.get("/api/backtests/999999").status_code == 404
    bad = client.post("/api/backtests", json={**req, "start": "2024-02-01"})
    assert bad.status_code == 422 and "start must be before end" in bad.text
    assert client.post("/api/backtests", json={**req, "grades": ["E"]}).status_code == 422


def test_backtests_list_summarises(client: TestClient, db: Session) -> None:
    params = {"grades": ["A"], "zones": ["fair"], "holding_days": 60}
    db.add(Backtest(status="queued", params=params))
    db.add(Backtest(status="running", params=params, results={"progress": {"done": 3, "total": 9}}))
    db.add(
        Backtest(
            status="done",
            params=params,
            results={"portfolio": {"cagr": 0.12}, "benchmark": {"cagr": 0.1}, "cells": []},
        )
    )
    db.commit()
    rows = client.get("/api/backtests").json()
    assert [r["status"] for r in rows] == ["done", "running", "queued"]
    assert rows[0]["cagr"] == 0.12 and rows[0]["benchmark_cagr"] == 0.1
    assert rows[0]["trades"] is None and rows[2]["trades"] is None
    assert "results" not in rows[0]
    assert rows[1]["progress"] == {"done": 3, "total": 9} and rows[1]["cagr"] is None
    assert len(client.get("/api/backtests?limit=1").json()) == 1
    assert client.get("/api/backtests?limit=0").status_code == 422
    sym = client.post(
        "/api/backtests",
        json={**params, "start": "2020-01-01", "end": "2021-01-01", "symbols": ["TCS"]},
    )
    assert sym.json()["params"]["symbols"] == ["TCS"]
    assert (
        client.post(
            "/api/backtests",
            json={**params, "start": "2020-01-01", "end": "2021-01-01", "symbols": ["bad sym!"]},
        ).status_code
        == 422
    )


def test_jobs_view(client: TestClient, seeded: Session, db: Session) -> None:
    db.add(JobRun(job_name="eod_prices", status="success", rows_written=5))
    db.add(JobRun(job_name="technicals", status="failed", error="boom"))
    db.flush()
    client.get("/api/stocks/SYNTH/report")
    body = client.get("/api/jobs").json()
    assert {r["job_name"] for r in body["runs"]} == {"eod_prices", "technicals"}
    only = client.get("/api/jobs", params={"job": "technicals"}).json()["runs"]
    assert [r["error"] for r in only] == ["boom"]
    f = body["freshness"]
    assert f["prices"] and f["reports"] and f["shareholding_period"] == "2024-03-31"
    assert f["delivery"] is None
    assert body["open_data_gaps"] > 0


def test_fundamentals_history(client: TestClient, seeded: Session) -> None:
    body = client.get("/api/stocks/SYNTH/fundamentals").json()
    assert [y["fiscal_year"] for y in body["years"]] == list(range(2015, 2025))
    first, last = body["years"][0], body["years"][-1]
    assert first["revenue"] == pytest.approx(1000.0)
    assert last["revenue"] == pytest.approx(1000 * 1.12**9)
    # FCF = CFO - capex (fixture: capex 4% of revenue); ROCE needs the prior year → null first
    assert last["fcf"] == pytest.approx(last["cfo"] - 0.04 * last["revenue"])
    assert first["roce"] is None and last["roce"] > 0
    assert body["statement_type"] == "consolidated" and body["missing"] == []
    shp = body["shareholding"]
    assert [s["period_end"] for s in shp] == ["2023-12-31", "2024-03-31"]
    assert shp[-1]["fii_pct"] == 20.5 and shp[-1]["public_pct"] == pytest.approx(12.0)
    assert shp[-1]["source"] == "nse" and shp[-1]["filing_date"] == "2024-04-21"
    assert shp[-1]["mf_pct"] == 8.0
    rep = client.get("/api/stocks/SYNTH/report").json()["shareholding"]
    assert rep["source"] == "nse" and rep["period_end"] == "2024-03-31"
    assert rep["filing_date"] == "2024-04-21" and rep["quarters"] == 2
    assert rep["fii_pct"] == 20.5 and rep["promoter_pledge_pct"] is not None
    assert client.get("/api/stocks/NOPE/fundamentals").status_code == 404


def test_report_carries_zone_bounds(client: TestClient, seeded: Session) -> None:
    lv = client.get("/api/stocks/SYNTH/report").json()["levels"]
    assert lv["discount_edge"] == pytest.approx(lv["fair_value"] * (1 - lv["mos_pct"]))
    assert lv["fair_upper"] == pytest.approx(lv["fair_value"] * 1.10)


def test_technical_debug_daily_bars(client: TestClient, seeded: Session) -> None:
    body = client.get("/api/stocks/SYNTH/technical/debug", params={"include_daily": True}).json()
    assert len(body["daily_bars"]) == 900 and body["daily_bars"][0]["time"] == "2021-01-01"
    assert "daily_bars" not in client.get("/api/stocks/SYNTH/technical/debug").json()


# ───────────────────────── P13 additions ─────────────────────────


def test_screener_presets_crud(client: TestClient) -> None:
    assert client.get("/api/screener/presets").json() == []
    body = {"filters": {"grade": ["A_plus", "A"], "zone": ["discount"], "min_earned_premium": 5}}
    res = client.put("/api/screener/presets/Quality at a discount", json=body)
    assert res.status_code == 200
    saved = res.json()
    assert saved["name"] == "Quality at a discount"
    assert (
        saved["filters"]["grade"] == ["A_plus", "A"] and saved["filters"]["sort"] == "total_score"
    )
    # replace
    body["filters"]["min_earned_premium"] = 6
    client.put("/api/screener/presets/Quality at a discount", json=body)
    presets = client.get("/api/screener/presets").json()
    assert len(presets) == 1 and presets[0]["filters"]["min_earned_premium"] == 6
    bad_sort = {"filters": {"sort": "name; drop table"}}
    assert client.put("/api/screener/presets/x", json=bad_sort).status_code == 422
    assert (
        client.put("/api/screener/presets/x", json={"filters": {"grade": ["E"]}}).status_code == 422
    )
    assert client.put("/api/screener/presets/x", json={"filters": {"extra": 1}}).status_code == 422
    assert client.delete("/api/screener/presets/Quality at a discount").status_code == 204
    assert client.delete("/api/screener/presets/Quality at a discount").status_code == 404


def test_uploads_are_listed(client: TestClient) -> None:
    assert client.get("/api/uploads/screener").json() == []
    with SCREENER_FIXTURE.open("rb") as fh:
        client.post(
            "/api/uploads/screener",
            files={"file": ("export.xlsx", fh, "application/octet-stream")},
            data={"symbol": "SAMPLEIND", "statement_type": "standalone"},
        )
    [row] = client.get("/api/uploads/screener").json()
    assert row["symbol"] == "SAMPLEIND" and row["statement_type"] == "standalone"
    assert row["annual_years"] == 10 and row["last_fiscal_year"] == 2024 and row["quarters"] == 10


def _xbrl(*names: str) -> list[tuple[str, tuple[str, bytes, str]]]:
    return [("files", (n, (XBRL_FIX / n).read_bytes(), "application/xml")) for n in names]


def test_xbrl_upload_parses_each_file_and_fills_the_ledger(client: TestClient, db: Session) -> None:
    res = client.post(
        "/api/uploads/xbrl",
        files=_xbrl("acme_q4fy24_consolidated.xml", "acme_q2fy25_standalone.xml"),
        data={"symbol": "acme"},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["symbol"] == "ACME"
    q4, q2 = body["files"]
    assert q4["status"] == "parsed" and q4["periods"] == ["quarter 2024-03-31", "year 2024-03-31"]
    assert q4["statement_type"] == "consolidated"
    assert q4["announcement_date"] == "2024-05-11"  # board meeting 10 May + 1 day
    assert q2["statement_type"] == "standalone" and q2["announcement_date"] == "2024-10-25"
    year = db.scalars(select(FinAnnual).where(FinAnnual.period_end == date(2024, 3, 31))).one()
    assert (year.revenue, year.source) == (pytest.approx(4800), "nse")
    # each upload was cached under RAW_DATA_DIR/upload/<yyyy>/<mm>/<dd>/ before parsing
    cached = db.scalars(select(ResultFiling.raw_path).order_by(ResultFiling.id)).all()
    assert all(p and p.startswith("upload/") and p.endswith(".xml") for p in cached)
    raw_root = get_settings().raw_data_dir
    assert (raw_root / cached[0]).read_bytes() == (
        XBRL_FIX / "acme_q4fy24_consolidated.xml"
    ).read_bytes()

    # the same document again is one ledger row; for another company it is refused
    client.post("/api/uploads/xbrl", files=_xbrl("acme_q4fy24_consolidated.xml"),
                data={"symbol": "ACME"})  # fmt: skip
    other = client.post("/api/uploads/xbrl", files=_xbrl("acme_q4fy24_consolidated.xml"),
                        data={"symbol": "OTHER"}).json()  # fmt: skip
    assert other["files"][0]["status"] == "failed"
    assert other["files"][0]["error"] == "document is for ACME, not OTHER"

    listed = client.get("/api/filings", params={"symbol": "ACME"}).json()
    assert len(listed) == 2 and {f["exchange"] for f in listed} == {"upload"}
    assert all(f["document"].startswith("upload:") for f in listed)
    assert client.get("/api/filings", params={"status": "failed"}).json()[0]["symbol"] == "OTHER"
    summary = client.get("/api/filings/summary").json()
    assert (summary["parsed"], summary["failed"], summary["symbols"]) == (2, 1, 1)
    assert summary["last_parsed_at"] is not None
    uploaded = listed[0]["id"]
    assert client.post(f"/api/filings/{uploaded}/retry").status_code == 409
    assert client.post("/api/filings/999999/retry").status_code == 404


def test_xbrl_upload_rejects_non_xml_and_oversize(client: TestClient) -> None:
    bad = client.post("/api/uploads/xbrl", files=[("files", ("a.xlsx", b"x", "x"))],
                      data={"symbol": "ACME"})  # fmt: skip
    assert bad.status_code == 422 and "expected .xml" in bad.text
    assert client.post("/api/uploads/xbrl", data={"symbol": "ACME"}).status_code == 422
    garbage = client.post("/api/uploads/xbrl", files=[("files", ("x.xml", b"<html/>", "x"))],
                          data={"symbol": "ACME"}).json()  # fmt: skip
    assert garbage["files"][0]["status"] == "failed"
    assert "not an XBRL instance" in garbage["files"][0]["error"]


def test_retry_requeues_a_failed_exchange_filing(client: TestClient, db: Session) -> None:
    iid = ensure_instrument(db, "ACME", "chemicals")
    row = ResultFiling(instrument_id=iid, exchange="nse", document="https://x/A.xml",
                       status=FilingStatus.FAILED, attempts=3, error="HTTP 503")  # fmt: skip
    db.add(row)
    db.commit()
    res = client.post(f"/api/filings/{row.id}/retry").json()
    assert (res["status"], res["attempts"], res["error"], res["symbol"]) == (
        "pending", 0, None, "ACME",
    )  # fmt: skip


def test_config_dry_run_validates_without_saving(config_copy: Path, db: Session) -> None:
    for c in app_client(db):
        original = (config_copy / "scoring.yaml").read_text()
        tuned = original.replace("momentum_entry_min: 6", "momentum_entry_min: 7")
        ok = c.put("/api/config", params={"dry_run": True}, json={"name": "scoring", "yaml": tuned})
        assert ok.status_code == 200 and ok.json()["saved"] is False
        assert (config_copy / "scoring.yaml").read_text() == original
        bad = original.replace("quality: 25", "quality: 30")
        res = c.put("/api/config", params={"dry_run": True}, json={"name": "scoring", "yaml": bad})
        assert res.status_code == 422 and "sum to 100" in res.json()["detail"]


def test_config_update_on_read_only_dir_is_a_clear_503(
    config_copy: Path, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(*_: object, **__: object) -> tuple[int, str]:
        raise PermissionError(13, "Permission denied")

    for c in app_client(db):
        original = (config_copy / "scoring.yaml").read_text()
        monkeypatch.setattr("app.api.admin.tempfile.mkstemp", denied)
        res = c.put("/api/config", json={"name": "scoring", "yaml": original})
        assert res.status_code == 503
        assert res.json()["detail"] == (
            "config directory is not writable by the API (Permission denied); nothing saved"
        )


def test_broker_status_reports_configuration(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = {b["broker"]: b for b in client.get("/api/brokers/status").json()}
    assert body["fyers"]["configured"] is False and body["kite"]["configured"] is False
    monkeypatch.setenv("KITE_API_KEY", "k")
    monkeypatch.setenv("KITE_API_SECRET", "s")
    get_settings.cache_clear()
    body = {b["broker"]: b for b in client.get("/api/brokers/status").json()}
    assert body["kite"]["configured"] is True and body["kite"]["connected"] is False


# ───────────────────────── P14 notifications ─────────────────────────


def test_notifications_list_and_read(client: TestClient, db: Session) -> None:
    from app.db.models import Notification

    body = client.get("/api/notifications").json()
    assert body == {
        "items": [],
        "unread": 0,
        "telegram_configured": False,
        "kinds": {},
        "next_before_id": None,
    }
    for i in range(3):
        db.add(Notification(kind="crosses_fv", title=f"T{i}", body="b", telegram="disabled"))
    db.flush()
    body = client.get("/api/notifications").json()
    assert body["unread"] == 3 and [n["title"] for n in body["items"]] == ["T2", "T1", "T0"]
    first = body["items"][0]["id"]
    assert client.post(f"/api/notifications/{first}/read").status_code == 204
    assert client.get("/api/notifications", params={"unread_only": True}).json()["unread"] == 2
    assert client.post("/api/notifications/999999/read").status_code == 404
    assert client.post("/api/notifications/read-all").status_code == 204
    assert client.get("/api/notifications").json()["unread"] == 0


def test_notification_test_message(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import responses

    res = client.post("/api/notifications/test")
    assert res.status_code == 201 and res.json()["telegram"] == "disabled"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    get_settings.cache_clear()
    with responses.RequestsMock() as rsps:
        rsps.post("https://api.telegram.org/bot1:abc/sendMessage", json={"ok": True})
        sent = client.post("/api/notifications/test").json()
    assert sent["telegram"] == "sent" and sent["kind"] == "test"
    assert client.get("/api/notifications").json()["telegram_configured"] is True


def test_notification_centre_filters_paging_resend(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    import responses

    from app.db.models import Notification

    for i, (kind, sym) in enumerate([("crosses_fv", "TCS"), ("results", "TCS"),
                                     ("results", "INFY"), ("broker_token", None),
                                     ("enters_buy_zone", "INFY")]):  # fmt: skip
        db.add(Notification(kind=kind, symbol=sym, title=f"T{i}", body="b",
                            telegram="failed" if i == 1 else "disabled",
                            telegram_error="HTTP 502" if i == 1 else None))  # fmt: skip
    db.flush()
    body = client.get("/api/notifications").json()
    assert body["kinds"] == {"crosses_fv": 1, "results": 2, "broker_token": 1,
                             "enters_buy_zone": 1}  # fmt: skip
    results = client.get("/api/notifications", params={"kind": ["results"]}).json()
    assert [n["title"] for n in results["items"]] == ["T2", "T1"]
    tcs = client.get("/api/notifications", params={"symbol": "tcs"}).json()
    assert [n["title"] for n in tcs["items"]] == ["T1", "T0"]
    page1 = client.get("/api/notifications", params={"limit": 2}).json()
    assert [n["title"] for n in page1["items"]] == ["T4", "T3"] and page1["next_before_id"]
    page2 = client.get("/api/notifications", params={"limit": 2,
                       "before_id": page1["next_before_id"]}).json()  # fmt: skip
    assert [n["title"] for n in page2["items"]] == ["T2", "T1"]
    page3 = client.get("/api/notifications", params={"limit": 2,
                       "before_id": page2["next_before_id"]}).json()  # fmt: skip
    assert [n["title"] for n in page3["items"]] == ["T0"] and page3["next_before_id"] is None

    failed = results["items"][1]
    assert client.post(f"/api/notifications/{failed['id']}/resend").status_code == 409
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    get_settings.cache_clear()
    with responses.RequestsMock() as rsps:
        rsps.post("https://api.telegram.org/bot1:abc/sendMessage", json={"ok": True})
        again = client.post(f"/api/notifications/{failed['id']}/resend").json()
    assert again["telegram"] == "sent" and again["telegram_error"] is None

    nid = page1["items"][0]["id"]
    client.post(f"/api/notifications/{nid}/read")
    assert client.post(f"/api/notifications/{nid}/unread").status_code == 204
    assert client.get("/api/notifications").json()["unread"] == 5
    assert client.post("/api/notifications/999999/unread").status_code == 404


def test_telegram_bot_status(client: TestClient, redis_client: Redis) -> None:
    from app.alerts.bot import STATUS_KEY

    redis_client.delete(STATUS_KEY, STATUS_KEY + ":counts")
    st = client.get("/api/notifications/telegram").json()
    assert st == {"configured": False, "bot_enabled": True, "state": None, "last_poll_at": None,
                  "last_command_at": None, "last_error": None, "last_error_at": None,
                  "ignored_messages": 0}  # fmt: skip
    redis_client.set(STATUS_KEY, '{"state": "polling", "last_poll_at": '
                     '"2024-06-14T13:00:00+00:00", "last_error": null}')  # fmt: skip
    redis_client.hincrby(STATUS_KEY + ":counts", "ignored", 2)
    st = client.get("/api/notifications/telegram").json()
    assert st["state"] == "polling" and st["ignored_messages"] == 2
    assert st["last_poll_at"].startswith("2024-06-14T13:00:00")
    redis_client.delete(STATUS_KEY, STATUS_KEY + ":counts")
