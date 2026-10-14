# VWAPSb

[Español](README.es.md) · [Algorithm](ALGORITHM.md) · [Backtest](BACKTEST.md) · [Output columns](OUTPUT.md)

A simpler, independent alternative to VWAPS for completing **power** price curves. It preserves own observations and uses one method per missing value: today's nearby observations, past own/EEX differences, or an average of EEX publications. It never uses an untransformed EEX quotation as the final fallback.

This version has **10 model settings**. It removes ratio/automatic mode switching, local/history blending, correlation, cross-product estimation, own-price arbitrage and shape adjustment. Month/quarter/year consistency is reported, not enforced. This makes decisions easier to audit; it does not establish that this version predicts better. The examples are synthetic; real-company input has not been available for validation.

## Install on this or another machine

Use Python **3.11 or newer**. From the project directory in PowerShell:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -e ".[dev,notebook,experiment]"
.venv\Scripts\python.exe -m ipykernel install --user --name vwapsb --display-name "VWAPSb"
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe run.py demo
```

If another supported Python version is installed, replace `py -3.11` accordingly. On Linux/macOS use `python3 -m venv .venv` and `.venv/bin/python`. No `.env` file is needed. In VS Code select the **VWAPSb** kernel or this project's `.venv` interpreter. Installing notebook dependencies and selecting an unrelated environment will not work.

For the exact resolved dependency versions shipped with this repository, install with `uv sync --all-extras --frozen` using the included `uv.lock`, then use `uv run python run.py demo` or the `.venv` interpreter. The pip commands above install compatible versions rather than reproducing that lockfile.

The demo generates synthetic inputs under `output/demo/` and fills 21 September–9 October 2026. It includes daily, weekend, weekly, monthly, quarterly, seasonal and annual contracts. The default real-input configuration is untouched. Re-running `demo` regenerates its synthetic files only.

## Connect the real inputs

1. Copy `config.toml` to `config.local.toml` in the project root. Edit the paths there. Relative paths are resolved against **the TOML directory**, independently of the working directory.
2. Set `[paths].vwap_input` to the original CSV/XLSX/XLSM file or a glob containing accumulated historical observations. Set `eex_curves_dir` to the scraper's `output/curves/POWER` directory. No scraper, credentials or market-data subscription is included.
3. Edit `[vwap_columns]` if the input's column names differ. Defaults are `reference_date`, `product`, `region`, `unit`, `tenor2`, `vwap`, `total_volume`. Volume is optional. Dates must be readable; decimal commas are accepted. Thousands separators and currency conversion are not inferred. Legacy `.xls` must first be converted to XLSX.
4. Generate the mapping, review it and enable selected rows:

```powershell
.venv\Scripts\python.exe run.py mapping --config config.local.toml
.venv\Scripts\python.exe run.py check --config config.local.toml --date 2026-10-09
```

The mapping key is **product + region + unit**, matched exactly after trimming. Blank region/unit is a literal blank, not a wildcard. Only rows with `use=fill` **and** a nonempty `eex_file` run. Drafts start `off`. There is no automatic currency conversion or verification that a manually assigned product has the right economic meaning. Review timezone, Base/Peak/Peak7 and units yourself.

CSV or XLSX mappings are supported; XLSX reads the first worksheet. Set `[paths].mapping = "mappings/products.xlsx"` before running `mapping` to create an Excel table. Use `products.example.csv` as a field example. All ten model settings can be columns in that table. `run.configuration_mode="global"` uses TOML settings; `"individual"` applies each row's nonblank overrides. Blank cells inherit the global value. `false` is a value, not a blank. Unknown fields fail validation.

EEX input is the scraper's long-format CSV, with `tradeDate`, `maturityType`, `deliveryStart`, `settlPx`. Supported maturity types: `Day`, `Weekend`, `Week`, `Month`, `Quarter`, `Season`, `Year`. Extra columns are ignored. Do not use a wide-format file. Missing files are logged; malformed existing files abort the run. The project never downloads settlements automatically.

Example paths for another computer:

```toml
[paths]
vwap_input = "D:/MarketData/own/*.xlsx"
mapping = "mappings/products.xlsx"
eex_curves_dir = "D:/MarketData/eex_scraper/output/curves/POWER"
output_dir = "output/company"
```

Keep real inputs, local configuration and output private. They are ignored by Git. Files outside the project are read only.

Migrating from VWAPS: the own/EEX input formats remain compatible, but use the **new** configuration template. An old mapping can reuse its identity, EEX file and delivery-convention columns; remove unsupported old model columns and replace `helper` rows with `off` unless that curve should itself be filled. Old output files and old model settings are not an interchangeable format for this independent project.

## Run daily, historically or for pending data

```powershell
# Today's curve. Own historical input must still be available.
.venv\Scripts\python.exe run.py daily --config config.local.toml

