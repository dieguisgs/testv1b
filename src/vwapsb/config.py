"""Strict configuration for a small, auditable additive curve-filling model."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import hashlib
import json
import math
import os
from pathlib import Path
import tomllib
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from vwapsb.tenors import parse_tenor

if TYPE_CHECKING:
    from vwapsb.mapping import ProductMap


@dataclass(frozen=True)
class Model:
    """The ten model controls; constructors reject invalid or ambiguous values."""

    local_enabled: bool = True
    history_enabled: bool = True
    local_max_steps: int = 2
    min_volume: float = 0.0
    history_window_days: int = 30
    history_min_days: int = 3
    eex_window: int = 5
    eex_average: str = "ewma"
    eex_halflife: float = 2.0
    local_min_anchors: int = 1

    def __post_init__(self) -> None:
        for name in ("local_enabled", "history_enabled"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"model.{name} must be true or false")
        for name, minimum in (("local_max_steps", 0), ("history_window_days", 1),
                              ("history_min_days", 1), ("eex_window", 2), ("local_min_anchors", 1)):
            _integer(f"model.{name}", getattr(self, name), minimum=minimum)
        for name, minimum, inclusive in (("min_volume", 0.0, True), ("eex_halflife", 0.0, False)):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < minimum or not inclusive and value == minimum):
                raise ValueError(f"model.{name} must be a finite number {'>=' if inclusive else '>'} {minimum}")
        if self.eex_average not in ("simple", "ewma"):
            raise ValueError("model.eex_average must be simple or ewma")
        if self.history_min_days > self.history_window_days:
            raise ValueError("model.history_min_days cannot exceed model.history_window_days")


MODEL_FIELDS = tuple(Model.__dataclass_fields__)
MODEL_PARAMETER_FIELDS = MODEL_FIELDS
BOOL_FIELDS = frozenset(("local_enabled", "history_enabled"))
INTEGER_FIELDS = frozenset(("local_max_steps", "history_window_days", "history_min_days",
                            "eex_window", "local_min_anchors"))


def _integer(name: str, value: Any, *, minimum: int | None = None, maximum: int | None = None) -> None:
    if (isinstance(value, bool) or not isinstance(value, int)
            or minimum is not None and value < minimum or maximum is not None and value > maximum):
        constraint = f" >= {minimum}" if minimum is not None else ""
        constraint += f" <= {maximum}" if maximum is not None else ""
        raise ValueError(f"{name} must be an integer{constraint}")


def default_targets() -> list[str]:
    """Return a new complete default target list, independent of other configs."""
    return [*[f"D+{n}" for n in range(1, 4)], "WE", *[f"WE+{n}" for n in range(1, 4)],
            "BOW", *[f"W+{n}" for n in range(1, 5)], "BOM", *[f"M+{n}" for n in range(1, 11)],
            *[f"Q+{n}" for n in range(1, 9)], *[f"Sum+{n}" for n in range(1, 4)],
            *[f"Win+{n}" for n in range(1, 4)], *[f"Cal+{n}" for n in range(1, 4)]]


def default_columns() -> dict[str, str]:
    return {"reference_date": "reference_date", "product": "product", "region": "region",
            "unit": "unit", "tenor": "tenor2", "vwap": "vwap", "volume": "total_volume"}


def validate_timezone(value: str, label: str = "timezone") -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty timezone name")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"{label}: unknown timezone {value!r}; install tzdata if timezone data is unavailable") from exc


@dataclass
class Config:
    """Execution settings; relative file paths are rooted at the TOML directory."""

    root: Path
    vwap_input: str = "data/vwaps.xlsx"
    mapping_file: Path = Path("mappings/products.csv")
    eex_curves_dir: Path = Path("../eex_scraper/output/curves/POWER")
    output_dir: Path = Path("output")
    configuration_mode: str = "global"
    model: Model = field(default_factory=Model)
    targets: list[str] = field(default_factory=default_targets)
    eex_offset_days: int = 0
    max_stale_days: int = 7
    day_convention: str = "calendar"
    weekend_offset: int = 0
    vwap_columns: dict[str, str] = field(default_factory=default_columns)
    timezones: dict[str, str] = field(default_factory=lambda: {"default": "Europe/Berlin"})

    def __post_init__(self) -> None:
        self.root = Path(os.path.expandvars(os.path.expanduser(str(self.root)))).resolve()
        for name in ("mapping_file", "eex_curves_dir", "output_dir"):
            value = getattr(self, name)
            if not isinstance(value, (str, Path)) or not str(value).strip():
                raise ValueError(f"paths.{name} must be a nonempty path")
            setattr(self, name, self.resolve(value))
        if not isinstance(self.vwap_input, str) or not self.vwap_input.strip():
            raise ValueError("paths.vwap_input must be a nonempty path or glob")
        if self.configuration_mode not in ("global", "individual"):
            raise ValueError("run.configuration_mode must be global or individual")
        if not isinstance(self.model, Model):
            raise ValueError("model must be a validated Model instance")
        if not isinstance(self.targets, list) or not self.targets:
            raise ValueError("targets.tenors must be a nonempty list of supported tenor strings")
        for label in self.targets:
            parsed = parse_tenor(label) if isinstance(label, str) else None
            if parsed is None or parsed[0] in ("Sum", "Win") and parsed[1] < 1:
                raise ValueError(f"targets.tenors contains an unsupported tenor: {label!r}")
        self.targets = list(dict.fromkeys(self.targets))
        _integer("eex.offset_days", self.eex_offset_days, maximum=0)
        _integer("eex.max_stale_days", self.max_stale_days, minimum=0)
        _integer("conventions.weekend_offset", self.weekend_offset)
        if self.day_convention not in ("calendar", "business"):
            raise ValueError("conventions.day must be calendar or business")
        if (not isinstance(self.vwap_columns, dict)
                or set(self.vwap_columns) - set(default_columns())
                or any(not isinstance(value, str) or not value.strip() for value in self.vwap_columns.values())):
            raise ValueError("vwap_columns must contain supported field names and nonempty column aliases")
        self.vwap_columns = {**default_columns(), **self.vwap_columns}
        if len(set(self.vwap_columns.values())) != len(self.vwap_columns):
            raise ValueError("vwap_columns aliases must be distinct")
        if not isinstance(self.timezones, dict):
            raise ValueError("timezones must be a table of timezone names")
        self.timezones = dict(self.timezones)
        for area, timezone in self.timezones.items():
            validate_timezone(timezone, f"timezones.{area}")

    def resolve(self, path: str | Path) -> Path:
        expanded = Path(os.path.expandvars(os.path.expanduser(str(path))))
        return expanded if expanded.is_absolute() else self.root / expanded

    def tz(self, area: str) -> str:
        return self.timezones.get(area, self.timezones.get("default", "Europe/Berlin"))


def model_from_overrides(model: Model, overrides: dict[str, Any]) -> Model:
    """Apply typed fixed values, rejecting unknown keys and implicit coercion."""
    if not isinstance(model, Model) or not isinstance(overrides, dict):
        raise ValueError("Model overrides require a Model and a dictionary")
    unknown = sorted(set(overrides) - set(MODEL_FIELDS))
    if unknown:
        raise ValueError(f"Unknown model parameters: {unknown}")
    return replace(model, **overrides)


def effective_model(cfg: Config, mapping: ProductMap) -> Model:
    """Use the global model, or its individual row's nonempty fixed overrides."""
    return (model_from_overrides(cfg.model, mapping.parameter_overrides)
            if cfg.configuration_mode == "individual" else cfg.model)


