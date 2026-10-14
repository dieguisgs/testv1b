"""A causal, additive refill with one selected method for each curve point.

The hierarchy is original, same-kind local median, historical median,
fixed-contract EEX average, then missing. Estimates never become observations.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
import json
import math
from zoneinfo import ZoneInfo

import pandas as pd

from vwapsb.config import Config, Model, configuration_id, effective_model
from vwapsb.hours import hours_fn
from vwapsb.identity import CurveKey, curve_keys
from vwapsb.io_eex import EexBook
from vwapsb.mapping import ProductMap
from vwapsb.pricer import Pricer
from vwapsb.tenors import Period, resolve_tenor


OUTPUT_COLUMNS = [
    "date", "product", "region", "unit", "tenor", "kind", "delivery_start", "delivery_end",
    "price", "status", "method", "eex_price", "eex_date", "eex_cutoff", "eex_age_days",
    "adjustment", "local_count", "history_days", "history_start", "history_end",
    "reason", "details", "configuration_id",
]
_KIND_ORDER = {kind: index for index, kind in enumerate(
    ("Day", "Weekend", "Week", "Month", "Quarter", "Season", "Year", "BOW", "BOM"))}
DeliveryKey = tuple[date, date, float]


def _date(value) -> date:
    if pd.isna(value):
        raise ValueError("Expected a nonmissing normalized date")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise ValueError(f"Expected a normalized date, received {value!r}")


def _finite(value: float, label: str) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    return value


def _number(value) -> float | None:
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _weighted_mean(values: list[float], weights: list[float]) -> float:
    """Average finite prices without overflowing their weighted sum."""
    scale = max(abs(value) for value in values)
    if not scale:
        return 0.0
    weight_scale = max(weights)
    normalized = [weight / weight_scale for weight in weights]
    total = math.fsum(normalized)
    mean = math.fsum((value / scale) * (weight / total) for value, weight in zip(values, normalized))
    return _finite(scale * max(-1.0, min(1.0, mean)), "Weighted price")


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else _weighted_mean(ordered[middle - 1:middle + 1], [1.0, 1.0])


def contract_position(day: date, kind: str) -> int:
    """Index a date in the calendar steps used by local/history matching.

    Months/BOM use calendar months, quarters calendar quarters, years calendar
    years, seasons April-September/October-March, weeks Monday-based weeks,
    and days calendar days. These are contract buckets, not elapsed-day ratios.
    """
    month = day.year * 12 + day.month - 1
    if kind in ("Month", "BOM"):
        return month
    if kind == "Quarter":
        return month // 3
    if kind == "Year":
        return day.year
    if kind == "Season":
        return (month - 3) // 6
    if kind in ("Week", "Weekend", "BOW"):
        return (day.toordinal() - 1) // 7
    if kind == "Day":
        return day.toordinal()
    raise ValueError(f"Unsupported delivery kind: {kind!r}")


def lead_steps(period: Period, reference: date) -> int:
    """Delivery-start bucket minus the observation date's calendar bucket."""
    return contract_position(period.start, period.kind) - contract_position(reference, period.kind)


@dataclass(frozen=True)
class OwnQuote:
    period: Period
    kinds: tuple[str, ...]
    labels: tuple[str, ...]
    price: float
    volume: float | None
    observations: tuple[dict, ...]


def _hours(mapping: ProductMap):
    if mapping.hours not in ("Base", "Peak", "Peak7"):
        raise ValueError(f"Unknown delivery profile: {mapping.hours!r}")
    ZoneInfo(mapping.timezone)
    return hours_fn(mapping.hours, mapping.timezone)


def _delivery_key(period: Period, hours) -> DeliveryKey:
    """Keep aliases separate when the same dates represent different energy."""
    return period.start, period.end, hours(period.start, period.end, kind=period.kind)


