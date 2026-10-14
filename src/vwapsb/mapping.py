"""Exact product-to-EEX assignments and optional fixed model overrides."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any

import pandas as pd

from vwapsb.config import BOOL_FIELDS, INTEGER_FIELDS, MODEL_FIELDS, Config, effective_model, validate_timezone
from vwapsb.identity import CurveKey, IDENTITY_COLUMNS
from vwapsb.log import get_logger

COLUMNS = [*IDENTITY_COLUMNS, "use", "area", "profile", "eex_file", "hours", "timezone", "comment"]
PARAMETER_COLUMNS = list(MODEL_FIELDS)
USES = ("fill", "off")
HOURS = ("Base", "Peak", "Peak7")


@dataclass
class ProductMap:
    product: str
    use: str
    area: str
    profile: str
    eex_file: str
    hours: str
    timezone: str
    comment: str = ""
    region: str = ""
    unit: str = ""
    parameter_overrides: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> CurveKey:
        return self.product, self.region, self.unit

    @property
    def active(self) -> bool:
        return self.use == "fill" and bool(self.eex_file.strip())

    @property
    def label(self) -> str:
        return f"{self.product} [region={self.region!r}, unit={self.unit!r}]"


def read_mapping_table(path: str | Path) -> pd.DataFrame:
    """Read CSV or the first XLSX sheet, preserving blank and literal identifiers."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Product mapping does not exist: {path}. Create it with: python run.py mapping")
    if path.suffix.lower() == ".csv":
        frame = pd.read_csv(path, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    elif path.suffix.lower() == ".xlsx":
        frame = pd.read_excel(path, sheet_name=0, dtype=str, keep_default_na=False, engine="openpyxl")
    else:
        raise ValueError("Mapping must be .csv or .xlsx; convert legacy Excel files to .xlsx first")
    frame.columns = [str(name).strip() for name in frame.columns]
    if frame.columns.duplicated().any():
        raise ValueError(f"{path.name}: duplicate mapping headers after trimming")
    return frame


def _parameter_cells(row: pd.Series) -> dict[str, Any]:
    values = {}
    for name in MODEL_FIELDS:
        text = str(row.get(name, "")).strip()
        if not text:
            continue
        try:
            if name in BOOL_FIELDS:
                if text.lower() not in ("true", "false"):
                    raise ValueError("use true or false")
                value = text.lower() == "true"
            elif name in INTEGER_FIELDS:
                if re.fullmatch(r"[+-]?\d+", text) is None:
                    raise ValueError("use a whole integer, for example 5")
                value = int(text)
            elif name == "eex_average":
                value = text.lower()
            else:
                value = float(text)
        except (ValueError, OverflowError) as exc:
            raise ValueError(f"Invalid {name} cell {text!r}: {exc}") from exc
        values[name] = value
    return values


def load_mapping(cfg: Config) -> list[ProductMap]:
    """Validate every row; unknown columns never silently become unused settings."""
    frame = read_mapping_table(cfg.mapping_file)
    missing = sorted(set(COLUMNS) - {"comment"} - set(frame.columns))
    unknown = sorted(set(frame.columns) - set(COLUMNS) - set(MODEL_FIELDS))
    if missing:
        raise ValueError(f"{cfg.mapping_file.name}: missing mapping columns {missing}")
    if unknown:
        raise ValueError(f"{cfg.mapping_file.name}: unknown mapping columns {unknown}; put notes in comment")
    if cfg.configuration_mode == "global":
        ignored = [name for name in MODEL_FIELDS if name in frame
                   and frame[name].astype(str).str.strip().ne("").any()]
        if ignored:
            get_logger().warning("Global configuration ignores mapping model cells: %s", ", ".join(ignored))
    products, errors = [], []
    for index, row in frame.iterrows():
        label = f"row {index + 2} ({row['product']!r})"
        try:
            values = {name: str(row.get(name, "")).strip() for name in COLUMNS}
            values["use"] = values["use"].lower()
            if not values["product"]:
                raise ValueError("product must not be empty")
            if values["use"] not in USES:
                raise ValueError("use must be fill or off; helper products are not part of this model")
            if values["hours"] not in HOURS:
                raise ValueError("hours must be Base, Peak or Peak7")
            values["timezone"] = values["timezone"] or cfg.tz(values["area"])
            validate_timezone(values["timezone"])
            product = ProductMap(**values)
            if cfg.configuration_mode == "individual":
                product.parameter_overrides = _parameter_cells(row)
                effective_model(cfg, product)
            products.append(product)
        except ValueError as exc:
            errors.append(f"{label}: {exc}")
    if errors:
        raise ValueError("Mapping errors:\n  " + "\n  ".join(errors))
    keys = [product.key for product in products]
    if len(set(keys)) != len(keys):
        duplicate = sorted({key for key in keys if keys.count(key) > 1})
        raise ValueError(f"Duplicate curve identities in mapping: {duplicate}")
    return products


def eex_path(cfg: Config, mapping: ProductMap) -> Path | None:
    """Resolve the mapping's EEX assignment relative to the configured curve root."""
    return cfg.eex_curves_dir / mapping.eex_file if mapping.eex_file else None


def guess_row(product: str, cfg: Config, region: str = "", unit: str = "") -> dict[str, str]:
    """Draft a disabled row; guesses require review before explicitly enabling it."""
    match = re.match(r"^(?P<area>[A-Za-z0-9]+)_(?P<rest>.+)$", product)
    area = match.group("area") if match else ""
    rest = (match.group("rest") if match else product).lower()
    profile = "Base" if rest.startswith("base") else "Peak" if rest.startswith("peak") else "Other"
    row = {"product": product, "region": region, "unit": unit, "use": "off", "area": area,
           "profile": profile, "eex_file": "", "hours": "Peak" if profile == "Peak" else "Base",
           "timezone": cfg.tz(area), "comment": "Review identity, EEX assignment, units and hours before enabling"}
    folder = cfg.eex_curves_dir / area
    if profile != "Other" and folder.is_dir():
        choices = sorted(path.stem for path in folder.glob("*.csv") if not path.stem.endswith("_wide"))
        choices = [name for name in choices if name == profile] or [name for name in choices if name.startswith(profile)]
        if choices:
            chosen = choices[0]
            row["eex_file"] = f"{area}/{chosen}.csv"
            if "Mo-Su" in chosen:
                row["hours"] = "Peak7"
    return row
