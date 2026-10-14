"""Validate standalone configuration, source contracts and delivery calendars."""

from dataclasses import FrozenInstanceError, asdict, replace
from datetime import date, datetime, timedelta
import logging
from pathlib import Path

import pandas as pd
import pytest

from vwapsb import publication
from vwapsb.config import Config, MODEL_FIELDS, Model, configuration_id, default_targets, effective_model, load_config, model_from_overrides
from vwapsb.dates import parse_reference_dates
from vwapsb.hours import hours_fn, period_hours
from vwapsb.io_eex import EexBook, REQUIRED, load_eex_books
from vwapsb.io_vwap import load_input
from vwapsb.mapping import COLUMNS, ProductMap, guess_row, load_mapping
from vwapsb.pricer import Pricer
from vwapsb.publication import CsvBatch, publication_lock
from vwapsb.tenors import period_from_eex, resolve_tenor


def product(**overrides):
    return ProductMap("POWER", "fill", "DE", "Base", "DE/Base.csv", "Base", "Europe/Berlin",
                      region="North", unit="EUR/MWh", parameter_overrides=overrides)


def raw_input():
    return pd.DataFrame({"reference_date": ["2026-09-01"], "product": ["POWER"],
                         "region": ["North"], "unit": ["EUR/MWh"], "tenor2": ["M+1"],
                         "vwap": [110], "total_volume": [10]})


def mapping_frame(cfg, **overrides):
    row = guess_row("POWER", cfg, "North", "EUR/MWh")
    row.update(use="fill", eex_file="DE/Base.csv", **overrides)
    return pd.DataFrame([row])


def test_model_has_exactly_ten_controls_and_is_immutable():
    assert len(MODEL_FIELDS) == 10
    assert asdict(Model()) == dict(local_enabled=True, history_enabled=True, local_max_steps=2,
                                  min_volume=0.0, history_window_days=30, history_min_days=3,
                                  eex_window=5, eex_average="ewma", eex_halflife=2.0,
                                  local_min_anchors=1)
    with pytest.raises(FrozenInstanceError):
        Model().eex_window = 7


@pytest.mark.parametrize("name,value", [
    ("local_enabled", "false"), ("history_enabled", 1), ("local_max_steps", -1),
    ("local_max_steps", True), ("history_window_days", 0), ("history_min_days", 0),
    ("history_min_days", 31), ("history_min_days", 2.5), ("eex_window", 1),
    ("eex_window", 5.0), ("local_min_anchors", 0), ("min_volume", -1),
    ("min_volume", "0"), ("min_volume", True), ("min_volume", float("nan")),
    ("eex_halflife", float("inf")), ("eex_halflife", 0), ("eex_average", "median"),
])
def test_model_rejects_invalid_or_coerced_values(name, value):
    with pytest.raises(ValueError, match=name):
        model_from_overrides(Model(), {name: value})


def test_unknown_model_field_is_a_configuration_error():
    with pytest.raises(ValueError, match="Unknown model.*tau_log"):
        model_from_overrides(Model(), {"tau_log": 0.5})


@pytest.mark.parametrize("text,match", [
    ('[layers]\nlocal=true\n', "Unknown configuration sections"),
    ('[model]\neex_windows=5\n', "Unknown model"),
    ('[run]\nconfiguration_mode="auto"\n', "configuration_mode"),
    ('[paths]\noutput_dir=false\n', "paths.output_dir"),
    ('[eex]\noffset_days=1\n', "offset_days"),
    ('[eex]\noffset_days=-1.0\n', "offset_days"),
    ('[eex]\nmax_stale_days=-1\n', "max_stale_days"),
    ('[eex]\nmax_stale_days=false\n', "max_stale_days"),
    ('[targets]\ntenors=[]\n', "targets.tenors"),
    ('[targets]\ntenors="M+1"\n', "targets.tenors"),
    ('[targets]\ntenors=["Win+0"]\n', "unsupported tenor"),
    ('[targets]\ntenors=["M+1",2]\n', "unsupported tenor"),
    ('[conventions]\nday="business_days"\n', "conventions.day"),
    ('[conventions]\nweekend_offset=0.5\n', "weekend_offset"),
    ('[timezones]\ndefault="Invalid/Zone"\n', "timezone"),
    ('[vwap_columns]\ntenor="product"\n', "distinct"),
    ('[vwap_columns]\nprice="vwap"\n', "Unknown vwap_columns"),
])
def test_toml_typos_and_invalid_settings_fail_early(tmp_path, text, match):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        load_config(path)


