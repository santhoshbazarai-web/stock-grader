"""Screener v2: filter logic (pure engine), preset loading, scan / screens API."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy.orm import Session

from app.core.config import ScreenFilter, load_config
from app.devtools.synthetic import seed_company, seed_index
from app.reports.service import refresh_report
from app.screener.engine import apply_filters, extract, paginate, passes, sort_rows
from tests.api_support import app_client
from tests.conftest import REPO_CONFIG_DIR

CFG = load_config(REPO_CONFIG_DIR)
FIELDS = CFG.screener_fields.fields


def payload(symbol: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "symbol": symbol,
        "name": f"{symbol} Ltd",
        "cmp": 100.0,
        "day_change_pct": 0.012,
        "grade_label": "A",
        "zone": "discount",
        "action": "buy",
        "earned_premium": 5,
        "levels": {"fair_value": 125.0, "confidence": "high"},
        "valuation": {"sector": "it_services", "market_cap_cr": 8000.0, "beta": 0.7},
        "fundamentals": {"roe_latest": 0.18, "debt_to_equity": 0.2, "eps_cagr_5y": 0.14},
        "peer_stats": {"pe": 22.0, "pb": 3.0},
        "scores": {"total": 70.0},
        "technical": {"stage": 2, "rs_percentile": 80.0},
        "data_depth": {"level": "full"},
        "pillars": [{"pillar": "quality", "score": 80.0}],
    }
    base.update(over)
    return base


def rows(*payloads: dict[str, Any]) -> list[dict[str, Any]]:
    return [extract(p, FIELDS) for p in payloads]


def test_extract_scales_percent_fields_and_computes_discount() -> None:
    r = extract(payload("AAA"), FIELDS)
    assert r["roe"] == pytest.approx(18.0) and r["day_change_pct"] == pytest.approx(1.2)
    assert r["discount_pct"] == pytest.approx(-20.0)  # 100 / 125 - 1
    assert r["zone"] == "discount" and r["grade"] == "A" and r["depth"] == "full"
    assert r["dividend_yield"] is None and r["pledge_pct"] is None  # not stored: never 0
    assert r["pillars"] == [{"pillar": "quality", "score": 80.0}]


def test_bank_metrics_are_read_in_percent() -> None:
    p = payload("BNK", bank_metrics=[{"name": "roa_pct", "value": 1.63}])
    assert extract(p, FIELDS)["roa_pct"] == pytest.approx(1.63)


def test_filters_min_max_in_and_missing_values() -> None:
    data = rows(
        payload("LOW", fundamentals={"roe_latest": 0.10}),
        payload("MID", fundamentals={"roe_latest": 0.15}),
        payload("HIGH", fundamentals={"roe_latest": 0.25}, grade_label="B"),
        payload("NONE", fundamentals={}),
    )

    def keep(**kw: Any) -> list[str]:
        return [r["symbol"] for r in apply_filters(data, [ScreenFilter.model_validate(kw)])]

    assert keep(key="roe", min=15) == ["MID", "HIGH"]  # inclusive; NONE has no ROE: dropped
    assert keep(key="roe", max=15) == ["LOW", "MID"]
    assert keep(key="roe", min=12, max=20) == ["MID"]
    assert keep(key="grade", **{"in": ["B"]}) == ["HIGH"]
    both = apply_filters(
        data, [ScreenFilter(key="roe", min=12), ScreenFilter(key="grade", **{"in": ["A"]})]
    )  # type: ignore[arg-type]
    assert [r["symbol"] for r in both] == ["MID"]  # filters AND together
    assert not passes(data[0], ScreenFilter(key="roe", min=1000))


def test_filter_needs_a_bound() -> None:
    with pytest.raises(ValueError, match="no min, max or in"):
        ScreenFilter(key="roe")
    with pytest.raises(ValueError, match="min > max"):
        ScreenFilter(key="roe", min=5, max=1)


def test_sort_puts_missing_last_in_both_directions_and_pages() -> None:
    data = rows(
        payload("A", peer_stats={"pe": 30.0}), payload("B", peer_stats={"pe": 10.0}),
        payload("C", peer_stats={}), payload("D", peer_stats={"pe": 20.0}),
    )  # fmt: skip
    assert [r["symbol"] for r in sort_rows(data, "pe", "asc")] == ["B", "D", "A", "C"]
    assert [r["symbol"] for r in sort_rows(data, "pe", "desc")] == ["A", "D", "B", "C"]
    assert [r["symbol"] for r in paginate(sort_rows(data, "symbol", "asc"), 2, 3)] == ["D"]


def test_presets_load_and_use_known_fields() -> None:
    presets = CFG.screen_presets.presets
    assert [p.name for p in presets] == [
        "Quality Compounders", "Dividend Growers", "High-Growth (speculative)",
        "Defensive Stocks", "Fallen Quality", "Cyclical Value", "Hidden Small Caps",
        "Growth at a Reasonable Price", "Quality at a Fair Price",
    ]  # fmt: skip
    fields = CFG.screener_fields.by_key()
    groups = {f.group for f in FIELDS}
    assert groups == set(CFG.screener_fields.groups) == {
        "Overview", "Valuation", "Growth", "Quality", "Financial strength", "Dividend",
        "Technical", "Ownership",
    }  # fmt: skip
    for key in ("zone", "grade", "action", "discount_pct", "pe", "pb", "roe", "roce",
                "eps_cagr_5y", "sales_cagr_5y", "debt_to_equity", "dividend_yield", "stage",
                "rs_percentile", "depth", "sector"):  # fmt: skip
        assert key in fields
    for p in presets:
        assert p.description and p.filters and p.sort in fields


def test_a_preset_with_an_unknown_field_is_refused(tmp_path: Any) -> None:
    import shutil

    from app.core.config import ConfigError

    shutil.copytree(REPO_CONFIG_DIR, tmp_path / "c")
    f = tmp_path / "c" / "screen_presets.yaml"
    f.write_text(f.read_text().replace("key: roe, min: 15}", "key: not_a_field, min: 15}", 1))
    with pytest.raises(ConfigError, match="unknown field 'not_a_field'"):
        load_config(tmp_path / "c")


# ───────────────────────── API ─────────────────────────


@pytest.fixture
def client(db: Session, redis_client: Redis) -> Iterator[TestClient]:
    redis_client.delete("auth:fail:testclient")
    yield from app_client(db)


@pytest.fixture
def seeded(db: Session) -> Session:
    seed_index(db)
    for i, sym in enumerate(("AAA", "BBB", "CCC", "DDD", "EEE")):
        seed_company(db, sym, seed=11 + i, pe=14.0 + 5 * i, growth=0.06 + 0.04 * i)
        refresh_report(db, sym, load_config(REPO_CONFIG_DIR))
    return db


def test_scan_filters_sorts_pages(client: TestClient, seeded: Session) -> None:
    allr = client.get("/api/screener/scan?page_size=10&sort=symbol&order=asc").json()
    assert allr["total"] == allr["universe"] == 5 and allr["page"] == 1
    assert [r["symbol"] for r in allr["rows"]] == ["AAA", "BBB", "CCC", "DDD", "EEE"]
    assert allr["scanned_at"]
    paged = client.get("/api/screener/scan?page_size=10&sort=symbol&order=asc&page=1").json()
    assert paged["rows"] == allr["rows"]
    pes = sorted(r["pe"] for r in allr["rows"] if r["pe"] is not None)
    cut = pes[2]
    low = client.get(f"/api/screener/scan?max.pe={cut}&sort=pe&order=asc").json()
    assert low["total"] == 3 and [r["pe"] for r in low["rows"]] == pes[:3]
    zone = allr["rows"][0]["zone"]
    only = client.get(f"/api/screener/scan?in.zone={zone}").json()
    assert only["total"] == sum(r["zone"] == zone for r in allr["rows"])
    two = client.get("/api/screener/scan?page_size=10&sort=symbol&order=desc").json()
    assert two["rows"][0]["symbol"] == "EEE"
    p2 = client.get("/api/screener/scan?sort=symbol&order=asc&page_size=10&min.market_cap_cr=1e99")
    assert p2.json()["total"] == 0


def test_scan_errors(client: TestClient, seeded: Session) -> None:
    assert client.get("/api/screener/scan?min.nope=1").status_code == 422
    assert client.get("/api/screener/scan?min.zone=1").status_code == 422  # enum: use in.
    assert client.get("/api/screener/scan?in.pe=1").status_code == 422  # number: min / max
    assert client.get("/api/screener/scan?min.pe=abc").status_code == 422
    assert client.get("/api/screener/scan?sort=nope").status_code == 422
    assert client.get("/api/screener/scan?page_size=7").status_code == 422


def test_every_idea_runs(client: TestClient, seeded: Session) -> None:
    ideas = client.get("/api/screener/ideas").json()
    assert len(ideas) == 9
    for idea in ideas:
        q = [f"sort={idea['sort']}", f"order={idea['order']}"]
        for f in idea["filters"]:
            if f["in"]:
                q.append(f"in.{f['key']}={','.join(f['in'])}")
            for op in ("min", "max"):
                if f[op] is not None:
                    q.append(f"{op}.{f['key']}={f[op]}")
        assert client.get("/api/screener/scan?" + "&".join(q)).status_code == 200, idea["id"]


def test_fields_endpoint_lists_groups_and_enum_options(client: TestClient, seeded: Session) -> None:
    body = client.get("/api/screener/fields").json()
    assert body["groups"][0] == "Overview" and len(body["fields"]) == len(FIELDS)
    zone = next(f for f in body["fields"] if f["key"] == "zone")
    assert zone["type"] == "enum" and zone["options"]
    assert next(f for f in body["fields"] if f["key"] == "pe")["options"] is None


def test_save_load_replace_delete_screens(client: TestClient, seeded: Session) -> None:
    assert client.get("/api/screens").json() == []
    defn = {"filters": [{"key": "roe", "min": 15}, {"key": "zone", "in": ["discount"]}],
            "sort": "discount_pct", "order": "asc"}  # fmt: skip
    saved = client.post("/api/screens", json={"name": "My value", "definition": defn})
    assert saved.status_code == 201 and saved.json()["definition"]["sort"] == "discount_pct"
    assert (
        client.post(
            "/api/screens", json={"name": "My value", "definition": {**defn, "order": "desc"}}
        ).status_code
        == 201
    )
    listed = client.get("/api/screens").json()
    assert [s["name"] for s in listed] == ["My value"] and listed[0]["definition"][
        "order"
    ] == "desc"
    assert listed[0]["definition"]["filters"][1]["in"] == ["discount"]
    bad = {"filters": [{"key": "nope", "min": 1}], "sort": "pe", "order": "asc"}
    assert client.post("/api/screens", json={"name": "x", "definition": bad}).status_code == 422
    assert client.delete("/api/screens/My value").status_code == 204
    assert client.delete("/api/screens/My value").status_code == 404
