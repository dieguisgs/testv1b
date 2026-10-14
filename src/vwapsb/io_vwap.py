"""Load own VWAPs using the power_row_..._future_spot input schema."""

from __future__ import annotations

import glob
import numpy as np
from pathlib import Path

import pandas as pd

from vwapsb.config import Config
from vwapsb.dates import parse_reference_dates
from vwapsb.identity import IDENTITY_COLUMNS, normalize_identity
from vwapsb.log import get_logger
from vwapsb.tenors import parse_tenor

OUT_COLS = ["date", *IDENTITY_COLUMNS, "tenor", "vwap", "volume"]


def _read_one(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".xls":
        raise ValueError(f"{path.name}: legacy .xls input is unsupported; save it as .xlsx or .csv first")
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        return pd.read_excel(path, dtype=object, keep_default_na=False, engine="openpyxl")
    if path.suffix.lower() != ".csv":
        raise ValueError(f"{path.name}: VWAP input must be .csv, .xlsx or .xlsm")
    # Preserve original identifiers, decimal formatting and text such as "NA".
    # Convert dates and numbers only in the normalized copy.
    return pd.read_csv(path, sep=None, engine="python", encoding="utf-8-sig",
                       dtype=str, keep_default_na=False)


def _to_number(s: pd.Series) -> pd.Series:
    if pd.api.types.is_object_dtype(s) or pd.api.types.is_string_dtype(s):
        s = s.astype(str).str.strip().str.replace(",", ".", regex=False)
    return pd.to_numeric(s, errors="coerce")


def load_vwaps(pattern: str | Path, cfg: Config) -> pd.DataFrame:
    return load_input(pattern, cfg)[0]


def load_input(pattern: str | Path, cfg: Config) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Read files or a glob such as data/vwaps_*.csv and return two tables.

    The normalized table contains date, product, region, unit, tenor,
    vwap and volume;
    the second table preserves the original input. Product meanings and
    EEX file locations come from the product mapping (mapping.py).
    """
    pat = str(cfg.resolve(pattern))
    paths = sorted(Path(p) for p in glob.glob(pat)) if any(c in pat for c in "*?[") else [Path(pat)]
    paths = [p for p in paths if p.exists()]
    if not paths:
        raise FileNotFoundError(f"No VWAP input files found at {pat}")

    col = cfg.vwap_columns
    need = ["reference_date", *IDENTITY_COLUMNS, "tenor", "vwap", "volume"]
    frames, originals = [], []
    log = get_logger()
    for p in paths:
        raw = _read_one(p)
        src = {k: col.get(k, k) for k in need}
        missing = [v for k, v in src.items() if v not in raw.columns and k != "volume"]
        if missing:
            raise ValueError(f"{p.name}: missing columns {missing}. Available: {list(raw.columns)}")
        dates = parse_reference_dates(raw[src["reference_date"]])
        if dates.isna().any():
            raise ValueError(f"{p.name}: {int(dates.isna().sum())} invalid or empty dates; review the input")
        identity = normalize_identity(raw, cfg)
        if identity["product"].eq("").any():
            raise ValueError(f"{p.name}: empty product identifiers")
        df = pd.DataFrame({
            "date": dates.dt.date,
            **{name: identity[name] for name in IDENTITY_COLUMNS},
            "tenor": raw[src["tenor"]].astype(str).str.strip(),
            "vwap": _to_number(raw[src["vwap"]]),
            "volume": _to_number(raw[src["volume"]]) if src["volume"] in raw.columns else float("nan"),
        })
        invalid = ~np.isfinite(df["vwap"])
        if invalid.any():
            log.warning("%s: %d empty/nonfinite VWAPs; preserved in enriched output and excluded from anchors",
                        p.name, int(invalid.sum()))
        df.loc[~np.isfinite(df["volume"]) | (df["volume"] < 0), "volume"] = float("nan")
        unknown = df["tenor"].map(parse_tenor).isna()
        if unknown.any():
            log.warning("%s: %d unrecognized tenors; original rows preserved: %s",
                        p.name, int(unknown.sum()), sorted(df.loc[unknown, "tenor"].unique())[:10])
        log.info("Input %s | rows=%d | readable VWAPs=%d | dates=%s..%s", p,
                 len(raw), int((~invalid).sum()), df["date"].min(), df["date"].max())
        originals.append(raw)
        df = df.loc[~invalid]
        frames.append(df)
    out = pd.concat(frames, ignore_index=True)
    out = out.dropna(subset=["vwap", "date"])
    duplicates = out.duplicated(["date", *IDENTITY_COLUMNS, "tenor"], keep=False)
    if duplicates.any():
        log.warning("Input: %d rows with duplicate keys; preserved in enriched output and aggregated only as anchors",
                    int(duplicates.sum()))
    return out[OUT_COLS].reset_index(drop=True), pd.concat(originals, ignore_index=True)
