"""Small command-line interface for setup, production, refill and backtesting."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date, timedelta
import json
from pathlib import Path
import sys

import pandas as pd

from vwapsb.config import MODEL_FIELDS, load_config
from vwapsb.dates import parse_reference_dates
from vwapsb.demo import create_demo
from vwapsb.identity import normalize_identity
from vwapsb.io_eex import load_eex_books
from vwapsb.io_vwap import load_input
from vwapsb.log import setup_logging
from vwapsb.mapping import COLUMNS, guess_row, load_mapping, read_mapping_table
from vwapsb.publication import CsvBatch, publication_lock


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="VWAPSb: transparent additive power curve filling")
    result.add_argument("command", choices=["mapping", "check", "daily", "refill", "catchup", "backtest", "demo"])
    result.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[2] / "config.toml")
    result.add_argument("--vwap", help="Override the own-input path/glob; relative to the configuration")
    result.add_argument("--date", type=date.fromisoformat, help="Daily reference date, YYYY-MM-DD")
    result.add_argument("--start", type=date.fromisoformat, help="Inclusive start, YYYY-MM-DD")
    result.add_argument("--end", type=date.fromisoformat, help="Inclusive end, YYYY-MM-DD")
    result.add_argument("--eex-offset", type=int, help="Latest allowed EEX publication: reference date + 0/-1/-2/... calendar days")
    result.add_argument("--grid", type=Path, help="Backtest JSON dictionary of model parameter lists")
    result.add_argument("--mask-fraction", type=float, default=1.0, help="Backtest fraction of known physical periods to test separately")
    result.add_argument("--seed", type=int, default=42, help="Backtest sampling seed")
    return result


def _range(args, raw, cfg) -> list[date]:
    if args.command == "daily":
        if args.start or args.end:
            raise ValueError("Use --date for daily, not --start/--end")
        return [args.date or date.today()]
    if args.date:
        raise ValueError("Use --start/--end for refill, catchup and backtest")
    original_dates = parse_reference_dates(raw[cfg.vwap_columns["reference_date"]]).dt.date
    start = args.start or (original_dates.min() if not original_dates.empty else None)
    end = args.end or (date.today() - timedelta(days=1) if args.command == "catchup"
                       else original_dates.max() if not original_dates.empty else None)
    if not isinstance(start, date) or not isinstance(end, date):
        raise ValueError("Provide --start and --end when the input has no dates")
    if start > end:
        raise ValueError("--start must be on or before --end")
    return [day.date() for day in pd.date_range(start, end)]


def _draft_mapping(raw, cfg) -> None:
    """Append disabled identities; never overwrite reviewed mapping values."""
    path = cfg.mapping_file
    with publication_lock(path.parent):
        if path.exists():
            load_mapping(cfg)  # Validate before preserving an existing table.
            table = read_mapping_table(path)
        else:
            table = pd.DataFrame(columns=[*COLUMNS, *MODEL_FIELDS])
        known = set(table[["product", "region", "unit"]].itertuples(index=False, name=None))
        rows = []
        for key in normalize_identity(raw, cfg).drop_duplicates().itertuples(index=False, name=None):
            if key not in known:
                rows.append(guess_row(key[0], cfg, region=key[1], unit=key[2]))
        if rows:
            table = pd.concat([table, pd.DataFrame(rows)], ignore_index=True).fillna("")
        with CsvBatch() as batch:
            if path.suffix.lower() == ".xlsx":
                batch.stage_excel(path, table)
            elif path.suffix.lower() == ".csv":
                batch.stage(path, table)
            else:
                raise ValueError("Mapping destination must be .csv or .xlsx")
    print(f"Mapping ready: {path}. Review EEX files, units and delivery profiles, then set use=fill.")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "demo":
            root = Path(__file__).resolve().parents[2]
            args.config = create_demo(root)
            args.command = "refill"
            args.start = date(2026, 9, 21)
            args.end = date(2026, 10, 9)
            print(f"Synthetic demo configuration: {args.config}")
        cfg = load_config(args.config)
        if args.vwap:
            cfg = replace(cfg, vwap_input=args.vwap)
        if args.eex_offset is not None:
            cfg = replace(cfg, eex_offset_days=args.eex_offset)
        log = setup_logging(cfg.output_dir / "logs", args.command)
        observations, raw = load_input(cfg.vwap_input, cfg)
        if args.command == "mapping":
            _draft_mapping(raw, cfg)
            return 0
        mappings = load_mapping(cfg)
        books = load_eex_books(cfg, mappings)
        if args.command == "check":
            active = [m for m in mappings if m.active]
            if not active:
                raise ValueError("No enabled mappings. Review the table and set use=fill with eex_file.")
            reference = args.date or date.today()
            failures = []
            for mapping in active:
                book = books.get(mapping.key)
                publication = book.available_asof(reference, cfg.max_stale_days or None, cfg.eex_offset_days) if book else None
                log.info("%s | EEX latest permitted=%s | reference=%s | offset=%s",
                         mapping.label, publication, reference, cfg.eex_offset_days)
                if publication is None:
                    failures.append(mapping.label)
            if failures:
                raise ValueError(f"No permitted fresh EEX publication for: {failures}")
            log.info("Input and mappings readable. %d active curves. Calendar conventions still require business validation.", len(active))
            return 0
        dates = _range(args, raw, cfg)
        if args.command == "backtest":
            from vwapsb.backtest import run_backtest

            grid = json.loads(args.grid.read_text(encoding="utf-8")) if args.grid else None
            reports = run_backtest(observations, books, mappings, cfg, dates[0], dates[-1],
                                   grid=grid, mask_fraction=args.mask_fraction, seed=args.seed)
            destination = cfg.output_dir / "backtest"
            with publication_lock(destination), CsvBatch() as batch:
                for name, frame in reports.items():
                    batch.stage(destination / f"{name}.csv", frame)
            log.info("Backtest reports: %s. Compare coverage and errors within the same unit and curve.", destination)
            return 0
        from vwapsb.workflow import run_fill

        run_fill(observations, raw, books, mappings, cfg, dates, args.command)
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
