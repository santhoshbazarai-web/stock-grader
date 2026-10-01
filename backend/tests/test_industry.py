"""Industry classification → sector model (industries.yaml): parsing, mapping, storage, the
job, and the report's "sector unmapped" gap. No network: fake providers."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from sqlalchemy.orm import Session

from app.core.config import ConfigError, Provider, load_config, normalise_label
from app.data.gaps import InMemoryGapRecorder
from app.data.industry import (
    IndustryInfo,
    classify,
    parse_nse_quote_industry,
    parse_yfinance_info,
)
from app.data.industry_store import classify_symbol
from app.data.providers.base import ProviderError, ProviderUnavailable
from app.data.router import DataRouter
from app.db.models import Instrument
from tests.conftest import REPO_CONFIG_DIR
from tests.jobs_support import Env, NoLimit

CFG = load_config(REPO_CONFIG_DIR)
FIX = Path(__file__).parent / "fixtures" / "nse"
NOW = datetime(2026, 10, 1, 4, 0, tzinfo=UTC)


def test_nse_quote_basic_industry() -> None:
    payload = json.loads((FIX / "quote_equity_hdfcbank.json").read_text())
    info = parse_nse_quote_industry(payload)
    assert info == IndustryInfo("nse", "Private Sector Bank", "Financial Services",
                                "Financial Services", "Banks")  # fmt: skip
    assert parse_nse_quote_industry({"info": {}}) is None
    assert parse_nse_quote_industry({"industryInfo": {"basicIndustry": "-"}}) is None
    with pytest.raises(ProviderError):
        parse_nse_quote_industry([])


def test_yfinance_industry() -> None:
    info = parse_yfinance_info({"sector": "Financial Services", "industry": "Banks - Regional"})
    assert info is not None and info.label == "Banks - Regional" and info.source == "yfinance"
    assert parse_yfinance_info({}) is None


@pytest.mark.parametrize(
    ("source", "label", "sector"),
    [
        ("nse", "Private Sector Bank", "banks"),
        ("nse", "PUBLIC SECTOR BANK", "banks"),
        ("nse", "Non Banking Financial Company (NBFC)", "nbfc"),
        ("nse", "Housing Finance Company", "nbfc"),
        ("nse", "Life Insurance", "insurance"),
        ("nse", "Computers - Software & Consulting", "it_services"),
        ("nse", "Cement & Cement Products", "cement"),
        ("nse", "2/3 Wheelers", "auto"),
        ("yfinance", "Banks - Regional", "banks"),
        ("yfinance", "Credit Services", "nbfc"),
        ("yfinance", "Insurance - Life", "insurance"),
    ],
)
def test_financials_route_to_their_own_models(source: str, label: str, sector: str) -> None:
    c = classify(IndustryInfo(source, label), CFG.industries)
    assert c.sector == sector
    model = CFG.sectors.for_sector(c.sector).model.value
    if sector in ("banks", "nbfc"):
        assert model == "bank"  # AGENTS.md rule 10: never FCFF
    if sector == "insurance":
        assert model == "insurance"


def test_unmapped_label_is_explained() -> None:
    c = classify(IndustryInfo("nse", "Stock Exchanges & Clearing"), CFG.industries)
    assert c.sector is None
    assert "sector unmapped: nse industry 'Stock Exchanges & Clearing'" in c.reason
    assert normalise_label("  Iron & Steel  ") == "iron steel"


def test_mapping_must_name_a_known_sector(tmp_path: Path) -> None:
    for f in REPO_CONFIG_DIR.glob("*.yaml"):
        (tmp_path / f.name).write_text(f.read_text())
    doc = yaml.safe_load((tmp_path / "industries.yaml").read_text())
    doc["industries"]["nse_basic_industry"]["Private Sector Bank"] = "bankz"
    (tmp_path / "industries.yaml").write_text(yaml.safe_dump(doc))
    with pytest.raises(ConfigError, match="unknown sectors: \\['bankz'\\]"):
        load_config(tmp_path)


# ───────────────────────── storage (DB) ─────────────────────────


class FakeSource:
    def __init__(self, name: Provider, answer: IndustryInfo | Exception) -> None:
        self.name = name
        self.answer = answer
        self.calls = 0

    def industry_info(self, symbol: str) -> IndustryInfo:
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def router(nse: FakeSource, yf: FakeSource) -> DataRouter:
    sources = {Provider.NSE: nse, Provider.YFINANCE: yf}
    return DataRouter(sources, CFG.providers, limiter=NoLimit(), gaps=InMemoryGapRecorder(),
                      sleep=lambda _: None)  # fmt: skip


def test_classify_symbol_nse_then_yfinance(db: Session) -> None:
    db.add(Instrument(symbol="HDFCBANK", source="nse", fetched_at=NOW))
    db.flush()
    nse = FakeSource(Provider.NSE, ProviderUnavailable("NSE is blocking automated access"))
    yf = FakeSource(Provider.YFINANCE, IndustryInfo("yfinance", "Banks - Regional"))
    out = classify_symbol(db, router(nse, yf), "hdfcbank", CFG.industries, now=NOW)
    assert out.fetched and out.sector == "banks" and out.source == "yfinance"
    inst = db.query(Instrument).filter_by(symbol="HDFCBANK").one()
    assert (inst.sector, inst.basic_industry, inst.industry_source) == ("banks",
                                                                       "Banks - Regional",
                                                                       "yfinance")  # fmt: skip
    assert inst.classified_at == NOW
    # NSE answers next time: its basic industry wins
    nse2 = FakeSource(Provider.NSE, IndustryInfo("nse", "Private Sector Bank"))
    out = classify_symbol(db, router(nse2, yf), "HDFCBANK", CFG.industries, now=NOW)
    assert out.source == "nse" and inst.sector == "banks"


def test_unmapped_clears_only_our_own_sector(db: Session) -> None:
    db.add_all([Instrument(symbol="SEEDED", sector="it_services", source="demo", fetched_at=NOW),
                Instrument(symbol="OURS", sector="banks", industry_source="nse",
                           source="nse", fetched_at=NOW)])  # fmt: skip
    db.flush()
    odd = FakeSource(Provider.NSE, IndustryInfo("nse", "Exchange and Data Platform"))
    none = FakeSource(Provider.YFINANCE, ProviderUnavailable("no"))
    r = router(odd, none)
    assert classify_symbol(db, r, "SEEDED", CFG.industries, now=NOW).sector == "it_services"
    assert classify_symbol(db, r, "OURS", CFG.industries, now=NOW).sector is None


def test_nothing_answers(db: Session) -> None:
    db.add(Instrument(symbol="X", source="nse", fetched_at=NOW))
    db.flush()
    out = classify_symbol(db, router(FakeSource(Provider.NSE, ProviderUnavailable("blocked")),
                                     FakeSource(Provider.YFINANCE, ProviderUnavailable("no"))),
                          "X", CFG.industries, now=NOW)  # fmt: skip
    assert not out.fetched and "industry unavailable" in out.message
    assert db.query(Instrument).filter_by(symbol="X").one().classified_at is None


# ───────────────────────── report gap, job ─────────────────────────


def test_report_names_the_unmapped_sector(db: Session) -> None:
    from app.reports.service import build_for
    from tests.report_support import seed_company, seed_index

    seed_index(db)
    seed_company(db, "UNCLASS", sector=None)
    gaps = build_for(db, "UNCLASS", CFG).report.data_gaps
    assert any(g.startswith("sector unmapped: industry not classified yet") for g in gaps)
    inst = db.query(Instrument).filter_by(symbol="UNCLASS").one()
    inst.basic_industry, inst.industry_source = "Exchange and Data Platform", "nse"
    db.flush()
    gaps = build_for(db, "UNCLASS", CFG).report.data_gaps
    assert any("nse industry 'Exchange and Data Platform' has no entry" in g for g in gaps)


def test_job_classifies_unclassified_first(env: Env) -> None:
    from app.core.config import JobName
    from app.jobs.registry import REGISTRY
    from app.jobs.runner import JobOptions, run_job

    with env.session() as s:
        s.add_all([Instrument(symbol=f"S{i}", source="nse", fetched_at=NOW) for i in range(3)])
        s.commit()
    env.ctx.router = router(FakeSource(Provider.NSE, IndustryInfo("nse", "Private Sector Bank")),
                            FakeSource(Provider.YFINANCE, ProviderUnavailable("no")))  # fmt: skip
    out = run_job(REGISTRY[JobName.INDUSTRY_CLASSIFICATION], env.ctx,
                  JobOptions(symbols=("S0", "S1", "S2"))).outcome  # fmt: skip
    assert out is not None and out.details["mapped"] == {"S0": "banks", "S1": "banks",
                                                         "S2": "banks"}  # fmt: skip
