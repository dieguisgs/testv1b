"""Synthetic evidence for the small model's hierarchy and information boundary."""

from dataclasses import replace
from datetime import date, timedelta
import json

import pandas as pd
import pytest

from vwapsb.config import Config, Model
from vwapsb.engine import fill_curves, lead_steps
from vwapsb.io_eex import EexBook, REQUIRED
from vwapsb.mapping import ProductMap
from vwapsb.tenors import resolve_tenor


DAY = date(2026, 10, 6)
OBS_COLUMNS = ["date", "product", "region", "unit", "tenor", "vwap", "volume"]


def mapping(**changes):
    return replace(ProductMap("P", "fill", "DE", "Base", "DE/Base.csv", "Base", "Europe/Berlin"), **changes)


def observations(*rows):
    return pd.DataFrame([(day, "P", "", "", label, price, volume)
                         for day, label, price, volume in rows], columns=OBS_COLUMNS)


def book(*rows):
    return EexBook(pd.DataFrame(rows, columns=REQUIRED))


def relative_book(day, prices):
    return book(*[(day, period.kind, period.start, price) for label, price in prices.items()
                  if (period := resolve_tenor(label, day)) is not None])


def run(tmp_path, own, eex, *, targets=None, model=None, day=DAY, **options):
    cfg = Config(root=tmp_path, targets=targets or ["M+2"], model=model or Model(), **options)
    product = mapping()
    return fill_curves(own, {product.key: eex}, [product], cfg, [day]).set_index("tenor")


def details(row):
    return json.loads(row.details)


def stage(row, name):
    return next(item for item in details(row)["stages"] if item["stage"] == name)


def test_original_aliases_duplicates_and_settings_are_preserved(tmp_path):
    own = observations((DAY, "Cal+1", 100, 10), (DAY, "Y+1", 140, 30),
                       (DAY, "Cal+1", -1000, 0), (DAY, "Y+1", 999, None))
    output = run(tmp_path, own, None, targets=["Cal+1", "Y+1", "Cal + 1"])
    assert output.price.tolist() == [130, 130, 130]
    assert output.method.tolist() == ["original", "original", "own_reused"]
    assert set(output.status) == {"original"}
    assert len(details(output.iloc[0])["own"]["observations"]) == 4
    assert details(output.iloc[0])["settings"]["model"]["eex_window"] == 5
    assert details(output.iloc[0])["settings"]["mapping"]["eex_file"] == "DE/Base.csv"
    assert details(output.iloc[0])["settings"]["eex_offset_days"] == 0
    reordered = run(tmp_path, own.iloc[::-1], None, targets=["Cal+1", "Y+1", "Cal + 1"])
    pd.testing.assert_frame_equal(output, reordered)


@pytest.mark.parametrize("prices,expected", [([0, -10], -5), ([1e308, 1e308], 1e308)])
def test_duplicate_mean_without_positive_volume(tmp_path, prices, expected):
    output = run(tmp_path, observations(*[(DAY, "M+2", price, None) for price in prices]), None)
    assert output.loc["M+2", "price"] == expected
    assert output.loc["M+2", "status"] == "original"


def test_local_uses_same_family_median_without_shrink_or_volume_weighting(tmp_path):
    own = observations((DAY, "M+1", 110, 10000), (DAY, "M+3", 330, 1),
                       (DAY, "M+4", 1400, 1), (DAY, "Q+1", 10000, 1000), (DAY, "D+1", 20000, 1000))
    eex = relative_book(DAY, {"M+1": 100, "M+2": 200, "M+3": 300, "M+4": 400, "Q+1": 500, "D+1": 50})
    row = run(tmp_path, own, eex).loc["M+2"]
    assert row.method == "local"
    assert row.price == 230
    assert row.adjustment == 30
    assert row.local_count == 3
    assert {item["reason"] for item in stage(row, "local")["rejected"]} == {"different_kind"}
    assert [item["stage"] for item in details(row)["stages"]] == ["original", "local"]


@pytest.mark.parametrize("volume,threshold,method", [(None, 0, "local"), (None, 1, "missing"), (0, 0, "local"), (1, 2, "missing")])
def test_local_volume_and_minimum_anchor_rules(tmp_path, volume, threshold, method):
    own = observations((DAY, "M+1", 5, volume))
    eex = relative_book(DAY, {"M+1": -5, "M+2": 0})
    row = run(tmp_path, own, eex, model=Model(min_volume=threshold)).loc["M+2"]
    assert row.method == method
    if method == "local":
        assert row.price == 10
    two_required = run(tmp_path, own, eex, model=Model(local_min_anchors=2)).loc["M+2"]
    assert two_required.method == "missing"


