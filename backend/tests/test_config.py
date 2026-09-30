import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core.config import (
    CONFIG_FILES,
    AppConfig,
    ConfigError,
    Dataset,
    PiecewiseLinearMap,
    Provider,
    SectorModel,
    get_config,
    load_config,
)
from tests.conftest import REPO_CONFIG_DIR

Mutator = Callable[[dict[str, Any]], None]


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    for name in CONFIG_FILES:
        shutil.copy(REPO_CONFIG_DIR / f"{name}.yaml", tmp_path / f"{name}.yaml")
    return tmp_path


def _mutate(config_dir: Path, name: str, fn: Mutator) -> None:
    path = config_dir / f"{name}.yaml"
    doc = yaml.safe_load(path.read_text())
    fn(doc[name])
    path.write_text(yaml.safe_dump(doc))


# ───────────── the committed config is valid ─────────────


def test_repo_config_loads() -> None:
    cfg = load_config(REPO_CONFIG_DIR)
    assert isinstance(cfg, AppConfig)
    assert cfg.providers.priority[Dataset.DAILY_OHLCV] == [
        Provider.FYERS,
        Provider.KITE,
        Provider.YFINANCE,
    ]
    assert cfg.valuation.mos_by_grade.B == pytest.approx(0.275)
    assert cfg.scoring.weights.quality == 25
    assert cfg.scoring.maps.stage_score[2] == 100
    assert cfg.technical.atr_period == 14


def test_config_files_split() -> None:
    assert set(CONFIG_FILES) == {
        "providers",
        "valuation",
        "sectors",
        "scoring",
        "technical",
        "jobs",
    }
    for name in CONFIG_FILES:
        assert (REPO_CONFIG_DIR / f"{name}.yaml").is_file()


def test_get_config_uses_settings_dir() -> None:
    assert get_config() is get_config()


def test_sector_lookup_falls_back_to_default() -> None:
    sectors = load_config(REPO_CONFIG_DIR).sectors
    assert sectors.for_sector("banks").model is SectorModel.BANK
    assert sectors.for_sector("unknown_sector") is sectors.root["default"]
    assert sectors.for_sector(None) is sectors.root["default"]


def test_financial_sectors_never_use_fcff() -> None:
    sectors = load_config(REPO_CONFIG_DIR).sectors.root
    for name in ("banks", "nbfc", "insurance"):
        assert sectors[name].model is not SectorModel.FCFF
        assert "dcf_base" not in (sectors[name].weights or {})


# ───────────── fail fast on invalid config ─────────────


def test_missing_file(config_dir: Path) -> None:
    (config_dir / "sectors.yaml").unlink()
    with pytest.raises(ConfigError, match="missing config file"):
        load_config(config_dir)


def test_malformed_yaml(config_dir: Path) -> None:
    (config_dir / "scoring.yaml").write_text("scoring: [unclosed\n")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(config_dir)


def test_wrong_top_level_key(config_dir: Path) -> None:
    (config_dir / "technical.yaml").write_text("tech:\n  atr_period: 14\n")
    with pytest.raises(ConfigError, match="exactly one top-level key 'technical'"):
        load_config(config_dir)


def _set(path: list[str | int], value: Any) -> Mutator:
    def fn(doc: dict[str, Any]) -> None:
        node: Any = doc
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    return fn


def _delete(path: list[str]) -> Mutator:
    def fn(doc: dict[str, Any]) -> None:
        node: Any = doc
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]

    return fn


