"""Compare finite model grids by hiding one observed physical contract at a time.

The examination sample is fixed before model filters are applied. All aliases
and duplicate rows for the held-out contract disappear together. Earlier own
observations remain available; future own observations are never passed to the
engine. Missing predictions count against coverage and remain in the results.
This is an isolated-gap examination, not a simulation of persistent gaps.
"""

from __future__ import annotations

from dataclasses import asdict, fields, replace
from datetime import date
from itertools import product
import json
import math
import random

import numpy as np
import pandas as pd

from vwapsb.config import Model, effective_model, model_from_overrides
from vwapsb.engine import fill_curves
from vwapsb.identity import curve_keys, normalize_identity
from vwapsb.hours import hours_fn
from vwapsb.tenors import resolve_tenor


MAX_CANDIDATES = 256
IDENTITY = ["product", "region", "unit"]
SCORE_COLUMNS = ["candidate_id", "grid_parameters", "parameters", "date", *IDENTITY,
                 "tenor", "kind", "delivery_start", "delivery_end", "price", "status", "method",
                 "eex_price", "actual", "error", "absolute_error", "squared_error", "hidden_rows",
                 "hidden_aliases"]
METRICS = ["n_cases", "n_predicted", "n_missing", "coverage", "mae", "rmse", "bias"]


def parameter_candidates(model: Model, grid: dict[str, list] | None) -> list[dict]:
    """Validate an explicit Cartesian grid, limited to 256 combinations."""
    if grid is None:
        return [{}]
    if not isinstance(grid, dict):
        raise ValueError("grid must be a dictionary of nonempty candidate lists")
    unknown = set(grid) - {field.name for field in fields(Model)}
    if unknown:
        raise ValueError(f"Unknown model grid parameters: {sorted(unknown)}")
    for name, values in grid.items():
        if not isinstance(values, list) or not values:
            raise ValueError(f"Grid parameter {name} requires a nonempty list")
    count = math.prod(len(values) for values in grid.values())
    if count > MAX_CANDIDATES:
        raise ValueError(f"Grid contains {count} combinations; maximum is {MAX_CANDIDATES}")
    names = list(grid)
    candidates = [dict(zip(names, values)) for values in product(*(grid[name] for name in names))]
    for candidate in candidates:
        model_from_overrides(model, candidate)
    return candidates


def configured_candidates(cfg, mappings, grid: dict[str, list] | None) -> list[dict]:
    """Validate candidates against actual active product bases, not unused defaults."""
    models = [effective_model(cfg, mapping) for mapping in mappings if mapping.active]
    if not models:
        raise ValueError("Backtest requires at least one active mapped curve")
    candidates = parameter_candidates(models[0], grid)
    for candidate in candidates:
        for model in models:
            model_from_overrides(model, candidate)
    return candidates


def _weighted_actual(rows: pd.DataFrame) -> float:
    prices = rows["vwap"].to_numpy(dtype=float)
    volumes = rows["volume"].to_numpy(dtype=float)
    positive = np.isfinite(volumes) & (volumes > 0)
    if positive.any():
        prices, weights = prices[positive], volumes[positive]
        weights = weights / weights.max()
    else:
        weights = np.ones(len(prices))
    scale = float(np.abs(prices).max())
    return 0.0 if scale == 0 else float(np.average(prices / scale, weights=weights)) * scale


def _cases(observations, mappings, cfg, start, end):
    required = {"date", "product", "region", "unit", "tenor", "vwap"}
    if required - set(observations):
        raise ValueError(f"Normalized observations lack columns: {sorted(required - set(observations))}")
    frame = observations.copy(deep=True).reset_index(drop=True)
    identity = normalize_identity(frame)
    frame[IDENTITY] = identity[IDENTITY]
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    frame["vwap"] = pd.to_numeric(frame["vwap"], errors="coerce")
    frame["volume"] = pd.to_numeric(frame.get("volume", pd.Series(float("nan"), index=frame.index)), errors="coerce")
    frame = frame.loc[frame.date.notna() & np.isfinite(frame.vwap)].copy()
    frame["_key"] = curve_keys(frame)
    active = {mapping.key: mapping for mapping in mappings if mapping.active}
    if len(active) != sum(mapping.active for mapping in mappings):
        raise ValueError("Duplicate active mapping identities")
    frame = frame.loc[frame["_key"].isin(active)].copy()
    periods = [resolve_tenor(str(label), day, cfg.day_convention, cfg.weekend_offset)
               for label, day in zip(frame.tenor, frame.date)]
    hours = {key: hours_fn(mapping.hours, mapping.timezone) for key, mapping in active.items()}
    physical = [None if period is None else
                (period.start, period.end, hours[key](period.start, period.end, kind=period.kind))
                for key, period in zip(frame["_key"], periods)]
    frame["_period"] = [None if value is None or value[2] <= 0 else value for value in physical]
    frame["_kind"] = [None if period is None else period.kind for period in periods]
    candidates = frame.loc[frame.date.between(start, end) & frame["_period"].notna()]
    cases = []
    for (day, key, physical), rows in candidates.groupby(["date", "_key", "_period"], sort=True):
        aliases = sorted(set(rows.tenor.astype(str)))
        cases.append({"day": day, "key": key, "physical": physical, "tenor": aliases[0],
                      "actual": _weighted_actual(rows), "hidden_rows": len(rows),
                      "aliases": aliases})
    return frame, active, cases