def _own_quotes(frame: pd.DataFrame | None, day: date, cfg: Config, hours) -> dict[DeliveryKey, OwnQuote]:
    if frame is None:
        return {}
    groups: dict[DeliveryKey, list[tuple[Period, str, float, float | None]]] = {}
    for label, price, volume in frame[["tenor", "vwap", "volume"]].itertuples(index=False, name=None):
        label = str(label).strip()
        period = resolve_tenor(label, day, cfg.day_convention, cfg.weekend_offset)
        price, volume = _number(price), _number(volume)
        if period is None or price is None or hours(period.start, period.end, kind=period.kind) <= 0:
            continue
        volume = volume if volume is not None and volume >= 0 else None
        groups.setdefault(_delivery_key(period, hours), []).append((period, label, price, volume))
    output = {}
    for key, values in groups.items():
        values.sort(key=lambda item: (item[1], item[2], -1.0 if item[3] is None else item[3]))
        positive = [item for item in values if item[3] is not None and item[3] > 0]
        used = positive or values
        weights = [item[3] for item in used] if positive else [1.0] * len(used)
        price = _weighted_mean([item[2] for item in used], weights)
        known_volumes = [item[3] for item in values if item[3] is not None]
        try:
            volume = _finite(math.fsum(known_volumes), "Aggregated volume") if known_volumes else None
        except OverflowError as exc:
            raise ValueError("Aggregated own volume exceeds the finite numeric range") from exc
        period = min((item[0] for item in values), key=lambda item: _KIND_ORDER[item.kind])
        output[key] = OwnQuote(period, tuple(sorted({item[0].kind for item in values})),
                              tuple(sorted({item[1] for item in values})), price, volume,
                              tuple({"tenor": item[1], "price": item[2], "volume": item[3]} for item in values))
    return output


def _volume_allowed(quote: OwnQuote, model: Model) -> bool:
    return model.min_volume == 0 if quote.volume is None else quote.volume >= model.min_volume


def _quote_details(quote: OwnQuote) -> dict:
    return {"tenors": list(quote.labels), "kind": quote.period.kind,
            "delivery_start": quote.period.start.isoformat(), "delivery_end": quote.period.end.isoformat(),
            "own_price": quote.price, "volume": quote.volume}


def _as_kind(quote: OwnQuote, kind: str) -> OwnQuote | None:
    """Retain a physical observation once when one of its aliases has this kind."""
    if kind not in quote.kinds:
        return None
    return replace(quote, period=Period(kind, quote.period.start, quote.period.end))


