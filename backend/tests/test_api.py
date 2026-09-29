"""SPEC §8 endpoints (Postgres + Redis): schemas, status codes, persistence, OpenAPI."""

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_config
from app.core.settings import get_settings
from app.db.models import Backtest, JobRun, UserOverride
from app.jobs.refresh import PENDING_KEY, QUEUE_KEY
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


@pytest.fixture
def client(db: Session, redis_client: Redis) -> Iterator[TestClient]:
    redis_client.delete(QUEUE_KEY, PENDING_KEY, "auth:fail:testclient")
    yield from app_client(db)
    redis_client.delete(QUEUE_KEY, PENDING_KEY)


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
    by_name = client.get("/api/stocks/search", params={"q": "Ltd"}).json()
    assert {r["symbol"] for r in by_name} == {"SYNTH", "BANKCO"}  # indices excluded
    with_idx = client.get("/api/stocks/search", params={"q": "NIFTY", "include_indices": True})
    assert [r["symbol"] for r in with_idx.json()] == ["NIFTY500"]
    assert client.get("/api/stocks/search", params={"q": ""}).status_code == 422
    assert client.get("/api/stocks/search", params={"q": "%"}).json() == []  # LIKE escaped


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


def test_refresh_enqueues_once(client: TestClient, redis_client: Redis) -> None:
    a = client.post("/api/stocks/tcs/refresh")
    assert a.status_code == 202 and a.json() == {"symbol": "TCS", "queued": True, "queue_length": 1}
    b = client.post("/api/stocks/TCS/refresh").json()
    assert b == {"symbol": "TCS", "queued": False, "queue_length": 1}
    client.post("/api/stocks/INFY/refresh")
    assert redis_client.lrange(QUEUE_KEY, 0, -1) == [b"TCS", b"INFY"]
    assert client.get("/api/jobs").json()["refresh_queue"] == ["TCS", "INFY"]


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
        "providers", "valuation", "sectors", "scoring", "technical", "jobs",
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
    assert body == {"items": [], "unread": 0, "telegram_configured": False}
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