def test_supplied_config_has_all_target_families_and_resolves_paths():
    path = Path(__file__).resolve().parents[1] / "config.toml"
    cfg = load_config(path)
    assert cfg.root == path.parent
    assert cfg.mapping_file == cfg.root / "mappings/products.csv"
    assert cfg.output_dir == cfg.root / "output"
    assert cfg.targets == default_targets() and len(cfg.targets) == 40
    assert cfg.max_stale_days == 7 and cfg.configuration_mode == "global"
    assert cfg.vwap_columns["tenor"] == "tenor2"


def test_default_lists_are_independent_and_zero_staleness_is_preserved(tmp_path):
    first, second = Config(tmp_path, max_stale_days=0), Config(tmp_path)
    first.targets.append("M+11")
    assert "M+11" not in second.targets
    assert first.max_stale_days == 0


@pytest.mark.parametrize("suffix", [".csv", ".xlsx"])
def test_mapping_individual_fixed_values_and_global_ignore_warning(tmp_path, suffix, caplog):
    cfg = Config(tmp_path, mapping_file=Path("mapping" + suffix), configuration_mode="individual")
    frame = mapping_frame(cfg, local_enabled="false", eex_window="7", min_volume="", local_max_steps="1")
    if suffix == ".csv":
        frame.to_csv(cfg.mapping_file, index=False)
    else:
        frame.to_excel(cfg.mapping_file, index=False)
    mapping = load_mapping(cfg)[0]
    assert effective_model(cfg, mapping) == replace(Model(), local_enabled=False, eex_window=7, local_max_steps=1)
    assert mapping.key == ("POWER", "North", "EUR/MWh") and mapping.active
    caplog.set_level(logging.WARNING, logger="vwapsb")
    global_cfg = replace(cfg, configuration_mode="global")
    assert load_mapping(global_cfg)[0].parameter_overrides == {}
    assert "ignores mapping model cells" in caplog.text


def test_global_ignores_invalid_known_cells_but_unknown_columns_always_fail(tmp_path):
    cfg = Config(tmp_path, mapping_file=Path("mapping.csv"))
    mapping_frame(cfg, eex_window="not-an-integer").to_csv(cfg.mapping_file, index=False)
    assert load_mapping(cfg)[0].parameter_overrides == {}
    with pytest.raises(ValueError, match="row 2.*eex_window"):
        load_mapping(replace(cfg, configuration_mode="individual"))
    mapping_frame(cfg, eex_windows="5").to_csv(cfg.mapping_file, index=False)
    with pytest.raises(ValueError, match="unknown mapping columns"):
        load_mapping(cfg)


@pytest.mark.parametrize("field,value,match", [
    ("use", "helper", "fill or off"), ("hours", "Unknown", "hours"),
    ("timezone", "Invalid/Timezone", "timezone"), ("product", "", "product"),
])
def test_mapping_rejects_invalid_scope_or_delivery_conventions(tmp_path, field, value, match):
    cfg = Config(tmp_path, mapping_file=Path("mapping.csv"))
    frame = mapping_frame(cfg)
    frame.loc[0, field] = value
    frame.to_csv(cfg.mapping_file, index=False)
    with pytest.raises(ValueError, match=match):
        load_mapping(cfg)


def test_mapping_requires_complete_unique_identity_with_literal_blank_regions(tmp_path):
    cfg = Config(tmp_path, mapping_file=Path("mapping.csv"))
    frame = mapping_frame(cfg)
    other = frame.copy()
    other["region"] = ""
    pd.concat([frame, other], ignore_index=True).to_csv(cfg.mapping_file, index=False)
    assert len(load_mapping(cfg)) == 2
    pd.concat([frame, frame], ignore_index=True).to_csv(cfg.mapping_file, index=False)
    with pytest.raises(ValueError, match="Duplicate curve identities"):
        load_mapping(cfg)
    frame.drop(columns="unit").to_csv(cfg.mapping_file, index=False)
    with pytest.raises(ValueError, match="missing mapping.*unit"):
        load_mapping(cfg)


