"""Backtests hide physical observations and keep a comparable case universe."""

from dataclasses import replace
from datetime import date, timedelta
import json

import pandas as pd
import pytest

import vwapsb.backtest as backtest
from vwapsb.config import Config, Model
from vwapsb.io_eex import EexBook
from vwapsb.mapping import ProductMap


@pytest.fixture
def dataset(tmp_path):
    cfg = Config(tmp_path, targets=["M+1", "M+2"],
                 model=Model(history_enabled=False, eex_window=2))
    mapping = ProductMap("P", "fill", "DE", "Base", "DE/Base.csv", "Base", "Europe/Berlin",
                         region="DE", unit="EUR/MWh")
    days = [date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30)]
    observations = pd.DataFrame([
        {"date": day, "product": "P", "region": "DE", "unit": "EUR/MWh", "tenor": tenor,
         "vwap": price, "volume": 10.0}
        for day in days for tenor, price in (("M+1", 110.0), ("M+2", 220.0))])
    quotes = pd.DataFrame([
        {"tradeDate": day, "maturityType": "Month", "deliveryStart": date(2026, month, 1), "settlPx": price}
        for day in [days[0] - timedelta(days=3), *days] for month, price in ((10, 100.0), (11, 200.0))])
    books = {mapping.key: EexBook(quotes)}
    return cfg, mapping, days, observations, books


def execute(dataset, **kwargs):
    cfg, mapping, days, observations, books = dataset
    return backtest.run_backtest(observations, books, [mapping], cfg, days[0], days[-1], **kwargs)


def test_aliases_and_duplicates_are_removed_together_and_truth_uses_positive_volumes(dataset):
    cfg, mapping, days, own, books = dataset
    first = own.loc[own.date.eq(days[0]) & own.tenor.eq("M+1")].iloc[0].to_dict()
    own.loc[own.date.eq(days[0]) & own.tenor.eq("M+1"), "volume"] = 1.0
    own = pd.concat([own, pd.DataFrame([{**first, "tenor": "M+01", "vwap": 130.0, "volume": 3.0},
                                      {**first, "vwap": 999.0, "volume": 0.0}])], ignore_index=True)
    result = execute((cfg, mapping, days, own, books))
    rows = result["scores"]
    held = rows.loc[rows.date.eq(days[0]) & rows.tenor.eq("M+01")].iloc[0]
    assert held.actual == pytest.approx(125.0) and held.price == pytest.approx(120.0)
    assert held.hidden_rows == 3 and json.loads(held.hidden_aliases) == ["M+01", "M+1"]
    assert held.status == "estimated" and held.method == "local"
    assert len(rows) == 6  # Aliases/duplicate input rows do not multiply exam cases.


def test_future_own_data_never_reaches_engine(dataset, monkeypatch):
    original = backtest.fill_curves
    calls = []

    def inspect(own, books, mappings, cfg, dates):
        assert own.empty or own.date.max() <= dates[0]
        calls.append(dates[0])
        return original(own, books, mappings, cfg, dates)

    monkeypatch.setattr(backtest, "fill_curves", inspect)
    result = execute(dataset)
    assert len(calls) == 6
    assert result["summary"].coverage.item() == 1.0


def test_negative_eex_offset_excludes_today_publication(dataset):
    cfg, mapping, days, own, books = dataset
    cfg = replace(cfg, eex_offset_days=-1)
    quotes = pd.DataFrame([
        {"tradeDate": day, "maturityType": "Month", "deliveryStart": date(2026, month, 1),
         "settlPx": price + (900.0 if day == days[-1] else 0.0)}
        for day in [days[0] - timedelta(days=3), *days] for month, price in ((10, 100.0), (11, 200.0))])
    report = execute((cfg, mapping, days, own, {mapping.key: EexBook(quotes)}))["scores"]
    final = report.loc[report.date.eq(days[-1])]
    assert set(final.eex_price) == {100.0, 200.0}
    assert final.eex_date.eq(days[-2]).all()


