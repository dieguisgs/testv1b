"""Parse reference dates without applying day-first rules to ISO dates."""

from __future__ import annotations

from numbers import Number

import pandas as pd


def parse_reference_dates(values: pd.Series) -> pd.Series:
    """Accept ISO YYYY-MM-DD, day-first text and spreadsheet date objects.

    ISO dates use an explicit parser: applying dayfirst=True to mixed ISO
    text can silently exchange the month and day on some pandas versions.
    Numeric dates are ambiguous (Excel serials versus Unix timestamps) and
    remain NaT, as do timezone-bearing values. The input contract is a local
    calendar date, not an instant. Callers report invalid rows in context.
    """
    parsed = pd.Series(pd.NaT, index=values.index, dtype="datetime64[ns]")
    if values.empty:
        return parsed
    text = values.astype(str).str.strip()
    numeric = values.map(lambda value: isinstance(value, Number)) | text.str.fullmatch(r"[+-]?\d+(?:\.\d+)?")
    zoned = text.str.contains(r"(?:Z|[+-]\d{2}:?\d{2})$", case=False, regex=True)
    valid_type = ~numeric & ~zoned
    iso = valid_type & text.str.match(r"^\d{4}-\d{2}-\d{2}(?:$|[ T])")
    if iso.any():
        parsed.loc[iso] = pd.to_datetime(text.loc[iso], format="ISO8601", errors="coerce")
    other = valid_type & ~iso
    if other.any():
        parsed.loc[other] = pd.to_datetime(values.loc[other], dayfirst=True,
                                         format="mixed", errors="coerce")
    return parsed
