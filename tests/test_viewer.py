"""Saved-row visualization preserves identity, candidates and delivery rolls."""

from datetime import date

import pandas as pd
import pytest

from vwapsb.viewer import create_viewer, load_output, prepare_frame, range_means, select_rows


def records():
    return pd.DataFrame([
        dict(date=date(2026, 9, 30), product="P", region="DE", unit="EUR/MWh", tenor="M+2", kind="Month",
             delivery_start=date(2026, 11, 1), delivery_end=date(2026, 12, 1), price=110.0, eex_price=100.0),
        dict(date=date(2026, 10, 1), product="P", region="DE", unit="EUR/MWh", tenor="M+1", kind="Month",
             delivery_start=date(2026, 11, 1), delivery_end=date(2026, 12, 1), price=130.0, eex_price=120.0),
        dict(date=date(2026, 10, 2), product="P", region="DE", unit="EUR/MWh", tenor="M+1", kind="Month",
             delivery_start=date(2026, 11, 1), delivery_end=date(2026, 12, 1), price=999.0, eex_price=float("nan")),
        dict(date=date(2026, 10, 1), product="P", region="DE", unit="EUR/MWh", tenor="Q+1", kind="Quarter",
             delivery_start=date(2027, 1, 1), delivery_end=date(2027, 4, 1), price=140.0, eex_price=135.0),
    ])


def test_range_mean_uses_paired_dates_and_absolute_period_across_tenor_roll():
    frame = prepare_frame(records())
    result = range_means(frame)
    month = result.loc[result.kind.eq("Month")].iloc[0]
    assert month.price == 120 and month.eex_price == 110
    assert month.n_dates == 3 and month.n_paired_dates == 2
    assert month.tenor == "M+1/M+2"


def test_aliases_do_not_double_weight_a_publication_date():
    frame = records()
    alias = frame.iloc[[0]].assign(tenor="M+02")
    month = range_means(prepare_frame(pd.concat([frame, alias]))).query("kind == 'Month'").iloc[0]
    assert month.price == 120 and month.n_paired_dates == 2


def test_csv_identity_preserves_leading_zeroes_and_na_like_literals(tmp_path):
    frame = records().iloc[:1].assign(product="001", region="NaN", unit="NA")
    path = tmp_path / "filled.csv"
    frame.to_csv(path, index=False)
    loaded = load_output(path)
    assert loaded.loc[0, "product"] == "001"
    assert loaded.loc[0, "region"] == "NaN"
    assert loaded.loc[0, "unit"] == "NA"


def test_candidate_and_exact_identity_selection_prevents_accidental_mixing():
    a = records().assign(candidate_id="candidate_001")
    b = records().assign(candidate_id="candidate_002", price=999.0)
    other = a.assign(unit="GBP/MWh")
    frame = prepare_frame(pd.concat([a, b, other]))
    options = dict(start=date(2026, 9, 1), end=date(2026, 10, 31))
    with pytest.raises(ValueError, match="candidate_id"):
        select_rows(frame, ("P", "DE", "EUR/MWh"), **options)
    selected = select_rows(frame, ("P", "DE", "EUR/MWh"), candidate_id="candidate_001", **options)
    assert len(selected) == 4 and selected.price.iloc[0] == 110
    with pytest.raises(ValueError, match="one selected candidate"):
        range_means(frame)


def test_all_kind_viewer_has_both_tabs_and_does_not_drop_contract_families():
    pytest.importorskip("ipywidgets")
    pytest.importorskip("plotly")
    viewer = create_viewer(records())
    _, _, curve, kind, tabs = viewer.children
    assert kind.value is None
    assert {value for _, value in kind.options} == {None, "Month", "Quarter"}
    assert tabs.get_title(0) == "Single date" and tabs.get_title(1) == "Range means"
    assert curve.value == ("P", "DE", "EUR/MWh")
    viewer.close()
