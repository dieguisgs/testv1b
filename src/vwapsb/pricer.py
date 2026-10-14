"""Price any delivery period from a set of quotes.

Three methods, in priority order:
  exact     A quote exists for that exact period.
  strip     Cover the period with contiguous quotes (Q = 3 months,
            Win = Q4 + Q1, Cal = 4 Q, BOM = remaining days...), weighted
            by delivery hours and using the fewest possible components.
  residual  The period is the tail of a larger quote (BOM within M+0,
            BOW within W+0): (P_total*H_total - P_head*H_head) / H_tail,
            with the head (already delivered) covered by other quotes.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import date, timedelta

from vwapsb.hours import DeliveryHours
from vwapsb.tenors import Key

HoursFn = Callable[[date, date], float]


class Pricer:
    def __init__(self, prices: dict[Key, float], hours: HoursFn):
        self.prices = prices
        self.hours = hours
        self._from: dict[date, list[tuple[date, float]]] = defaultdict(list)
        self._by_end: dict[date, list[tuple[date, float]]] = defaultdict(list)
        for (s, e), p in prices.items():
            self._from[s].append((e, p))
            self._by_end[e].append((s, p))
        for lst in self._from.values():
            lst.sort(key=lambda t: t[0], reverse=True)  # Longest components first.

    def price(self, start: date, end: date, kind: str | None = None) -> tuple[float, str] | None:
        hours = self.hours.for_kind(kind) if isinstance(self.hours, DeliveryHours) else self.hours
        if hours(start, end) <= 0:
            return None
        p = self.prices.get((start, end))
        if p is not None:
            return p, "exact"
        strip = self._strip_price(start, end, hours)
        if strip is not None:
            return strip, "strip"
        weekend_parents_only = (isinstance(self.hours, DeliveryHours)
                                and self.hours.profile == "Peak" and kind in ("Day", "Weekend"))
        res = self._residual_price(start, end, hours, weekend_parents_only)
        if res is not None:
            return res, "residual"
        return None

    def _strip(self, s: date, e: date, hours: HoursFn | None = None) -> list[tuple[Key, float | None]] | None:
        """Find the path from s to e with the fewest components. Skip days
        without delivery hours (Peak weekends) without requiring a quote."""
        if s >= e:
            return None
        hours = hours if hours is not None else self.hours
        prev: dict[date, tuple[date, Key, float | None]] = {}
        seen = {s}
        frontier = [s]
        while frontier:
            nxt = []
            for x in frontier:
                cands = [(b, p) for b, p in self._from.get(x, ()) if b <= e]
                gap = x + timedelta(days=1)
                if gap <= e and hours(x, gap) == 0:
                    cands.append((gap, None))
                for b, p in cands:
                    if b in seen:
                        continue
                    seen.add(b)
                    prev[b] = (x, (x, b), p)
                    if b == e:
                        path = []
                        cur = e
                        while cur != s:
                            a, k, pp = prev[cur]
                            path.append((k, pp))
                            cur = a
                        return path[::-1]
                    nxt.append(b)
            frontier = nxt
        return None

    def _weighted(self, path: list[tuple[Key, float | None]], hours: HoursFn | None = None) -> tuple[float, float] | None:
        hours = hours if hours is not None else self.hours
        num = den = 0.0
        for (a, b), p in path:
            if p is None:
                continue
            h = hours(a, b)
            num += p * h
            den += h
        return (num / den, den) if den > 0 else None

    def _strip_price(self, s: date, e: date, hours: HoursFn | None = None) -> float | None:
        path = self._strip(s, e, hours)
        if path is None or len(path) < 2:
            return None
        w = self._weighted(path, hours)
        return w[0] if w else None

    def _residual_price(self, s: date, e: date, hours: HoursFn | None = None,
                        weekend_parents_only: bool = False) -> float | None:
        hours = hours if hours is not None else self.hours
        for s0, p0 in sorted(self._by_end.get(e, ()), key=lambda t: t[0], reverse=True):
            if s0 >= s:
                continue
            # Peak Day/Weekend includes weekends; Peak Week/Month does not.
            # Without the parent's type in the key, only a Saturday-Monday
            # Weekend has an unambiguously compatible residual calendar.
            if weekend_parents_only and not (s0.weekday() == 5 and (e - s0).days == 2):
                continue
            head = self._strip(s0, s, hours)
            if head is None:
                continue
            w = self._weighted(head, hours)
            if w is None:
                # A fully covered head containing only non-delivery days has
                # zero energy. The tail retains the entire parent's value.
                # Missing positive-hour evidence must still remain unpriced.
                if hours(s0, s) != 0:
                    continue
                w = (0.0, 0.0)
            ph, hh = w
            h_total = hours(s0, e)
            h_tail = h_total - hh
            if h_tail <= 0:
                continue
            return (p0 * h_total - ph * hh) / h_tail
        return None