def _metrics(frame: pd.DataFrame) -> dict:
    errors = frame.error.dropna().to_numpy(dtype=float)
    count = len(errors)
    scale = float(np.abs(errors).max()) if count else 0.0
    return {"n_cases": len(frame), "n_predicted": count, "n_missing": len(frame) - count,
            "coverage": count / len(frame) if len(frame) else math.nan,
            "mae": scale * float(np.mean(np.abs(errors / scale))) if scale else (0.0 if count else math.nan),
            "rmse": scale * float(np.sqrt(np.mean((errors / scale) ** 2))) if scale else (0.0 if count else math.nan),
            "bias": scale * float(np.mean(errors / scale)) if scale else (0.0 if count else math.nan)}


def _report(scores: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    records = []
    for key, rows in scores.groupby(columns, sort=True, dropna=False):
        if not isinstance(key, tuple):
            key = (key,)
        records.append({**dict(zip(columns, key)), "grid_parameters": rows.grid_parameters.iloc[0], **_metrics(rows)})
    return pd.DataFrame(records, columns=[*columns, "grid_parameters", *METRICS])


def run_backtest(observations, books, mappings, cfg, start: date, end: date,
                 grid: dict[str, list] | None = None, mask_fraction: float = 1.0,
                 seed: int = 42) -> dict[str, pd.DataFrame]:
    """Return detailed scores, per-unit summary and per-curve/kind metrics.

    Each candidate overrides the selected fields of each curve's effective
    production Model. Non-varied parameters retain their global or individual
    base values. No configuration is changed or selected automatically.

    ``mask_fraction`` deterministically samples physical-period cases once,
    shared by every candidate. It does not hide that fraction simultaneously.
    ``1.0`` examines every eligible observed period, one at a time. Coverage
    includes predictions that remain missing. Error metrics use predicted
    cases; candidates with different coverage need that qualification.
    """
    if not isinstance(start, date) or not isinstance(end, date) or start > end:
        raise ValueError("start and end must be dates with start <= end")
    if (isinstance(mask_fraction, bool) or not isinstance(mask_fraction, (int, float))
            or not math.isfinite(mask_fraction) or not 0 < mask_fraction <= 1):
        raise ValueError("mask_fraction must be a finite number greater than 0 and at most 1")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    candidates = configured_candidates(cfg, mappings, grid)
    frame, active, cases = _cases(observations, mappings, cfg, start, end)
    if not cases:
        raise ValueError("No finite observed physical periods in active mapped curves within the requested dates")
    if mask_fraction < 1:
        indexes = sorted(random.Random(seed).sample(range(len(cases)), max(1, math.ceil(len(cases) * mask_fraction))))
        cases = [cases[index] for index in indexes]
    models = {key: effective_model(cfg, mapping) for key, mapping in active.items()}

    records = []
    for index, candidate in enumerate(candidates, 1):
        candidate_id = f"candidate_{index:03d}"
        encoded_grid = json.dumps(candidate, sort_keys=True, allow_nan=False)
        for case in cases:
            mapping = active[case["key"]]
            model = model_from_overrides(models[mapping.key], candidate)
            targets = list(dict.fromkeys([*cfg.targets, case["tenor"]]))
            trial_cfg = replace(cfg, model=model, targets=targets, configuration_mode="global")
            current = frame.loc[frame["_key"].isin([case["key"]]) & frame.date.le(case["day"])].copy()
            hidden = current.date.eq(case["day"]) & current["_period"].isin([case["physical"]])
            current = current.loc[~hidden].drop(columns=["_key", "_period", "_kind"])
            output = fill_curves(current, books, [mapping], trial_cfg, [case["day"]])
            prediction = output.loc[output.tenor.eq(case["tenor"])]
            if len(prediction) != 1:
                raise ValueError(f"Engine must return one prediction for held-out {mapping.key} {case['day']} {case['tenor']}")
            row = prediction.iloc[0].to_dict()
            price = pd.to_numeric(pd.Series([row["price"]]), errors="coerce").iloc[0]
            error = float(price) - case["actual"] if pd.notna(price) and math.isfinite(price) else math.nan
            if pd.notna(error) and not math.isfinite(error):
                raise ValueError("Backtest error exceeds the finite numeric range")
            records.append({**row, "candidate_id": candidate_id, "grid_parameters": encoded_grid,
                            "parameters": json.dumps(asdict(model), sort_keys=True, allow_nan=False),
                            "actual": case["actual"], "error": error, "absolute_error": abs(error),
                            "squared_error": error * error, "hidden_rows": case["hidden_rows"],
                            "hidden_aliases": json.dumps(case["aliases"])})
    scores = pd.DataFrame(records)
    ordered = [name for name in SCORE_COLUMNS if name in scores]
    scores = scores[[*ordered, *[name for name in scores if name not in ordered]]]
    return {"scores": scores,
            "summary": _report(scores, ["candidate_id", "unit"]),
            "by_curve": _report(scores, ["candidate_id", *IDENTITY, "kind"])}
