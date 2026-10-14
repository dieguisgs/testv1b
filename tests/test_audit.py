"""Consistency audit respects complete physical coverage and delivery hours."""

from dataclasses import replace
from datetime import date, timedelta
import json

import pandas as pd
import pytest

from vwapsb.audit import audit_consistency
from vwapsb.hours import hours_fn
from vwapsb.mapping import ProductMap


PRODUCT = ProductMap("P", "fill", "DE", "Base", "DE/Base.csv", "Base", "Europe/Berlin")
DAY = date(2026, 1, 5)


def curve(*rows):
    return pd.DataFrame([dict(date=DAY, product="P", region="", unit="", tenor=tenor, kind=kind,
                              delivery_start=start, delivery_end=end, price=price, status="estimated")
                         for tenor, kind, start, end, price in rows])


def months_and_quarter():
    return curve(("Q+0", "Quarter", date(2026, 1, 1), date(2026, 4, 1), 100),
                 ("M+0", "Month", date(2026, 1, 1), date(2026, 2, 1), 120),
                 ("M+1", "Month", date(2026, 2, 1), date(2026, 3, 1), 90),
                 ("M+2", "Month", date(2026, 3, 1), date(2026, 4, 1), 80))


def test_month_quarter_average_uses_actual_dst_hours_without_changing_prices():
    frame = months_and_quarter()
    original = frame.copy(deep=True)
    result = audit_consistency(frame, [PRODUCT])
    assert len(result) == 1
    expected = (744 * 120 + 672 * 90 + 743 * 80) / (744 + 672 + 743)
    assert result.iloc[0].children_price == pytest.approx(expected)
    assert result.iloc[0].difference == pytest.approx(100 - expected)
    assert [item["hours"] for item in json.loads(result.iloc[0].children)] == [744, 672, 743]
    pd.testing.assert_frame_equal(frame, original)


def test_partial_or_overlapping_coverage_does_not_create_a_comparison():
    frame = months_and_quarter().iloc[[0, 1, 3]]
    assert audit_consistency(frame, [PRODUCT]).empty
    overlap = curve(("whole", "Quarter", date(2026, 1, 1), date(2026, 4, 1), 100),
                    ("left", "Month", date(2026, 1, 1), date(2026, 3, 1), 100),
                    ("right", "Month", date(2026, 2, 1), date(2026, 4, 1), 100))
    assert audit_consistency(overlap, [PRODUCT]).empty


def test_year_uses_shortest_disjoint_cover_and_never_double_counts_months():
    frame = months_and_quarter()
    extra = curve(("Cal", "Year", date(2026, 1, 1), date(2027, 1, 1), 100),
                  ("Q+1", "Quarter", date(2026, 4, 1), date(2026, 7, 1), 110),
                  ("Q+2", "Quarter", date(2026, 7, 1), date(2026, 10, 1), 120),
                  ("Q+3", "Quarter", date(2026, 10, 1), date(2027, 1, 1), 130))
    result = audit_consistency(pd.concat([frame, extra]), [PRODUCT]).set_index("parent_tenor")
    children = json.loads(result.loc["Cal", "children"])
    assert [item["tenor"] for item in children] == ["Q+0", "Q+1", "Q+2", "Q+3"]
    assert sum(item["weight"] for item in children) == pytest.approx(1)


def test_saved_csv_dates_and_aliases_can_be_mixed_with_new_rows():
    frame = months_and_quarter()
    saved = frame.iloc[:2].copy()
    for name in ("date", "delivery_start", "delivery_end"):
        saved[name] = saved[name].astype(str)
    duplicate = frame.iloc[[1]].assign(tenor="month alias")
    combined = pd.concat([saved, frame.iloc[2:], duplicate])
    result = audit_consistency(combined, [PRODUCT])
    assert len(result) == 1
    assert len(json.loads(result.iloc[0].children)) == 3
    with pytest.raises(ValueError, match="Conflicting prices"):
        audit_consistency(pd.concat([combined, duplicate.assign(price=999)]), [PRODUCT])


def test_peak_week_requires_only_weekdays_and_ignores_weekend_day_prices():
    monday = date(2026, 1, 5)
    frame = curve(("W+0", "Week", monday, monday + timedelta(days=7), 100),
                  *[(f"D+{n}", "Day", monday + timedelta(days=n), monday + timedelta(days=n + 1),
                     100 if n < 5 else 9999) for n in range(7)])
    result = audit_consistency(frame, [replace(PRODUCT, hours="Peak")])
    assert len(result) == 1
    assert result.iloc[0].difference == pytest.approx(0)
    assert len(json.loads(result.iloc[0].children)) == 5


def test_peak_child_with_different_energy_profile_is_not_a_valid_strip():
    saturday = date(2026, 1, 10)
    # A Weekend includes Saturday and Sunday, while the same-date BOW has zero
    # weekday Peak energy and cannot stand in for the quoted Weekend contract.
    frame = curve(("weekend", "Weekend", saturday, saturday + timedelta(days=2), 100),
                  ("remainder", "BOW", saturday, saturday + timedelta(days=2), 200),
                  ("sat", "Day", saturday, saturday + timedelta(days=1), 90),
                  ("sun", "Day", saturday + timedelta(days=1), saturday + timedelta(days=2), 110))
    result = audit_consistency(frame, [replace(PRODUCT, hours="Peak")])
    assert len(result) == 1
    assert result.iloc[0].parent_tenor == "weekend"
    assert result.iloc[0].difference == pytest.approx(0)


def test_identity_and_missing_status_do_not_cross_contaminate():
    frame = months_and_quarter()
    foreign = frame.assign(region="OTHER", price=999)
    incomplete = frame.copy()
    incomplete.loc[incomplete.tenor == "M+1", "status"] = "missing"
    result = audit_consistency(pd.concat([incomplete, foreign]), [PRODUCT, replace(PRODUCT, region="OTHER")])
    assert len(result) == 1
    assert result.iloc[0].region == "OTHER"
    assert result.iloc[0].difference == 0


@pytest.mark.parametrize("bad_mapping", [replace(PRODUCT, hours="BAD"), replace(PRODUCT, timezone="No/Such_Zone")])
def test_invalid_delivery_convention_fails_explicitly(bad_mapping):
    with pytest.raises((ValueError, KeyError)):
        audit_consistency(months_and_quarter(), [bad_mapping])


def test_nonfinite_estimated_and_unmapped_rows_fail_explicitly():
    frame = months_and_quarter()
    with pytest.raises(ValueError, match="unmapped"):
        audit_consistency(frame, [])
    frame["price"] = frame.price.astype(float)
    frame.loc[0, "price"] = float("inf")
    with pytest.raises(ValueError, match="finite"):
        audit_consistency(frame, [PRODUCT])