def test_local_radius_and_family_keep_far_anchors_out(tmp_path):
    own = observations((DAY, "M+1", 110, 1), (DAY, "Q+1", 510, 1))
    eex = relative_book(DAY, {"M+1": 100, "M+4": 200, "Q+1": 500})
    row = run(tmp_path, own, eex, targets=["M+4"]).loc["M+4"]
    assert row.method == "missing"
    assert {item["reason"] for item in stage(row, "local")["rejected"]} == {"different_kind", "outside_radius"}


def test_history_daily_medians_give_each_date_equal_weight(tmp_path):
    days = [DAY - timedelta(days=n) for n in (3, 2, 1)]
    own_rows, eex_rows = [], []
    for day, points in zip(days, [{"M+1": 10, "M+2": 20, "M+3": 30}, {"M+2": 100}, {"M+2": 200}]):
        for label, adjustment in points.items():
            own_rows.append((day, label, 100 + adjustment, 10))
            period = resolve_tenor(label, day)
            eex_rows.append((day, period.kind, period.start, 100))
    target = resolve_tenor("M+2", DAY)
    eex_rows.append((DAY, target.kind, target.start, 300))
    row = run(tmp_path, observations(*own_rows), book(*eex_rows)).loc["M+2"]
    assert row.method == "history"
    assert row.price == 400  # median([20, 100, 200]), not all five quote differences.
    assert row.history_days == 3
    assert [item["daily_median"] for item in stage(row, "history")["days"]] == [20, 100, 200]


@pytest.mark.parametrize("tenor,previous,today", [
    ("M+1", date(2026, 9, 30), date(2026, 10, 1)),
    ("Q+1", date(2026, 9, 30), date(2026, 10, 1)),
    ("Cal+1", date(2026, 12, 31), date(2027, 1, 1)),
])
def test_history_compares_relative_calendar_lead_through_rollover(tmp_path, tenor, previous, today):
    past, target = resolve_tenor(tenor, previous), resolve_tenor(tenor, today)
    assert past.start != target.start
    assert lead_steps(past, previous) == lead_steps(target, today) == 1
    eex = book((previous, past.kind, past.start, 100), (today, target.kind, target.start, 200))
    row = run(tmp_path, observations((previous, tenor, 110, 1)), eex, targets=[tenor], day=today,
              model=Model(local_max_steps=0, history_min_days=1)).loc[tenor]
    assert row.method == "history"
    assert row.price == 210
    assert stage(row, "history")["target_lead_steps"] == 1


def test_history_requires_exact_publication_and_inclusive_window_boundary(tmp_path):
    old = DAY - timedelta(days=3)
    near = DAY - timedelta(days=1)
    target = resolve_tenor("M+2", DAY)
    own = observations((old, "M+2", 110, 1), (near, "M+2", 1000, 1))
    eex = book((old, "Month", target.start, 100), (DAY, "Month", target.start, 200))
    model = Model(history_window_days=3, history_min_days=1)
    row = run(tmp_path, own, eex, model=model).loc["M+2"]
    assert row.price == 210
    assert row.history_days == 1  # Missing exact publication on near cannot use old's EEX.
    assert row.history_start == old
    short = run(tmp_path, own, eex, model=replace(model, history_window_days=2)).loc["M+2"]
    assert short.method == "missing"


@pytest.mark.parametrize("offset,expected", [(-1, 220), (-2, 110)])
def test_offsets_gate_current_snapshot_and_historical_training(tmp_path, offset, expected):
    before = DAY - timedelta(days=2)
    yesterday = DAY - timedelta(days=1)
    target = resolve_tenor("M+2", DAY)
    eex = book((before, "Month", target.start, 100), (yesterday, "Month", target.start, 200),
               (DAY, "Month", target.start, 10000))
    own = observations((before, "M+2", 110, 1), (yesterday, "M+2", 230, 1),
                       (DAY + timedelta(days=1), "M+2", 1e308, 1e308))
    row = run(tmp_path, own, eex, model=Model(history_min_days=1), eex_offset_days=offset).loc["M+2"]
    assert row.method == "history"
    assert row.price == expected
    assert row.eex_cutoff == DAY + timedelta(days=offset)
    assert row.history_end <= row.eex_cutoff
    assert row.eex_date <= row.eex_cutoff


