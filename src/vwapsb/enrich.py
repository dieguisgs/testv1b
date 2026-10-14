"""Append refill information while preserving every original input cell."""
from __future__ import annotations

import math
import pandas as pd

from vwapsb.dates import parse_reference_dates
from vwapsb.identity import normalize_identity
from vwapsb.io_vwap import _to_number


def enrich_input(raw: pd.DataFrame, filled: pd.DataFrame, cfg) -> pd.DataFrame:
    """Keep original rows and add target rows absent from the current input.

    A finite input price always remains the final value for its original row,
    including duplicate observations. The canonical filled table may instead
    contain the volume-weighted consolidation of those duplicates.
    """
    reserved = [c for c in raw if str(c).startswith("refill_")]
    if reserved:
        raise ValueError(f"Input already has reserved refill_ columns: {reserved}. Use the original input.")
    columns = cfg.vwap_columns
    identity = normalize_identity(raw, cfg)
    dates = parse_reference_dates(raw[columns["reference_date"]]).dt.date
    tenors = raw[columns["tenor"]].astype(str).str.strip()
    own = _to_number(raw[columns["vwap"]])
    lookup = {}
    for row in filled.to_dict("records"):
        key = (str(row["date"]), row["product"], row["region"], row["unit"], row["tenor"])
        if key in lookup:
            raise ValueError(f"Duplicate canonical output key: {key}")
        lookup[key] = row
    output, represented = [], set()
    for position, (_, raw_row) in enumerate(raw.iterrows()):
        key = (str(dates.iloc[position]), *identity.iloc[position].tolist(), tenors.iloc[position])
        represented.add(key)
        result = lookup.get(key, {})
        row = raw_row.to_dict()
        row.update({f"refill_{name}": val for name, val in result.items()})
        observed = math.isfinite(float(own.iloc[position]))
        if observed:
            row.update(refill_price=float(own.iloc[position]), refill_status="original",
                       refill_method="original", refill_reason="Original input row preserved")
            # The canonical aggregation's explanation must not be mistaken for
            # the method used to retain this individual raw row.
            row["refill_details"] = '{"retained_input_row":true}'
        elif not result:
            row.update(refill_price=float("nan"), refill_status="missing",
                       refill_method="not_processed", refill_reason="No matching canonical output row")
        row.update(refill_generated=False, refill_input_row=position + 1)
        output.append(row)
    for key, result in lookup.items():
        if key in represented:
            continue
        row = {name: "" for name in raw.columns}
        row.update({columns["reference_date"]: key[0], columns["product"]: key[1],
                    columns["region"]: key[2], columns["unit"]: key[3], columns["tenor"]: key[4]})
        row.update({f"refill_{name}": val for name, val in result.items()})
        row.update(refill_generated=True, refill_input_row="")
        output.append(row)
    return pd.DataFrame(output)

