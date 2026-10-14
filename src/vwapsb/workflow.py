"""Idempotent, scoped publication of daily and historical refill results."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timezone
import json
from pathlib import Path

import pandas as pd

from vwapsb.audit import audit_consistency
from vwapsb.config import configuration_id, effective_model
from vwapsb.engine import fill_curves
from vwapsb.enrich import enrich_input
from vwapsb.log import get_logger
from vwapsb.publication import CsvBatch, publication_lock

KEY = ["date", "product", "region", "unit", "tenor"]


def _keys(frame: pd.DataFrame) -> list[tuple]:
    return list(frame[KEY].itertuples(index=False, name=None))


def read_output(path: Path) -> pd.DataFrame:
    """Read saved identifiers literally, including strings such as NA."""
    frame = pd.read_csv(path, keep_default_na=False, dtype={c: str for c in KEY}, float_precision="round_trip")
    required = [*KEY, "configuration_id", "price", "status", "method", "delivery_start", "delivery_end"]
    if any(c not in frame for c in required):
        raise ValueError(f"{path}: not a VWAPSb filled.csv output")
    if frame.duplicated(KEY).any():
        raise ValueError(f"{path}: duplicate canonical keys")
    frame["price"] = pd.to_numeric(frame["price"], errors="coerce")
    return frame


def run_fill(observations, raw, books, mappings, cfg, dates: list[date], mode="refill") -> pd.DataFrame:
    """Publish results; catchup only replaces missing/unprocessed target rows.

    Refill and daily replace the active curves within their requested dates.
    Catchup rejects configuration changes in its date scope; use refill for
    revised observations, model settings or target definitions.
    """
    if mode not in {"daily", "refill", "catchup"}:
        raise ValueError(f"Unknown execution mode: {mode}")
    active = [m for m in mappings if m.active]
    if not active:
        raise ValueError("No enabled mappings with an EEX file. Review use=fill and eex_file.")
    if not dates:
        raise ValueError("No reference dates selected")
    ids = {m.key: configuration_id(cfg, m) for m in active}
    date_text = {d.isoformat() for d in dates}
    output_path = cfg.output_dir / "filled.csv"
    with publication_lock(cfg.output_dir):
        existing = read_output(output_path) if output_path.exists() else pd.DataFrame()
        scope = pd.Series(False, index=existing.index)
        if not existing.empty:
            curve_keys = list(existing[["product", "region", "unit"]].itertuples(index=False, name=None))
            scope = existing["date"].isin(date_text) & pd.Series([key in ids for key in curve_keys], index=existing.index)
            if mode == "catchup":
                for idx in existing.index[scope]:
                    if existing.at[idx, "configuration_id"] != ids[curve_keys[idx]]:
                        raise ValueError("Catchup configuration differs from saved results. Run refill for this date range.")
        fresh = fill_curves(observations, books, mappings, cfg, dates)
        if fresh.empty:
            raise ValueError("No valid target periods were produced for this date range")
        fresh = fresh.copy()
        fresh["date"] = fresh["date"].astype(str)
        if mode == "catchup" and not existing.empty:
            complete = set(_keys(existing.loc[existing["price"].notna()]))
            fresh = fresh.loc[[key not in complete for key in _keys(fresh)]]
            replacements = set(_keys(fresh))
            retained = existing.loc[[key not in replacements for key in _keys(existing)]]
        else:
            retained = existing.loc[~scope] if not existing.empty else pd.DataFrame()
        if retained.empty:
            combined = fresh.reset_index(drop=True)
        elif fresh.empty:
            combined = retained.reset_index(drop=True)
        else:
            combined = pd.concat([retained, fresh], ignore_index=True)
        combined = combined.sort_values(KEY).reset_index(drop=True)
        if combined.duplicated(KEY).any():
            raise ValueError("Internal error: duplicate output keys; no results published")
        # Re-audit only this execution's scope. Retained historical rows may
        # belong to mappings/calendars that are no longer in the current file.
        audit_scope = combined["date"].isin(date_text) & pd.Series(
            [key in ids for key in combined[["product", "region", "unit"]].itertuples(index=False, name=None)],
            index=combined.index)
        audit = audit_consistency(combined.loc[audit_scope], mappings)
        audit_path = cfg.output_dir / "consistency.csv"
        if audit_path.exists():
            prior_audit = pd.read_csv(audit_path, keep_default_na=False, dtype={c: str for c in KEY[:4]})
            required_audit = set(KEY[:4]) | {"parent_tenor", "difference"}
            if required_audit - set(prior_audit):
                raise ValueError("Existing consistency.csv has an invalid schema")
            audit_keys = prior_audit[["product", "region", "unit"]].itertuples(index=False, name=None)
            replace_audit = prior_audit["date"].isin(date_text) & pd.Series([key in ids for key in audit_keys], index=prior_audit.index)
            old_audit = prior_audit.loc[~replace_audit]
            if not old_audit.empty:
                audit = old_audit if audit.empty else pd.concat([old_audit, audit], ignore_index=True)
        enriched = enrich_input(raw, combined, cfg)
        manifest = {"schema_version": 1, "project": "VWAPSb", "mode": mode,
                    "created_utc": datetime.now(timezone.utc).isoformat(),
                    "dates": sorted(date_text), "eex_offset_days": cfg.eex_offset_days,
                    "max_stale_days": cfg.max_stale_days, "rows_written": len(fresh),
                    "total_rows": len(combined), "configuration_mode": cfg.configuration_mode,
                    "curves": [{"product": m.product, "region": m.region, "unit": m.unit,
                                "configuration_id": ids[m.key], "model": asdict(effective_model(cfg, m))}
                               for m in active],
                    "note": "This manifest describes the latest execution; individual rows retain their configuration_id."}
        with CsvBatch() as batch:
            batch.stage(output_path, combined)
            batch.stage(cfg.output_dir / "enriched.csv", enriched)
            batch.stage(audit_path, audit)
            batch.stage_text(cfg.output_dir / "run_manifest.json", json.dumps(manifest, indent=2))
        logger = get_logger()
        logger.info("Published %d new/recalculated rows; %d total rows -> %s", len(fresh), len(combined), output_path)
        # Print availability once per curve and date, including missing outputs.
        if not fresh.empty:
            for row in fresh[["date", "product", "region", "unit", "eex_cutoff", "eex_date"]].drop_duplicates().itertuples(index=False):
                logger.info("Curve %s [%s, %s] | reference=%s | EEX cutoff=%s | actual publication=%s",
                            row.product, row.region, row.unit, row.date, row.eex_cutoff, row.eex_date)
        return combined
