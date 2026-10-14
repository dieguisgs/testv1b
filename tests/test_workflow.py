"""Production publication and original-cell preservation contract tests."""
from dataclasses import replace
from datetime import date
import json

import pandas as pd
import pytest

from vwapsb.config import Config, load_config
from vwapsb.demo import create_demo
from vwapsb.enrich import enrich_input
from vwapsb.io_eex import load_eex_books
from vwapsb.io_vwap import load_input
from vwapsb.mapping import load_mapping
from vwapsb.workflow import read_output, run_fill


@pytest.fixture(scope="module")
def demo_bundle(tmp_path_factory):
    root = tmp_path_factory.mktemp("standalone_demo")
    path = create_demo(root)
    cfg = load_config(path)
    observations, raw = load_input(cfg.vwap_input, cfg)
    mappings = load_mapping(cfg)
    books = load_eex_books(cfg, mappings)
    return cfg, observations, raw, mappings, books


def test_daily_is_idempotent_and_preserves_other_dates(demo_bundle, tmp_path):
    cfg, observations, raw, mappings, books = demo_bundle
    cfg = replace(cfg, output_dir=tmp_path)
    first = date(2026, 10, 8)
    second = date(2026, 10, 9)
    initial = run_fill(observations, raw, books, mappings, cfg, [first, second])
    repeated = run_fill(observations, raw, books, mappings, cfg, [second], "daily")
    assert len(repeated) == len(initial)
    assert set(repeated.date) == {str(first), str(second)}
    assert not repeated.duplicated(["date", "product", "region", "unit", "tenor"]).any()
    loaded = read_output(tmp_path / "filled.csv")
    assert len(loaded) == len(initial)
    manifest = json.loads((tmp_path / "run_manifest.json").read_text())
    assert manifest["mode"] == "daily"
    assert manifest["dates"] == [str(second)]


def test_catchup_adds_absent_row_and_rejects_changed_settings(demo_bundle, tmp_path):
    cfg, observations, raw, mappings, books = demo_bundle
    cfg = replace(cfg, output_dir=tmp_path)
    day = date(2026, 10, 9)
    original = run_fill(observations, raw, books, mappings, cfg, [day])
    original.iloc[1:].to_csv(tmp_path / "filled.csv", index=False)
    caught = run_fill(observations, raw, books, mappings, cfg, [day], "catchup")
    assert len(caught) == len(original)
    changed = replace(cfg, model=replace(cfg.model, local_max_steps=1))
    with pytest.raises(ValueError, match="configuration differs"):
        run_fill(observations, raw, books, mappings, changed, [day], "catchup")
    # An explicit refill is the path to recalculation with changed parameters.
    run_fill(observations, raw, books, mappings, changed, [day], "refill")


def test_enriched_keeps_original_cells_even_duplicate_and_unmapped(tmp_path):
    cfg = Config(root=tmp_path)
    raw = pd.DataFrame([
        {"reference_date": "2026-10-09", "product": "NA", "region": "", "unit": "EUR/MWh",
         "tenor2": "M+1", "vwap": "1,20", "total_volume": "0", "note": "NA"},
        {"reference_date": "2026-10-09", "product": "NA", "region": "", "unit": "EUR/MWh",
         "tenor2": "M+1", "vwap": "2,40", "total_volume": "10", "note": "original"},
        {"reference_date": "2026-10-09", "product": "OFF", "region": "", "unit": "EUR/MWh",
         "tenor2": "M+1", "vwap": "", "total_volume": "", "note": "keep"}])
    filled = pd.DataFrame([{"date": "2026-10-09", "product": "NA", "region": "", "unit": "EUR/MWh",
                            "tenor": "M+1", "price": 2.4, "status": "original", "method": "original"},
                           {"date": "2026-10-09", "product": "NA", "region": "", "unit": "EUR/MWh",
                            "tenor": "M+2", "price": 3.0, "status": "estimated", "method": "history"}])
    enriched = enrich_input(raw, filled, cfg)
    pd.testing.assert_frame_equal(enriched.iloc[:3][list(raw)].reset_index(drop=True), raw)
    assert enriched.refill_price.iloc[:2].tolist() == [1.2, 2.4]
    assert enriched.refill_method.iloc[2] == "not_processed"
    assert bool(enriched.refill_generated.iloc[3])
    assert enriched.vwap.iloc[3] == ""


def test_empty_active_mapping_does_not_publish(demo_bundle, tmp_path):
    cfg, observations, raw, mappings, books = demo_bundle
    cfg = replace(cfg, output_dir=tmp_path)
    with pytest.raises(ValueError, match="No enabled mappings"):
        run_fill(observations, raw, books, [], cfg, [date(2026, 10, 9)])
    assert not (tmp_path / "filled.csv").exists()


def test_old_curve_is_retained_when_mapping_removed(demo_bundle, tmp_path):
    cfg, observations, raw, mappings, books = demo_bundle
    cfg = replace(cfg, output_dir=tmp_path)
    day = date(2026, 10, 9)
    original = run_fill(observations, raw, books, mappings, cfg, [day])
    old_audit = pd.read_csv(tmp_path / "consistency.csv")
    # A later execution can contain a different active curve. It must not try
    # to audit old retained rows using a missing/currently different mapping.
    other = replace(mappings[0], product="OTHER")
    new_obs = observations.assign(product="OTHER")
    updated = run_fill(new_obs, raw, {other.key: books[mappings[0].key]}, [other], cfg, [day])
    assert set(updated["product"]) == {"SYNTHETIC_POWER", "OTHER"}
    assert len(updated) == 2 * len(original)
    new_audit = pd.read_csv(tmp_path / "consistency.csv")
    assert len(new_audit[new_audit["product"] == "SYNTHETIC_POWER"]) == len(old_audit)


def test_partial_catchup_keeps_equivalent_alias_prices_identical(demo_bundle, tmp_path):
    cfg, observations, raw, mappings, books = demo_bundle
    cfg = replace(cfg, output_dir=tmp_path, targets=["D+1", "BOW"])
    saturday = date(2026, 9, 26)
    original = run_fill(observations, raw, books, mappings, cfg, [saturday])
    assert len(original) == 2
    assert original.price.iloc[0] == original.price.iloc[1]
    original.loc[original.tenor != "BOW"].to_csv(tmp_path / "filled.csv", index=False)
    # One alias is recomputed; its sibling must survive CSV round-tripping
    # exactly, so the audit does not reject identical physical deliveries.
    result = run_fill(observations, raw, books, mappings, cfg, [saturday], "catchup")
    assert result.price.iloc[0] == result.price.iloc[1]