def test_candidate_filters_do_not_remove_truth_and_missing_predictions_reduce_coverage(dataset):
    cfg, mapping, days, own, books = dataset
    cfg = replace(cfg, model=replace(cfg.model, eex_window=20))
    result = execute((cfg, mapping, days, own, books), grid={"min_volume": [0.0, 1000.0]})
    summary = result["summary"].set_index("candidate_id")
    assert summary.n_cases.tolist() == [6, 6]
    assert summary.coverage.tolist() == [1.0, 0.0]
    assert summary.n_missing.tolist() == [0, 6]
    assert pd.isna(summary.loc["candidate_002", "mae"])
    assert result["scores"].candidate_id.value_counts().eq(6).all()


def test_fraction_samples_cases_once_not_simultaneous_masking(dataset):
    first = execute(dataset, grid={"local_max_steps": [1, 2]}, mask_fraction=0.5, seed=9)
    second = execute(dataset, grid={"local_max_steps": [1, 2]}, mask_fraction=0.5, seed=9)
    pd.testing.assert_frame_equal(first["scores"], second["scores"])
    keys = ["date", "product", "region", "unit", "delivery_start", "delivery_end"]
    a = first["scores"].query("candidate_id == 'candidate_001'")[keys].reset_index(drop=True)
    b = first["scores"].query("candidate_id == 'candidate_002'")[keys].reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b)
    assert len(a) == 3 and first["scores"].method.eq("local").all()


def test_units_are_not_pooled_and_inactive_products_are_ignored(dataset):
    cfg, mapping, days, own, books = dataset
    pound = replace(mapping, unit="GBP/MWh")
    off = replace(mapping, product="OFF", use="off")
    own = pd.concat([own, own.assign(unit="GBP/MWh", vwap=own.vwap + 30), own.assign(product="OFF")])
    result = backtest.run_backtest(own, {**books, pound.key: books[mapping.key]}, [mapping, pound, off],
                                   cfg, days[0], days[-1])
    assert set(result["summary"].unit) == {"EUR/MWh", "GBP/MWh"}
    assert set(result["scores"]["product"]) == {"P"}
    assert len(result["by_curve"]) == 2


def test_grid_overrides_fixed_product_settings_but_leaves_unspecified_fields(dataset):
    cfg, mapping, days, own, books = dataset
    cfg = replace(cfg, configuration_mode="individual")
    mapping = replace(mapping, parameter_overrides={"local_enabled": False, "eex_halflife": 9.0})
    scores = execute((cfg, mapping, days, own, books), grid={"local_enabled": [True]})["scores"]
    assert scores.method.eq("local").all()
    assert all(json.loads(value)["eex_halflife"] == 9 for value in scores.parameters)
    assert mapping.parameter_overrides["local_enabled"] is False


@pytest.mark.parametrize("grid", [{"unknown": [1]}, {"eex_window": []}, {"eex_window": [1]},
                                 {"local_enabled": ["true"]}, {"local_max_steps": list(range(257))},
                                 {"eex_halflife": [float("nan")]}])
def test_invalid_grids_fail_before_engine(dataset, grid, monkeypatch):
    monkeypatch.setattr(backtest, "fill_curves", lambda *args: pytest.fail("Invalid grid reached the engine"))
    with pytest.raises(ValueError):
        execute(dataset, grid=grid)


def test_no_positive_volumes_uses_simple_mean_for_hidden_truth(dataset):
    cfg, mapping, days, own, books = dataset
    first = own.iloc[0].to_dict()
    own.loc[own.index[0], "volume"] = 0.0
    own = pd.concat([own, pd.DataFrame([{**first, "vwap": 130.0, "volume": float("nan")}])])
    score = execute((cfg, mapping, days, own, books))["scores"].iloc[0]
    assert score.actual == 120.0


def test_jointly_valid_grid_values_are_validated_as_complete_models():
    candidates = backtest.parameter_candidates(Model(), {"history_window_days": [2], "history_min_days": [1]})
    assert candidates == [{"history_window_days": 2, "history_min_days": 1}]


def test_individual_grid_uses_effective_product_base_instead_of_unused_global_defaults(dataset):
    cfg, mapping, days, own, books = dataset
    cfg = replace(cfg, configuration_mode="individual")
    mapping = replace(mapping, parameter_overrides={"history_min_days": 1})
    reports = execute((cfg, mapping, days, own, books), grid={"history_window_days": [2]})
    assert len(reports["scores"]) == 6
    assert all(json.loads(parameters)["history_min_days"] == 1 for parameters in reports["scores"].parameters)
