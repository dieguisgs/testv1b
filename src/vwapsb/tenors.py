"""Relative tenors (D+1, WE, BOM, Q+3, Win+2, Cal+1...) -> absolute delivery periods.

Match own VWAPs to EEX by their delivery period [start, end), rather than
their label, because labeling conventions can differ between sources.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

Key = tuple[date, date]

KIND_GROUP = {
    "Day": "short", "Weekend": "short", "BOW": "short", "Week": "short",
    "BOM": "month", "Month": "month",
    "Quarter": "quarter",
    "Season": "long", "Year": "long",
}


@dataclass(frozen=True)
class Period:
    kind: str
    start: date
    end: date  # Exclusive.

    @property
    def key(self) -> Key:
        return (self.start, self.end)

    @property
    def group(self) -> str:
        return KIND_GROUP[self.kind]

    @property
    def name(self) -> str:
        s, last = self.start, self.end - timedelta(days=1)
        match self.kind:
            case "Day":
                return s.isoformat()
            case "Weekend":
                y, w, _ = s.isocalendar()
                return f"WE {y}-W{w:02d}"
            case "Week":
                y, w, _ = s.isocalendar()
                return f"{y}-W{w:02d}"
            case "Month":
                return f"{s:%Y-%m}"
            case "Quarter":
                return f"{s.year}-Q{(s.month - 1) // 3 + 1}"
            case "Season":
                if s.month == 4:
                    return f"Sum-{s.year % 100:02d}"
                return f"Win-{s.year % 100:02d}/{(s.year + 1) % 100:02d}"
            case "Year":
                return f"Cal-{s.year}"
            case _:  # BOW, BOM
                return f"{self.kind} {s.isoformat()}..{last.isoformat()}"


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


_RX = re.compile(
    r"^\s*(BOW|BOM|WE|Summer|Winter|Sum|Win|Cal|Y|D|W|M|Q)\s*(?:\+\s*(\d+))?\s*$",
    re.IGNORECASE,
)
_CANON = {
    "bow": "BOW", "bom": "BOM", "we": "WE", "summer": "Sum", "winter": "Win",
    "sum": "Sum", "win": "Win", "cal": "Cal", "y": "Cal", "d": "D", "w": "W", "m": "M", "q": "Q",
}


def parse_tenor(label: str) -> tuple[str, int] | None:
    m = _RX.match(str(label))
    if not m:
        return None
    return _CANON[m.group(1).lower()], int(m.group(2) or 0)


def _add_business_days(d: date, n: int) -> date:
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def resolve_tenor(
    label: str, ref: date, day_convention: str = "calendar", weekend_offset: int = 0
) -> Period | None:
    """Delivery period of `label` traded on `ref`. Return None for an unknown
    label or an empty BOM/BOW with no remaining delivery period."""
    if day_convention not in ("calendar", "business"):
        raise ValueError("day_convention must be calendar or business")
    if isinstance(weekend_offset, bool) or not isinstance(weekend_offset, int):
        raise ValueError("weekend_offset must be an integer")
    parsed = parse_tenor(label)
    if parsed is None:
        return None
    base, n = parsed
    match base:
        case "D":
            d = ref + timedelta(days=n) if day_convention == "calendar" else _add_business_days(ref, n)
            return Period("Day", d, d + timedelta(days=1))
        case "WE":
            sat = ref + timedelta(days=(5 - ref.weekday()) % 7 or 7)  # Next Saturday, strictly after ref.
            sat += timedelta(weeks=n + weekend_offset)
            return Period("Weekend", sat, sat + timedelta(days=2))
        case "BOW":
            nxt_monday = ref + timedelta(days=7 - ref.weekday())
            s = ref + timedelta(days=1)
            return Period("BOW", s, nxt_monday) if s < nxt_monday else None
        case "W":
            mon = ref - timedelta(days=ref.weekday()) + timedelta(weeks=n)
            return Period("Week", mon, mon + timedelta(days=7))
        case "BOM":
            s, e = ref + timedelta(days=1), add_months(ref.replace(day=1), 1)
            return Period("BOM", s, e) if s < e else None
        case "M":
            s = add_months(ref.replace(day=1), n)
            return Period("Month", s, add_months(s, 1))
        case "Q":
            q0 = date(ref.year, 3 * ((ref.month - 1) // 3) + 1, 1)
            s = add_months(q0, 3 * n)
            return Period("Quarter", s, add_months(s, 3))
        case "Sum" | "Win":
            if n < 1:
                return None
            start_month = 4 if base == "Sum" else 10
            y = ref.year if ref.month < start_month else ref.year + 1  # First season that has not started.
            s = date(y + n - 1, start_month, 1)
            return Period("Season", s, add_months(s, 6))
        case "Cal":
            s = date(ref.year + n, 1, 1)
            return Period("Year", s, date(s.year + 1, 1, 1))
    return None


_EEX_LEN = {"Day": ("d", 1), "Weekend": ("d", 2), "Week": ("d", 7),
            "Month": ("m", 1), "Quarter": ("m", 3), "Season": ("m", 6), "Year": ("m", 12)}


def period_from_eex(maturity_type: str, delivery_start: date) -> Period | None:
    spec = _EEX_LEN.get(maturity_type)
    if spec is None:
        return None
    unit, n = spec
    end = delivery_start + timedelta(days=n) if unit == "d" else add_months(delivery_start, n)
    return Period(maturity_type, delivery_start, end)