def configuration_id(cfg: Config, mapping: ProductMap) -> str:
    """Fingerprint calculation settings without machine paths or input contents.

    The mapping's relative EEX assignment identifies the intended reference.
    Local root, input/output paths and the mapping filename are excluded so a
    copied project preserves the same fingerprint. Preserve data/code versions
    separately; this ID identifies settings, not a dataset snapshot.
    """
    model = asdict(effective_model(cfg, mapping))
    model["min_volume"] = float(model["min_volume"])
    model["eex_halflife"] = float(model["eex_halflife"])
    document = {"schema_version": 1, "model": model, "targets": cfg.targets,
                "identity": list(mapping.key), "mapping": {
                    name: getattr(mapping, name) for name in ("use", "area", "profile", "hours", "timezone")},
                "eex_file": str(mapping.eex_file).replace("\\", "/"),
                "eex_offset_days": cfg.eex_offset_days, "max_stale_days": cfg.max_stale_days,
                "day_convention": cfg.day_convention, "weekend_offset": cfg.weekend_offset}
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_config(path: str | Path) -> Config:
    """Load known TOML sections only; errors identify the incorrect field."""
    path = Path(path).resolve()
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    sections = {
        "paths": {"vwap_input", "mapping", "eex_curves_dir", "output_dir"},
        "run": {"configuration_mode"}, "model": set(MODEL_FIELDS),
        "targets": {"tenors"}, "eex": {"offset_days", "max_stale_days"},
        "conventions": {"day", "weekend_offset"}, "vwap_columns": set(default_columns()),
        "timezones": None,
    }
    unknown = sorted(set(raw) - set(sections))
    if unknown:
        raise ValueError(f"Unknown configuration sections: {unknown}")
    for name, values in raw.items():
        if not isinstance(values, dict):
            raise ValueError(f"{name} must be a TOML table")
        if sections[name] is not None:
            unknown = sorted(set(values) - sections[name])
            if unknown:
                raise ValueError(f"Unknown {name} settings: {unknown}")
    paths = raw.get("paths", {})
    for name, value in paths.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"paths.{name} must be a nonempty path string")
    eex, run, conventions = raw.get("eex", {}), raw.get("run", {}), raw.get("conventions", {})
    return Config(
        root=path.parent,
        vwap_input=paths.get("vwap_input", "data/vwaps.xlsx"),
        mapping_file=Path(paths.get("mapping", "mappings/products.csv")),
        eex_curves_dir=Path(paths.get("eex_curves_dir", "../eex_scraper/output/curves/POWER")),
        output_dir=Path(paths.get("output_dir", "output")),
        configuration_mode=run.get("configuration_mode", "global"),
        model=model_from_overrides(Model(), raw.get("model", {})),
        targets=raw.get("targets", {}).get("tenors", default_targets()),
        eex_offset_days=eex.get("offset_days", 0), max_stale_days=eex.get("max_stale_days", 7),
        day_convention=conventions.get("day", "calendar"), weekend_offset=conventions.get("weekend_offset", 0),
        vwap_columns=raw.get("vwap_columns", {}), timezones=raw.get("timezones", {"default": "Europe/Berlin"}),
    )