@pytest.mark.parametrize("average,expected", [("simple", 200), ("ewma", 1700 / 7)])
def test_fixed_contract_average_keeps_delivery_unchanged_across_month_roll(tmp_path, average, expected):
    today = date(2026, 10, 1)
    target = date(2026, 11, 1)
    days = [date(2026, 9, 29), date(2026, 9, 30), today]
    eex = book(*[(day, "Month", target, px) for day, px in zip(days, [100, 200, 300])],
               (days[0], "Month", date(2026, 10, 1), 999), (days[1], "Month", date(2026, 10, 1), 999))
    row = run(tmp_path, observations(), eex, targets=["M+1"], day=today,
              model=Model(eex_window=3, eex_average=average, eex_halflife=1)).loc["M+1"]
    assert row.method == f"eex_{average}"
    assert row.price == pytest.approx(expected)
    assert [item["price"] for item in stage(row, "eex_average")["observations"]] == [100, 200, 300]
    assert stage(row, "eex_average")["delivery_start"] == "2026-11-01"


def test_average_does_not_skip_missing_contract_publications(tmp_path):
    target = resolve_tenor("M+2", DAY)
    eex = book((DAY - timedelta(days=2), "Month", target.start, 100),
               (DAY - timedelta(days=1), "Year", date(2027, 1, 1), 200),
               (DAY, "Month", target.start, 300))
    row = run(tmp_path, observations(), eex, model=Model(eex_window=2)).loc["M+2"]
    assert row.method == "missing"
    assert row.reason == "missing_contract_in_window"
    assert stage(row, "eex_average")["missing_contract_dates"] == [str(DAY - timedelta(days=1))]


def test_average_underflow_never_degenerates_to_raw_latest_eex(tmp_path):
    target = resolve_tenor("M+2", DAY)
    eex = book((DAY - timedelta(days=1), "Month", target.start, 100), (DAY, "Month", target.start, 200))
    row = run(tmp_path, observations(), eex, model=Model(eex_window=2, eex_halflife=1e-320)).loc["M+2"]
    assert row.method == "missing"
    assert row.reason == "degenerate_averaging_weights"


def test_constant_average_may_equal_reference_without_raw_fallback(tmp_path):
    target = resolve_tenor("M+2", DAY)
    eex = book((DAY - timedelta(days=1), "Month", target.start, 100), (DAY, "Month", target.start, 100))
    row = run(tmp_path, observations(), eex, model=Model(eex_window=2)).loc["M+2"]
    assert row.method == "eex_ewma"
    assert row.price == row.eex_price == 100


def test_staleness_uses_latest_age_not_older_window_dates(tmp_path):
    target = resolve_tenor("M+2", DAY)
    eex = book((DAY - timedelta(days=30), "Month", target.start, 100),
               (DAY - timedelta(days=1), "Month", target.start, 200))
    row = run(tmp_path, observations(), eex, model=Model(eex_window=2), max_stale_days=1).loc["M+2"]
    assert row.method == "eex_ewma"
    unlimited = run(tmp_path, observations(), eex, day=DAY + timedelta(days=10),
                    model=Model(eex_window=2), max_stale_days=0).loc["M+2"]
    assert unlimited.method == "eex_ewma"
    stale = run(tmp_path, observations(), eex, day=DAY + timedelta(days=10),
                model=Model(eex_window=2), max_stale_days=7).loc["M+2"]
    assert stale.reason == "no_publication_within_cutoff_and_age"


def test_no_local_or_history_without_current_target_reference(tmp_path):
    previous = DAY - timedelta(days=1)
    own = observations((previous, "M+2", 110, 1), (DAY, "M+1", 120, 1))
    eex = book((previous, "Month", resolve_tenor("M+2", DAY).start, 100),
               (DAY, "Month", resolve_tenor("M+1", DAY).start, 100))
    row = run(tmp_path, own, eex, model=Model(history_min_days=1, eex_window=2)).loc["M+2"]
    assert row.method == "missing"
    assert stage(row, "local")["outcome"] == stage(row, "history")["outcome"] == "target_eex_unavailable"