@pytest.mark.parametrize(
    ("file", "mutator", "message"),
    [
        # providers
        ("providers", _set(["priority", "daily_ohlcv"], ["fyers", "zerodha"]), "priority"),
        ("providers", _set(["priority", "ltp"], []), "at least one provider"),
        ("providers", _set(["priority", "ltp"], ["fyers", "fyers"]), "provider twice"),
        ("providers", _delete(["priority", "surveillance"]), "missing datasets"),
        ("providers", _set(["rate_limits", "nse", "per_sec"], 0), "greater than 0"),
        ("providers", _set(["rate_limits", "kite"], {"per_sec": 10, "per_min": 5}), "per_min"),
        ("providers", _set(["unknown_key"], 1), "Extra inputs"),
        ("providers", _set(["retry", "max_attempts"], 0), "greater than 0"),
        ("providers", _set(["retry", "backoff_base_s"], 10), "backoff_base_s"),
        ("providers", _delete(["retry"]), "retry"),
        ("providers", _set(["api_limits", "fyers", "history_max_days"], 0), "greater than 0"),
        ("providers", _set(["oauth_state_ttl_s"], -1), "greater than 0"),
        ("providers", _set(["token_daily_expiry_ist", "kite"], "25:00"), "token_daily_expiry_ist"),
        ("providers", _set(["instruments_cache_hours"], 0), "greater than 0"),
        ("providers", _set(["nse", "results", "quarter_days"], {"min": 99, "max": 9}), "min must"),
        ("providers", _set(["nse", "results", "quarter_days", "max"], 360), "must end before"),
        ("providers", _set(["nse", "results", "xbrl_hosts"], []), "at least 1"),
        ("providers", _set(["nse", "results", "available_after_ist"], "25:00"), "available_after"),
        ("providers", _delete(["nse", "results"]), "results"),
        ("providers", _delete(["priority", "results_filings"]), "missing datasets"),
        # valuation
        ("valuation", _set(["risk_free_rate"], 6.5), "less than or equal to 1"),
        ("valuation", _set(["dcf", "terminal_growth"], 0.08), "terminal_growth_bounds"),
        ("valuation", _set(["beta", "floor"], 2.0), "floor must be < cap"),
        ("valuation", _set(["size_premium", 2, "max_mcap"], 50000), "max_mcap: null"),
        ("valuation", _set(["size_premium", 0, "max_mcap"], 30000), "strictly increasing"),
        ("valuation", _delete(["mos_by_grade", "B"]), "mos_by_grade.B"),
        ("valuation", _set(["zones", "fair_upper_mult"], 0.9), "greater than 1"),
        ("valuation", _delete(["dcf", "scenarios", "bear"]), "bear"),
        ("valuation", _set(["confidence", "medium_if_method_cv_above"], 0.5), "medium_if"),
        ("valuation", _set(["dcf", "reverse_growth_bracket"], [1.0, -0.5]), "reverse_growth"),
        ("valuation", _delete(["blend"]), "blend"),
        ("sectors", _delete(["nbfc", "long_run_growth"]), "long_run_growth"),
        # sectors
        ("sectors", _delete(["default"]), "'default'"),
        ("sectors", _set(["it_services", "weights", "dcf_base"], 0.5), "sum to 1.0"),
        ("sectors", _set(["banks", "weights"], {"dcf_base": 0.5, "band_pb": 0.5}), "FCFF"),
        ("sectors", _set(["fmcg", "model"], "magic"), "model"),
        ("sectors", _set(["fmcg", "weights"], {"dcf_bse": 1.0}), "weights"),
        ("sectors", _delete(["metals", "normalise_years"]), "normalise_years"),
        ("sectors", _delete(["real_estate", "nav_discount"]), "nav_discount"),
        # scoring
        ("scoring", _set(["weights", "quality"], 30), "sum to 100"),
        ("scoring", _set(["grade_cutoffs", "B"], 80), "strictly descending"),
        ("scoring", _set(["maps", "roce_5y_avg"], [[0.1, 0], [0.1, 50]]), "monotonic"),
        ("scoring", _set(["maps", "piotroski"], [[3, 0], [9, 120]]), "0..100"),
        ("scoring", _set(["maps", "piotroski"], [[3, 0]]), "at least 2 points"),
        ("scoring", _set(["maps", "stage_score"], {1: 50, 2: 100, 3: 30}), "stages 1, 2, 3"),
        ("scoring", _delete(["maps", "rs_percentile"]), "rs_percentile"),
        ("scoring", _set(["knockouts", "cap_grade"], "E"), "cap_grade"),
        ("scoring", _set(["earned_premium", "momentum_entry_min"], 9), "less than or equal"),
        ("scoring", _set(["forensic", "altman_safe_above"], 1.0), "altman_distress_below"),
        ("scoring", _set(["fundamentals", "days_in_year"], 400), "days_in_year"),
        ("scoring", _set(["fundamentals", "cagr_years"], []), "cagr_years"),
        ("scoring", _set(["knockouts", "negative_cfo_years_in_5"], 6), "negative_cfo_window"),
        ("scoring", _set(["maps", "trend_score"], {"up": 100, "down": 0}), "up, range and down"),
        ("scoring", _set(["maps", "rpt_score"], {"clean": 100}), "clean and flagged"),
        ("scoring", _delete(["decision", "matrix", "C"]), "row for every grade"),
        ("scoring", _delete(["decision", "matrix", "B", "fair"]), "row B must have a cell"),
        ("scoring", _set(["decision", "matrix", "A", "fair"], "yolo"), "matrix.A.fair"),
        ("scoring", _set(["decision", "confirmation"], {"stages": [], "trends": []}), "at least"),
        ("scoring", _set(["decision", "checklists", "why_cheap"], []), "why_cheap"),
        ("scoring", _set(["pillar_min_coverage"], 1.5), "pillar_min_coverage"),
        # jobs
        ("jobs", _set(["schedules", "eod_prices"], "61 18 * * *"), "invalid cron"),
        ("jobs", _delete(["schedules", "nse_bhavcopy"]), "schedules missing jobs"),
        ("jobs", _set(["schedules", "made_up_job"], "0 1 * * *"), "schedules"),
        ("jobs", _set(["timezone"], "Mars/Olympus"), "unknown timezone"),
        ("jobs", _set(["universe_index"], "NIFTY9000"), "universe_index"),
        ("jobs", _set(["shareholding_season", "days"], [25, 1]), "first, last"),
        ("jobs", _set(["alerts", "market_open"], "16:00"), "market_open must be before"),
        ("jobs", _set(["alerts", "hysteresis_pct"], 2), "less than or equal to 1"),
        ("jobs", _delete(["alerts"]), "alerts"),
        ("jobs", _set(["results_watch", "max_downloads_per_run"], 0), "greater than 0"),
        ("jobs", _delete(["results_watch"]), "results_watch"),
        ("jobs", _set(["backtest", "cost_per_side"], 1.5), "cost_per_side"),
        ("jobs", _set(["backtest", "execution_lag_days"], 9), "execution_lag_days"),
        ("jobs", _set(["backtest", "equity_curve_points"], "hourly"), "equity_curve_points"),
        ("jobs", _delete(["backtest"]), "backtest"),
        # technical
        ("technical", _set(["ote_retracement"], [0.79, 0.618]), "ote_retracement"),
        ("technical", _set(["major_swing_fractal_n"], 1), "major_swing_fractal_n"),
        ("technical", _set(["vcp", "min_contractions"], 1), "min_contractions"),
        ("technical", _set(["atr_period"], 0), "greater than 0"),
        ("technical", _set(["avwap_anchors"], ["low_52w", "ipo_date"]), "avwap_anchors"),
        ("technical", _delete(["rsi_period"]), "rsi_period"),
    ],
)
def test_invalid_config_fails_fast(
    config_dir: Path, file: str, mutator: Mutator, message: str
) -> None:
    _mutate(config_dir, file, mutator)
    with pytest.raises(ConfigError, match=message):
        load_config(config_dir)


# ───────────── piecewise map ─────────────


def test_piecewise_map_accepts_decreasing_x() -> None:
    m = PiecewiseLinearMap.model_validate([[1.5, 0], [1.0, 30], [0.1, 100]])
    assert m.root[0] == (1.5, 0.0)
