"""Create reproducible synthetic input without accessing external data."""
from __future__ import annotations

from datetime import date, timedelta
import math
from pathlib import Path

import pandas as pd

from vwapsb.hours import hours_fn
from vwapsb.tenors import add_months, resolve_tenor


def create_demo(root: str | Path) -> Path:
    """Write synthetic inputs under root/output/demo and return its config path.

    Only deterministic demo files are replaced. Real configuration and inputs
    are never read. The price generator is deliberately a simple test fixture,
    not a model of actual market behavior.
    """
    directory = Path(root).resolve() / "output" / "demo"
    eex_dir = directory / "eex" / "DE"
    eex_dir.mkdir(parents=True, exist_ok=True)
    publications = [d.date() for d in pd.bdate_range("2026-08-03", "2026-10-09")]
    hfn = hours_fn("Base", "Europe/Berlin")

    def value(start: date, end: date, publication: date) -> float:
        months = []
        current = start
        while current < end:
            stop = min(add_months(current.replace(day=1), 1), end)
            seasonal = 80 + 12 * math.cos(2 * math.pi * (current.month - 1) / 12)
            forward = .005 * (current - date(2026, 8, 1)).days
            movement = .15 * (publication - date(2026, 8, 1)).days
            months.append((seasonal + forward + movement, hfn(current, stop)))
            current = stop
        return sum(p * h for p, h in months) / sum(h for _, h in months)

    eex_rows = []
    own_rows = []
    eex_labels = [*[f"D+{i}" for i in range(1, 36)],
                  *["WE", "WE+1", "WE+2", "WE+3"],
                  *[f"W+{i}" for i in range(1, 6)],
                  *[f"M+{i}" for i in range(0, 49)],
                  *[f"Q+{i}" for i in range(0, 17)],
                  *[f"{s}+{i}" for s in ("Sum", "Win") for i in range(1, 5)],
                  *[f"Cal+{i}" for i in range(1, 5)]]
    for day_index, publication in enumerate(publications):
        seen = set()
        for label in eex_labels:
            period = resolve_tenor(label, publication)
            if period.key in seen:
                continue
            seen.add(period.key)
            eex_rows.append({"tradeDate": publication, "maturityType": period.kind,
                             "deliveryStart": period.start,
                             "settlPx": round(value(period.start, period.end, publication), 8)})
        if publication < date(2026, 9, 1):
            continue
        labels = ["M+1", "D+1"] if day_index % 3 else []
        if day_index % 2:
            labels += ["M+4"]
        if day_index % 4 == 0:
            labels += ["Q+2"]
        if day_index % 9 == 0:
            labels += ["Cal+1"]
        for label in [*labels, "M+2", "Q+1"]:
            period = resolve_tenor(label, publication)
            basis = {"Day": 7.0, "Month": 3.0, "Quarter": 4.0, "Year": 2.0}[period.kind]
            price = value(period.start, period.end, publication) + basis + .4 * math.sin(day_index)
            own_rows.append({"reference_date": publication, "product": "SYNTHETIC_POWER",
                             "region": "DE", "unit": "EUR/MWh", "tenor2": label,
                             "vwap": round(price, 6) if label in labels else "",
                             "total_volume": 10 + day_index % 5 if label in labels else "",
                             "note": "SYNTHETIC example only"})
    pd.DataFrame(eex_rows).to_csv(eex_dir / "Base.csv", index=False)
    pd.DataFrame(own_rows).to_csv(directory / "own.csv", index=False)
    pd.DataFrame([{"product": "SYNTHETIC_POWER", "region": "DE", "unit": "EUR/MWh",
                   "use": "fill", "area": "DE", "profile": "Base", "eex_file": "DE/Base.csv",
                   "hours": "Base", "timezone": "Europe/Berlin", "comment": "Synthetic example"}]
                 ).to_csv(directory / "products.csv", index=False)
    config = '''# Reproducible synthetic fixture. Never use as a calibration result.
[paths]
vwap_input = "own.csv"
mapping = "products.csv"
eex_curves_dir = "eex"
output_dir = "results"

[targets]
tenors = ["D+1", "D+2", "D+3", "WE", "WE+1", "BOW", "W+1", "W+2", "BOM", "M+1", "M+2", "M+3", "M+4", "M+5", "M+6", "M+7", "M+8", "M+9", "M+10", "Q+1", "Q+2", "Q+3", "Q+4", "Sum+1", "Win+1", "Cal+1", "Cal+2"]

[eex]
offset_days = 0
max_stale_days = 7

[run]
configuration_mode = "global"

[model]
local_enabled = true
history_enabled = true
local_max_steps = 2
local_min_anchors = 1
min_volume = 0
history_window_days = 30
history_min_days = 3
eex_window = 5
eex_average = "ewma"
eex_halflife = 2.0
'''
    config_path = directory / "config.toml"
    config_path.write_text(config, encoding="utf-8")
    return config_path