@pytest.mark.parametrize("day,alias", [(date(2026, 9, 26), "BOW"), (date(2026, 9, 29), "BOM")])
def test_estimated_day_aliases_share_one_canonical_result(tmp_path, day, alias):
    own = observations((day, "D+2", 140, 1))
    eex = relative_book(day, {"D+1": 100, "D+2": 120})
    output = run(tmp_path, own, eex, targets=[alias, "D+1"], day=day)
    assert output.loc[alias, "price"] == output.loc["D+1", "price"] == 120
    assert output.loc[alias, "method"] == output.loc["D+1", "method"] == "local"
    assert details(output.loc[alias])["equivalent_target"]["tenor"] == "D+1"
    opposite = run(tmp_path, own, eex, targets=["D+1", alias], day=day)
    pd.testing.assert_frame_equal(output.sort_index().drop(columns=["configuration_id", "details"]),
                                  opposite.sort_index().drop(columns=["configuration_id", "details"]), check_like=True)
    assert stage(output.loc[alias], "local") == stage(opposite.loc[alias], "local")


def test_original_day_aliases_keep_original_provenance(tmp_path):
    today = date(2026, 9, 26)
    output = run(tmp_path, observations((today, "BOW", 88, 1)), None, targets=["D+1", "BOW"], day=today)
    assert output.loc["D+1", "method"] == "own_reused"
    assert output.loc["BOW", "method"] == "original"
    assert set(output.price) == {88}


def test_peak_zero_energy_alias_does_not_reuse_positive_day(tmp_path):
    today = date(2026, 9, 26)
    product = mapping(hours="Peak")
    cfg = Config(root=tmp_path, targets=["D+1", "BOW"])
    output = fill_curves(observations((today, "D+1", 88, 1)), {}, [product], cfg, [today]).set_index("tenor")
    assert output.loc["D+1", "method"] == "original"
    assert output.loc["BOW", "method"] == "missing"
    assert output.loc["BOW", "reason"] == "zero_delivery_hours"


def test_individual_model_and_full_identity_do_not_leak_between_curves(tmp_path):
    own = observations((DAY, "M+1", 110, 1))
    other = own.assign(region="GB", vwap=9999)
    products = [mapping(parameter_overrides={"local_enabled": False}), mapping(region="GB", use="off")]
    eex = relative_book(DAY, {"M+1": 100, "M+2": 200})
    cfg = Config(root=tmp_path, targets=["M+2"], configuration_mode="individual")
    output = fill_curves(pd.concat([own, other]), {products[0].key: eex}, products, cfg, [DAY])
    assert set(output.region) == {""}
    assert output.loc[output.tenor == "M+2", "method"].iloc[0] == "missing"
    global_output = fill_curves(own, {products[0].key: eex}, products, replace(cfg, configuration_mode="global"), [DAY])
    assert global_output.loc[global_output.tenor == "M+2", "price"].iloc[0] == 210
    assert cfg.model.local_enabled is True


def test_batch_and_separate_dates_are_identical_and_estimates_do_not_train(tmp_path):
    tomorrow = DAY + timedelta(days=1)
    own = observations((DAY, "M+1", 110, 1))
    target = resolve_tenor("M+2", DAY)
    eex = book((DAY, "Month", target.start, 200), (tomorrow, "Month", target.start, 300),
               (DAY, "Month", resolve_tenor("M+1", DAY).start, 100))
    product = mapping()
    cfg = Config(root=tmp_path, targets=["M+2"], model=Model(history_enabled=False, eex_window=2, eex_average="simple"))
    batch = fill_curves(own, {product.key: eex}, [product], cfg, [DAY, tomorrow])
    separate = pd.concat([fill_curves(own, {product.key: eex}, [product], cfg, [day]) for day in [DAY, tomorrow]], ignore_index=True)
    pd.testing.assert_frame_equal(batch, separate)
    final = batch.loc[(batch.date == tomorrow) & (batch.tenor == "M+2")].iloc[0]
    assert final.method == "eex_simple"
    assert final.price == pytest.approx(250)


def test_invalid_dates_fail_explicitly(tmp_path):
    with pytest.raises(ValueError, match="nonmissing"):
        run(tmp_path, observations((pd.NaT, "M+1", 1, 1)), None)