class _Curve:
    def __init__(self, cfg: Config, mapping: ProductMap, book: EexBook | None, frame: pd.DataFrame):
        self.cfg, self.mapping, self.book = cfg, mapping, book
        self.model = effective_model(cfg, mapping)
        self.id = configuration_id(cfg, mapping)
        self.settings = {
            "model": asdict(self.model), "targets": list(cfg.targets),
            "configuration_mode": cfg.configuration_mode, "eex_offset_days": cfg.eex_offset_days,
            "max_stale_days": cfg.max_stale_days, "day_convention": cfg.day_convention,
            "weekend_offset": cfg.weekend_offset,
            "mapping": {name: getattr(mapping, name) for name in (
                "product", "region", "unit", "area", "profile", "eex_file", "hours", "timezone")},
        }
        self.hours = _hours(mapping)
        self.own_days = {day: _own_quotes(group, day, cfg, self.hours)
                         for day, group in frame.groupby("date", sort=False)}
        self.pricers: dict[date, Pricer] = {}
        self.history: dict[date, list[tuple[OwnQuote, float, str]]] = {}

    def _snapshot(self, publication: date) -> Pricer:
        if publication not in self.pricers:
            quotes, actual = self.book.available_quotes(publication, 0, 0)
            if actual != publication:
                raise ValueError(f"Expected an exact EEX publication on {publication}")
            self.pricers[publication] = Pricer(quotes, self.hours)
        return self.pricers[publication]

    @staticmethod
    def _reference(pricer: Pricer | None, period: Period) -> tuple[float, str] | None:
        if pricer is None:
            return None
        quote = pricer.price(period.start, period.end, kind=period.kind)
        return (_finite(quote[0], "Constructed EEX reference"), quote[1]) if quote is not None else None

    def _historical_pairs(self, day: date) -> list[tuple[OwnQuote, float, str]]:
        if day not in self.history:
            self.history[day] = []
            if self.book is not None and self.book.available_asof(day, 0, 0) == day:
                for quote in self.own_days[day].values():
                    if not _volume_allowed(quote, self.model):
                        continue
                    reference = self._reference(self._snapshot(day), quote.period)
                    if reference is not None:
                        self.history[day].append((quote, reference[0], reference[1]))
        return self.history[day]

    def rows(self, day: date) -> list[dict]:
        cutoff = EexBook.cutoff_date(day, self.cfg.eex_offset_days)
        limit = None if self.cfg.max_stale_days == 0 else self.cfg.max_stale_days
        asof = self.book.available_asof(day, limit, self.cfg.eex_offset_days) if self.book is not None else None
        pricer = self._snapshot(asof) if asof is not None else None
        own = self.own_days.get(day, {})
        labels = list(self.cfg.targets) + [label for quote in own.values() for label in quote.labels]
        targets = [(label, period) for label in dict.fromkeys(labels)
                   if (period := resolve_tenor(label, day, self.cfg.day_convention, self.cfg.weekend_offset)) is not None]
        representatives = {}
        for label, period in targets:
            key = _delivery_key(period, self.hours)
            previous = representatives.get(key)
            if previous is None or (_KIND_ORDER[period.kind], label) < (_KIND_ORDER[previous[1].kind], previous[0]):
                representatives[key] = (label, period)
        calculated = {}
        rows = []
        for label, period in targets:
            key = _delivery_key(period, self.hours)
            # Original labels keep their own original/own_reused provenance.
            if key in own:
                rows.append(self._fill(day, cutoff, asof, pricer, own, label, period))
                continue
            representative_label, representative_period = representatives[key]
            if key not in calculated:
                calculated[key] = self._fill(day, cutoff, asof, pricer, own,
                                             representative_label, representative_period)
            row = dict(calculated[key], tenor=label, kind=period.kind)
            if label != representative_label:
                details = json.loads(row["details"])
                details["equivalent_target"] = {"tenor": representative_label,
                                                "kind": representative_period.kind,
                                                "rule": "same_delivery_interval_and_hours"}
                row["details"] = json.dumps(details, sort_keys=True, separators=(",", ":"), allow_nan=False)
            rows.append(row)
        return rows

    def _fill(self, day: date, cutoff: date, asof: date | None, pricer: Pricer | None,
              own: dict[DeliveryKey, OwnQuote], label: str, period: Period) -> dict:
        reference = self._reference(pricer, period)
        row = dict(date=day, product=self.mapping.product, region=self.mapping.region, unit=self.mapping.unit,
                   tenor=label, kind=period.kind, delivery_start=period.start, delivery_end=period.end,
                   price=None, status="missing", method="missing", eex_price=reference[0] if reference else None,
                   eex_date=asof, eex_cutoff=cutoff, eex_age_days=(day - asof).days if asof else None,
                   adjustment=None, local_count=0, history_days=0, history_start=None, history_end=None,
                   reason="", details="", configuration_id=self.id)
        details = {"settings": self.settings, "eex_reference_method": reference[1] if reference else None, "stages": []}

        def finish(method: str, price: float | None, reason: str) -> dict:
            row.update(method=method, price=price, reason=reason,
                       status="original" if method in ("original", "own_reused") else
                              "estimated" if price is not None else "missing")
            row["details"] = json.dumps(details, sort_keys=True, separators=(",", ":"), allow_nan=False)
            return row

        if self.hours(period.start, period.end, kind=period.kind) <= 0:
            details["stages"].append({"stage": "original", "outcome": "zero_delivery_hours"})
            return finish("missing", None, "zero_delivery_hours")
        original = own.get(_delivery_key(period, self.hours))
        if original is not None:
            details["own"] = {**_quote_details(original), "observations": list(original.observations),
                              "aggregation": "positive_volume_weighted_mean_else_simple_mean"}
            reused = label not in original.labels
            details["stages"].append({"stage": "original", "outcome": "equivalent_alias" if reused else "selected"})
            return finish("own_reused" if reused else "original", original.price,
                          "equivalent_original_delivery_period" if reused else "observed_original_period")
        details["stages"].append({"stage": "original", "outcome": "unavailable"})

        local = []
        rejected = []
        if self.model.local_enabled and reference is not None:
            for quote in own.values():
                matched = _as_kind(quote, period.kind)
                quote = matched or quote
                distance = abs(contract_position(quote.period.start, period.kind)
                               - contract_position(period.start, period.kind))
                reason = ("different_kind" if matched is None else
                          "outside_radius" if distance > self.model.local_max_steps else
                          "volume_below_minimum_or_unknown" if not _volume_allowed(quote, self.model) else None)
                anchor_reference = self._reference(pricer, quote.period) if reason is None else None
                if reason is None and anchor_reference is None:
                    reason = "missing_eex_reference"
                if reason is not None:
                    rejected.append({**_quote_details(quote), "reason": reason})
                else:
                    local.append({**_quote_details(quote), "distance_steps": distance,
                                  "eex_price": anchor_reference[0], "eex_method": anchor_reference[1],
                                  "adjustment": _finite(quote.price - anchor_reference[0], "Local additive difference")})
        row["local_count"] = len(local)
        local_outcome = ("disabled" if not self.model.local_enabled else "target_eex_unavailable" if reference is None
                         else "selected" if len(local) >= self.model.local_min_anchors else "insufficient_anchors")
        details["stages"].append({"stage": "local", "outcome": local_outcome,
                                  "required_anchors": self.model.local_min_anchors,
                                  "anchors": local, "rejected": rejected})
        if local_outcome == "selected":
            adjustment = _median([item["adjustment"] for item in local])
            row["adjustment"] = adjustment
            return finish("local", _finite(reference[0] + adjustment, "Local filled price"), "same_kind_local_median")

        history = []
        window_start = day - timedelta(days=self.model.history_window_days)
        target_lead = lead_steps(period, day)
        if self.model.history_enabled and reference is not None:
            for historical_day in sorted(self.own_days):
                if not (window_start <= historical_day < day and historical_day <= cutoff):
                    continue
                eligible = []
                for quote, historical_eex, eex_method in self._historical_pairs(historical_day):
                    quote = _as_kind(quote, period.kind)
                    if quote is None:
                        continue
                    past_lead = lead_steps(quote.period, historical_day)
                    if abs(past_lead - target_lead) > self.model.local_max_steps:
                        continue
                    eligible.append({**_quote_details(quote), "lead_steps": past_lead,
                                     "eex_price": historical_eex, "eex_method": eex_method,
                                     "adjustment": _finite(quote.price - historical_eex, "Historical additive difference")})
                if eligible:
                    history.append({"date": historical_day.isoformat(), "anchors": eligible,
                                    "daily_median": _median([item["adjustment"] for item in eligible])})
        row["history_days"] = len(history)
        if history:
            row.update(history_start=date.fromisoformat(history[0]["date"]), history_end=date.fromisoformat(history[-1]["date"]))
        history_outcome = ("disabled" if not self.model.history_enabled else "target_eex_unavailable" if reference is None
                           else "selected" if len(history) >= self.model.history_min_days else "insufficient_days")
        details["stages"].append({"stage": "history", "outcome": history_outcome, "days": history,
                                  "window_start": window_start.isoformat(), "required_days": self.model.history_min_days,
                                  "target_lead_steps": target_lead, "radius_steps": self.model.local_max_steps,
                                  "matching": "same_kind_calendar_lead_distance"})
        if history_outcome == "selected":
            adjustment = _median([item["daily_median"] for item in history])
            row["adjustment"] = adjustment
            return finish("history", _finite(reference[0] + adjustment, "Historical filled price"),
                          "historical_median_of_daily_medians")

        averages = []
        unavailable = []
        publications = []
        if self.book is not None and asof is not None:
            end = bisect_right(self.book.trade_dates, asof)
            publications = self.book.trade_dates[max(0, end - self.model.eex_window):end]
            for publication in publications:
                quote = self._reference(self._snapshot(publication), period)
                if quote is None:
                    unavailable.append(publication.isoformat())
                else:
                    averages.append({"date": publication.isoformat(), "price": quote[0], "eex_method": quote[1]})
        complete = len(publications) == self.model.eex_window and not unavailable
        average_outcome = ("no_eex_book" if self.book is None else "no_publication_within_cutoff_and_age" if asof is None
                           else "insufficient_publications" if len(publications) < self.model.eex_window
                           else "missing_contract_in_window" if unavailable else "selected")
        if complete:
            raw_weights = ([1.0] * len(averages) if self.model.eex_average == "simple" else
                           [2.0 ** (-(len(averages) - 1 - index) / self.model.eex_halflife)
                            for index in range(len(averages))])
            total = math.fsum(raw_weights)
            for item, weight in zip(averages, raw_weights):
                item["weight"] = weight / total
            if sum(item["weight"] > 0 for item in averages) < 2:
                complete = False
                average_outcome = "degenerate_averaging_weights"
        details["stages"].append({"stage": "eex_average", "outcome": average_outcome,
                                  "required_publications": self.model.eex_window, "average": self.model.eex_average,
                                  "halflife_observations": self.model.eex_halflife,
                                  "observations": averages, "missing_contract_dates": unavailable,
                                  "delivery_start": period.start.isoformat(), "delivery_end": period.end.isoformat()})
        if complete:
            return finish(f"eex_{self.model.eex_average}",
                          _weighted_mean([item["price"] for item in averages], [item["weight"] for item in averages]),
                          "complete_fixed_contract_eex_window")
        return finish("missing", None, average_outcome)


