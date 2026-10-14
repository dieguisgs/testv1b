"""Exact curve identities shared by input, mapping, engine and output.

Blank values are literal identity components, never wildcards. Missing
columns become blanks only for internal/legacy tables; file loaders validate
the required schema before calling these helpers.
"""

from __future__ import annotations

import pandas as pd

IDENTITY_COLUMNS = ["product", "region", "unit"]
CurveKey = tuple[str, str, str]


def identity_text(value) -> str:
    """Normalize surrounding whitespace without changing case or spelling."""
    return "" if pd.isna(value) else str(value).strip()


def normalize_identity(frame: pd.DataFrame, cfg=None) -> pd.DataFrame:
    """Return normalized product/region/unit columns with the original index."""
    aliases = cfg.vwap_columns if cfg is not None else {}
    return pd.DataFrame({
        name: frame[aliases.get(name, name)].map(identity_text)
        if aliases.get(name, name) in frame else pd.Series("", index=frame.index)
        for name in IDENTITY_COLUMNS
    }, index=frame.index)


def curve_keys(frame: pd.DataFrame, cfg=None) -> pd.Series:
    """Return collision-free tuple keys, including blank region/unit values."""
    identity = normalize_identity(frame, cfg)
    return pd.Series(list(identity.itertuples(index=False, name=None)),
                     index=frame.index, dtype=object)
