"""Delivery hours for a period, used to weight strips and arbitrage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import lru_cache, partial
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import pandas as pd


@lru_cache(maxsize=500_000)
def period_hours(start: date, end: date, profile: str, tz: str, kind: str | None = None) -> float:
    """Base: actual local delivery hours (23/25 on daylight saving changes).
    Peak Day/Weekend: 12 h per delivery day, including Saturdays and Sundays.
    Other Peak contracts (or omitted kind): 12 h per Monday-Friday day.
    Peak7: 12 h every day (e.g. ES PeakMo-Su).

    When decomposing a contract, kind belongs to the target contract:
    weekend Day contracts are not part of a Monday-Friday Peak Week/Month.
    """
    if profile not in ("Base", "Peak", "Peak7"):
        raise ValueError("Delivery hours profile must be Base, Peak or Peak7")
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError(f"Unknown delivery timezone: {tz!r}") from exc
    if end <= start:
        return 0.0
    if profile == "Peak":
        if kind in ("Day", "Weekend"):
            return 12.0 * (end - start).days
        return 12.0 * int(np.busday_count(start, end))
    if profile == "Peak7":
        return 12.0 * (end - start).days
    a = pd.Timestamp(start).tz_localize(tz)
    b = pd.Timestamp(end).tz_localize(tz)
    return (b - a).total_seconds() / 3600.0


@dataclass(frozen=True)
class DeliveryHours:
    profile: str
    tz: str

    def __call__(self, start: date, end: date, kind: str | None = None) -> float:
        return period_hours(start, end, self.profile, self.tz, kind)

    def for_kind(self, kind: str | None):
        return partial(self, kind=kind)


def hours_fn(profile: str, tz: str) -> DeliveryHours:
    # Validate even if no price/period later exercises this calendar.
    period_hours(date(2026, 1, 1), date(2026, 1, 1), profile, tz)
    return DeliveryHours(profile, tz)