def fill_curves(observations: pd.DataFrame, books: dict[CurveKey, EexBook | None],
                mappings: list[ProductMap], cfg: Config, dates: list[date]) -> pd.DataFrame:
    """Refill requested dates without changing observations, books or mappings.

    Historical pairs require exact same-date EEX and an own date strictly
    earlier than the target date and no later than its EEX cutoff. Every own
    date has equal weight through a median of daily medians. Historical lead
    matching uses :func:`lead_steps`, not an absolute historical contract.
    """
    required = {"date", "product", "region", "unit", "tenor", "vwap", "volume"}
    if required - set(observations):
        raise ValueError(f"Normalized observations lack columns: {sorted(required - set(observations))}")
    requested = sorted({_date(day) for day in dates})
    if not requested:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    active = [mapping for mapping in mappings if mapping.active]
    if len({mapping.key for mapping in active}) != len(active):
        raise ValueError("Active mappings contain duplicate curve identities")
    frame = observations.copy()
    frame["date"] = frame["date"].map(_date)
    # Do not inspect/aggregate future originals: they cannot affect this run.
    frame = frame.loc[frame.date <= requested[-1]]
    grouped = {key: group for key, group in frame.groupby(curve_keys(frame), sort=False)}
    curves = [_Curve(cfg, mapping, books.get(mapping.key), grouped.get(mapping.key, frame.iloc[:0])) for mapping in active]
    rows = [row for day in requested for curve in curves for row in curve.rows(day)]
    output = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    for name in ("price", "eex_price", "eex_age_days", "adjustment"):
        output[name] = pd.to_numeric(output[name], errors="raise").astype(float)
    return output