# Specified day, excluding that day's EEX publication.
.venv\Scripts\python.exe run.py daily --config config.local.toml --date 2026-10-09 --eex-offset -1

# Recalculate all calendar reference dates in an inclusive range.
.venv\Scripts\python.exe run.py refill --config config.local.toml --start 2026-09-01 --end 2026-10-09

# Add absent rows and retry missing prices; keep already priced rows.
.venv\Scripts\python.exe run.py catchup --config config.local.toml --start 2026-09-01 --end 2026-10-09
```

`daily` defaults to the machine's local date. `refill` defaults to the input's first/last dates. `catchup` defaults from the input's first date through yesterday. Explicit dates are recommended for scheduled runs. All ranges include weekends. There is no permanent process: schedule the daily command with Task Scheduler/cron if needed.

`offset_days=0` allows today's EEX publication, if present; otherwise the latest earlier publication. `-1` only allows publications on/before yesterday; `-2` on/before two calendar days ago. It is **not a trading-session index** and does not change the delivery dates of M+1, Q+1 etc. Logs and output record the actual publication selected. `max_stale_days=7` rejects snapshots older than seven calendar days relative to the curve date; `0` removes that age limit.

Outputs: `filled.csv` is one canonical row per date/identity/tenor; `enriched.csv` retains original cells and adds `refill_` columns and missing target rows; `consistency.csv` reports complete contract aggregation checks; `run_manifest.json` describes the latest run; `logs/` records execution. Same-scope daily/refill runs replace their rows, not duplicate them. Other dates/identities remain. Catchup refuses to mix changed settings within its requested scope: use refill after changing settings, correcting original prices or changing targets. It does not detect input-file revisions. Multi-file publication handles ordinary failures with rollback; it is not a power-loss transaction.

## Inspect and evaluate

Open `notebooks/inspect_curves.ipynb` for a selectable curve/date plot, an all-tenors view and range averages. Open `notebooks/backtest.ipynb` to edit an explicit parameter grid and date range. Both are English and include path examples for a different computer. Run the demo first to have something to display locally.

Backtest reports can also be generated with:

```powershell
.venv\Scripts\python.exe run.py backtest --config output/demo/config.toml --start 2026-10-01 --end 2026-10-09 --grid notebooks/grid.example.json
```

`notebooks/backtest_mlflow.ipynb` adds a local MLflow service and experiment logging, with editable dates and a parameter grid. The basic backtest notebook remains usable without MLflow. Models are not promoted automatically. Read [BACKTEST.md](BACKTEST.md): coverage and errors must be examined together, and an isolated-gap backtest cannot prove accuracy on every truly unknown price.

## Configuration at a glance

The complete model settings, operational options, examples and decision rules are documented in [ALGORITHM.md](ALGORITHM.md). Start with the defaults, validate mapping and dates, then test a small grid. Defaults are starting values, not calibrated recommendations for your products. In particular, this version uses additive differences everywhere; it does not offer ratio mode.