def test_configuration_fingerprint_is_portable_but_detects_model_and_context_changes(tmp_path):
    cfg = Config(tmp_path, configuration_mode="individual")
    mapping = product(eex_halflife=2)
    fingerprint = configuration_id(cfg, mapping)
    relocated = replace(cfg, root=tmp_path / "another_machine", output_dir=tmp_path / "other_output")
    assert fingerprint == configuration_id(relocated, product(eex_halflife=2.0))
    assert fingerprint != configuration_id(cfg, product(eex_window=7))
    assert fingerprint != configuration_id(replace(cfg, eex_offset_days=-1), mapping)
    assert fingerprint != configuration_id(replace(cfg, targets=["M+1"]), mapping)
    assert fingerprint != configuration_id(cfg, replace(mapping, timezone="Europe/London"))
    assert fingerprint != configuration_id(cfg, replace(mapping, eex_file="FR/Base.csv"))


@pytest.mark.parametrize("suffix", [".csv", ".xlsx"])
def test_input_preserves_raw_rows_and_identifiers_while_normalizing_values(tmp_path, suffix):
    cfg = Config(tmp_path)
    raw = pd.concat([raw_input()] * 4, ignore_index=True)
    raw["reference_date"] = ["01/09/2026", "2026-09-01", datetime(2026, 9, 1), "2026-09-01"]
    raw["product"] = "001"
    raw["region"] = "NA"
    raw["vwap"] = ["100,5", "0", "-5", "invalid"]
    raw["total_volume"] = ["10", "", "-1", "5"]
    path = tmp_path / ("own" + suffix)
    if suffix == ".csv":
        raw.to_csv(path, index=False)
    else:
        raw.to_excel(path, index=False)
    normalized, original = load_input(path, cfg)
    assert normalized.vwap.tolist() == [100.5, 0.0, -5.0]
    assert normalized.date.tolist() == [date(2026, 9, 1)] * 3
    assert normalized["product"].eq("001").all() and normalized.region.eq("NA").all()
    assert normalized.volume.isna().tolist() == [False, True, True]
    assert len(original) == 4 and original.iloc[-1].vwap == "invalid"


@pytest.mark.parametrize("invalid", [45931, "20260928", "2026-02-30", "31/09/2026", "2026-10-07T10:00:00Z"])
def test_input_rejects_ambiguous_or_invalid_dates(tmp_path, invalid):
    cfg = Config(tmp_path)
    frame = raw_input()
    frame["reference_date"] = [invalid]
    path = tmp_path / "own.xlsx"
    frame.to_excel(path, index=False)
    with pytest.raises(ValueError, match="invalid or empty dates"):
        load_input(path, cfg)


def test_missing_volume_is_allowed_and_legacy_xls_is_explained(tmp_path):
    cfg = Config(tmp_path)
    raw_input().drop(columns="total_volume").to_csv(tmp_path / "own.csv", index=False)
    normalized, _ = load_input("own.csv", cfg)
    assert normalized.volume.isna().all()
    (tmp_path / "legacy.xls").write_bytes(b"not an Excel file")
    with pytest.raises(ValueError, match="legacy .xls.*save it as .xlsx"):
        load_input("legacy.xls", cfg)


def test_eex_cutoff_is_causal_and_age_is_measured_from_reference_day():
    delivery = date(2026, 11, 1)
    book = EexBook(pd.DataFrame([(date(2026, 10, 2), "Month", delivery, 100),
                                (date(2026, 10, 5), "Month", delivery, 120)], columns=REQUIRED))
    monday = date(2026, 10, 5)
    quotes, asof = book.available_quotes(monday, None, -1)
    assert asof == date(2026, 10, 2) and list(quotes.values()) == [100.0]
    assert book.available_quotes(monday, 2, -1) == ({}, None)
    assert book.available_quotes(monday, 0)[1] == monday
    assert book.available_quotes(date(2026, 10, 3), 0) == ({}, None)
    assert book.available_quotes(date(2026, 10, 3), None)[1] == date(2026, 10, 2)


def test_eex_future_day_revision_does_not_leak_into_older_snapshot():
    delivery = date(2026, 10, 1)
    book = EexBook(pd.DataFrame([
        (date(2026, 10, 1), "Day", delivery, 100),
        (date(2026, 10, 2), "Month", delivery, 200),
        (date(2026, 10, 3), "Day", delivery, 300),
    ], columns=REQUIRED))
    quotes, _ = book.quotes(date(2026, 10, 2), 0)
    assert quotes[(delivery, delivery + timedelta(days=1))] == 100


