"""Report delivery-hour aggregation differences without adjusting any price."""

from __future__ import annotations

from collections import deque
from datetime import timedelta
import json
import math

import pandas as pd

from vwapsb.engine import _hours, _weighted_mean
from vwapsb.identity import curve_keys
from vwapsb.mapping import ProductMap


AUDIT_COLUMNS = ["date", "product", "region", "unit", "parent_tenor", "parent_price",
                 "children_price", "difference", "children"]


def _cover(parent: dict, candidates: list[dict], hours) -> list[dict] | None:
    """Find a deterministic disjoint complete cover using the fewest components."""
    start, end = parent["delivery_start"], parent["delivery_end"]
    by_start = {}
    for row in candidates:
        left, right = row["delivery_start"], row["delivery_end"]
        own_hours = hours(left, right, kind=row["kind"])
        parent_hours = hours(left, right, kind=parent["kind"])
        if (start <= left < right <= end and (left, right) != (start, end)
                and own_hours > 0 and own_hours == parent_hours):
            by_start.setdefault(left, []).append(row)
    for rows in by_start.values():
        rows.sort(key=lambda row: (-row["delivery_end"].toordinal(), row["tenor"]))
    paths = {start: []}
    queue = deque([start])
    while queue:
        left = queue.popleft()
        if left == end:
            return paths[left] or None
        choices = [(row["delivery_end"], row) for row in by_start.get(left, [])]
        tomorrow = left + timedelta(days=1)
        if tomorrow <= end and hours(left, tomorrow, kind=parent["kind"]) == 0:
            choices.append((tomorrow, None))
        for right, row in choices:
            path = paths[left] + ([row] if row is not None else [])
            if right in paths and len(paths[right]) <= len(path):
                continue
            paths[right] = path
            (queue.append if row is not None else queue.appendleft)(right)
    return None


def audit_consistency(output: pd.DataFrame, mappings: list[ProductMap]) -> pd.DataFrame:
    """Compare each quoted parent with one complete, disjoint child cover.

    Only narrower quoted intervals may be children. Overlapping months and
    quarters are never counted twice; equal interval aliases count once. Hours
    follow the parent's delivery convention, including DST and Peak weekends.
    Missing coverage produces no comparison, rather than a partial average.
    Input frames and mappings are never modified. Unknown mappings or invalid
    delivery conventions fail explicitly because their hours cannot be audited.
    """
    if output.empty:
        return pd.DataFrame(columns=AUDIT_COLUMNS)
    required = {"date", "product", "region", "unit", "tenor", "kind", "delivery_start", "delivery_end", "price"}
    if required - set(output):
        raise ValueError(f"Curve output lacks audit columns: {sorted(required - set(output))}")
    frame = output.copy()
    for name in ("date", "delivery_start", "delivery_end"):
        parsed = pd.to_datetime(frame[name], format="mixed", errors="coerce")
        if parsed.isna().any():
            raise ValueError(f"Invalid {name} in curve audit input")
        frame[name] = parsed.dt.date
    frame["price"] = pd.to_numeric(frame["price"], errors="coerce")
    finite = frame.price.map(math.isfinite)
    if "status" in frame:
        if ((frame.status != "missing") & ~finite).any():
            raise ValueError("Non-missing curve rows require finite prices for audit")
        finite &= frame.status != "missing"
    frame = frame.loc[finite]
    lookup = {mapping.key: mapping for mapping in mappings}
    if len(lookup) != len(mappings):
        raise ValueError("Duplicate mapping identities prevent unambiguous auditing")
    results = []
    for key, curve in frame.groupby(curve_keys(frame), sort=False):
        if key not in lookup:
            raise ValueError(f"Cannot audit an unmapped curve: {key}")
        hours = _hours(lookup[key])
        for day, daily in curve.groupby("date", sort=True):
            intervals = {}
            for row in daily.to_dict("records"):
                interval = (row["delivery_start"], row["delivery_end"],
                            hours(row["delivery_start"], row["delivery_end"], kind=row["kind"]))
                if interval[0] >= interval[1]:
                    raise ValueError("Audit delivery intervals must have a positive duration")
                previous = intervals.get(interval)
                if previous is not None and previous["price"] != row["price"]:
                    raise ValueError(f"Conflicting prices for equivalent delivery aliases on {day}: {interval}")
                if previous is None or row["tenor"] < previous["tenor"]:
                    intervals[interval] = row
            candidates = list(intervals.values())
            for parent in sorted(candidates, key=lambda row: (row["delivery_start"], row["delivery_end"], row["tenor"])):
                if parent["kind"] == "Day":
                    continue
                cover = _cover(parent, candidates, hours)
                if not cover:
                    continue
                weighted = [(row, hours(row["delivery_start"], row["delivery_end"], kind=parent["kind"])) for row in cover]
                weighted = [(row, weight) for row, weight in weighted if weight > 0]
                if not weighted:
                    continue
                total = math.fsum(weight for _, weight in weighted)
                expected = hours(parent["delivery_start"], parent["delivery_end"], kind=parent["kind"])
                if not math.isclose(total, expected, rel_tol=1e-12, abs_tol=1e-9):
                    raise ValueError("The child cover does not match the parent's delivery hours")
                price = _weighted_mean([row["price"] for row, _ in weighted], [weight for _, weight in weighted])
                difference = parent["price"] - price
                if not math.isfinite(difference):
                    raise ValueError("Audit discrepancy exceeds the finite numeric range")
                details = [{"tenor": row["tenor"], "kind": row["kind"],
                            "delivery_start": row["delivery_start"].isoformat(),
                            "delivery_end": row["delivery_end"].isoformat(), "price": row["price"],
                            "hours": weight, "weight": weight / total} for row, weight in weighted]
                results.append(dict(date=day, product=key[0], region=key[1], unit=key[2],
                                    parent_tenor=parent["tenor"], parent_price=parent["price"], children_price=price,
                                    difference=difference, children=json.dumps(details, sort_keys=True, allow_nan=False)))
    return pd.DataFrame(results, columns=AUDIT_COLUMNS)