def test_invalid_present_eex_fails_but_missing_source_is_explicitly_unavailable(tmp_path):
    cfg = Config(tmp_path, eex_curves_dir=tmp_path)
    mapping = replace(product(), eex_file="eex.csv")
    assert load_eex_books(cfg, [mapping])[mapping.key] is None
    (tmp_path / "eex.csv").write_text("wrong\nvalue\n", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot read EEX.*missing columns"):
        load_eex_books(cfg, [mapping])


def test_conflicting_eex_quotes_are_rejected():
    with pytest.raises(ValueError, match="Conflicting EEX"):
        EexBook(pd.DataFrame([(date(2026, 9, 30), "Month", date(2026, 10, 1), price)
                             for price in (100, 101)], columns=REQUIRED))


def test_calendar_rollovers_and_dst_delivery_hours():
    before, after = date(2026, 9, 30), date(2026, 10, 1)
    assert resolve_tenor("M+1", before).start == date(2026, 10, 1)
    assert resolve_tenor("M+1", after).start == date(2026, 11, 1)
    assert resolve_tenor("Q+1", before).start == date(2026, 10, 1)
    assert resolve_tenor("Q+1", after).start == date(2027, 1, 1)
    assert resolve_tenor("BOM", before) is None
    assert resolve_tenor("Win+1", after).start == date(2027, 10, 1)
    assert resolve_tenor("Cal+1", after).end == date(2028, 1, 1)
    assert period_hours(date(2026, 3, 29), date(2026, 3, 30), "Base", "Europe/Berlin") == 23
    assert period_hours(date(2026, 10, 25), date(2026, 10, 26), "Base", "Europe/Berlin") == 25


def test_day_weekend_and_peak_calendars():
    friday = date(2026, 10, 2)
    assert resolve_tenor("D+1", friday, "calendar").start == date(2026, 10, 3)
    assert resolve_tenor("D+1", friday, "business").start == date(2026, 10, 5)
    assert resolve_tenor("WE", date(2026, 10, 3)).start == date(2026, 10, 10)
    saturday, monday = date(2026, 10, 3), date(2026, 10, 5)
    assert period_hours(saturday, monday, "Peak", "Europe/Berlin", "Weekend") == 24
    assert period_hours(saturday, monday, "Peak", "Europe/Berlin", "Month") == 0
    assert period_hours(saturday, monday, "Peak7", "Europe/Berlin", "Month") == 24


def test_invalid_calendars_fail_instead_of_silently_becoming_base_or_business():
    with pytest.raises(ValueError, match="profile"):
        hours_fn("invalid", "Europe/Berlin")
    with pytest.raises(ValueError, match="timezone"):
        hours_fn("Peak", "invalid")
    with pytest.raises(ValueError, match="day_convention"):
        resolve_tenor("D+1", date(2026, 10, 2), "typo")


def test_quarter_strip_weights_real_hours_and_residual_uses_known_days():
    hours = hours_fn("Base", "Europe/Berlin")
    october, november, december, january = (date(2026, 10, 1), date(2026, 11, 1),
                                          date(2026, 12, 1), date(2027, 1, 1))
    strip = Pricer({(october, november): 100, (november, december): 200, (december, january): 300}, hours)
    value, method = strip.price(october, january, "Quarter")
    assert method == "strip"
    assert value == pytest.approx((100 * 745 + 200 * 720 + 300 * 744) / (745 + 720 + 744))
    second, third = date(2026, 10, 2), date(2026, 10, 3)
    residual = Pricer({(october, november): 200, (october, second): 100, (second, third): 110}, hours)
    value, method = residual.price(third, november, "BOM")
    assert method == "residual" and value == pytest.approx((200 * 745 - 100 * 24 - 110 * 24) / (745 - 48))


def test_publication_rollback_preserves_originals_after_partial_replace(tmp_path, monkeypatch):
    first, second = tmp_path / "first.csv", tmp_path / "second.csv"
    first.write_text("old first", encoding="utf-8")
    second.write_text("old second", encoding="utf-8")
    original_replace = publication.os.replace
    injected = False

    def fail_once(source, destination):
        nonlocal injected
        if Path(destination) == second and not injected:
            injected = True
            raise OSError("injected publication failure")
        original_replace(source, destination)

    monkeypatch.setattr(publication.os, "replace", fail_once)
    with pytest.raises(OSError, match="previous output files were restored"):
        with publication_lock(tmp_path), CsvBatch() as batch:
            batch.stage(first, pd.DataFrame({"value": [1]}))
            batch.stage(second, pd.DataFrame({"value": [2]}))
    assert first.read_text(encoding="utf-8") == "old first"
    assert second.read_text(encoding="utf-8") == "old second"
    assert not list(tmp_path.glob("*.tmp"))
