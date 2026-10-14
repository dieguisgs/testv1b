# Understanding the VWAPSb backtest

The backtest asks a question that can actually be checked: **if this observed own price had been missing, what would the algorithm have estimated using the other information available?** The withheld own price is the reference answer. EEX is supporting information, not the answer the model is required to copy.

This is useful evidence about isolated gaps. It does not prove that unobserved prices are correct, that the market's theoretical fair value equals a traded VWAP, or that a configuration will handle a long missing section equally well. Synthetic examples verify mechanics; real observations are needed to assess real predictive accuracy.

## 1. The examination, explained simply

Suppose one product has M+1 = 100, M+2 = 110 and Q+1 = 120 on a date.

1. Hide M+1. Leave M+2 and Q+1 available. Predict M+1 and compare it with 100.
2. Restore M+1 and hide M+2. Predict M+2 and compare it with 110.
3. Restore M+2 and hide Q+1. Predict Q+1 and compare it with 120.

These are **three separate examinations**, not a progressive deletion of all three prices. The model's same-kind rule still applies: keeping Q+1 available does not make it an eligible local anchor for M+1.

The default examines all eligible cases. Ten contracts on each of twenty dates give 200 cases per candidate if all are distinct physical contracts. Already missing own prices cannot provide a known answer, so they are not scored.

## 2. What is one case?

A case identifies one reference date, exact `(product, region, unit)` and physical delivery contract. Recognized finite own observations in active mapped curves are eligible; delivery periods with no applicable delivery hours are excluded. The case list is built **before candidate volume or anchor-count rules**. A candidate cannot remove a difficult answer from the examination by rejecting it as an anchor.

Equivalent aliases and duplicate rows are hidden together. For example, `M+1` and `M+01` cannot leave a visible copy of the same answer behind. Effective delivery hours form part of physical equivalence: equal boundaries with different actual Peak delivery conventions are not automatically interchangeable.

Duplicate observations are consolidated exactly as own observations are consolidated by the engine:

- If any rows have positive finite volume, use those rows and their volume-weighted mean. Zero, negative or unknown volumes do not contribute to that weighted mean.
- If no row has positive finite volume, use the simple mean of the finite prices.

For prices 110 with volume 1, 130 with volume 3, and 999 with volume 0, the hidden actual is `(110 × 1 + 130 × 3) / 4 = 125`. Without any positive volumes, prices 110 and 130 instead give 120.

Observed recognized contracts outside the configured target list can also be examined. The held-out label is temporarily added to the prediction targets for that case. Consequently, results should be inspected by contract family rather than assumed to describe only your target tenors.

## 3. How much is hidden?

`mask_fraction=1.0` examines every case. `mask_fraction=0.5` reproducibly selects approximately half of the cases, rounded up, with at least one case selected. `seed` controls that selection. Selection happens once and is shared by every candidate.

**The fraction controls how many separate examinations are run. It does not control how many prices disappear simultaneously.** Each examination still hides one physical contract, including its equivalent aliases and duplicates.

The implementation does not simulate a block of consecutive missing months, a whole day without own observations, or persistent missing observations across many days. Those would require different masking experiments. A result for isolated gaps should not be described as validation of those scenarios.

## 4. Dates, history and EEX availability

`start` and `end` are inclusive evaluation dates. Earlier supplied own observations remain available as history, subject to the candidate's historical rules. Future own rows are removed before calling the engine. Only the current case is hidden; its genuine earlier observations may still inform its estimate.

EEX follows the normal production cutoff:

- Offset `0`: latest available publication on or before the reference date.
- Offset `-1`: latest available publication on or before the previous calendar day.
- Offset `-2`: latest available publication on or before two calendar days earlier.

An offset of `-1` on Monday can select Friday. Staleness is measured from the reference date. Historical own/EEX pairs require exact same-date EEX evidence, must precede the prediction date and must be available under its cutoff. The target delivery contract remains defined by the prediction date, even when the EEX publication is older.

Files contain publication dates, not complete intraday release and revision histories. This is a date-based availability scenario, not proof that a revised historical record was visible at a specific historical minute.

## 5. The candidate grid and product configuration

The grid is a dictionary of candidate lists, for example:

```python
PARAMETER_GRID = {
    "local_max_steps": [1, 2],
    "history_enabled": [False, True],
}
```

This produces four combinations. An empty dictionary evaluates the current effective settings once. Only the ten `Model` fields are permitted; maximum grid size is 256 candidates. Invalid values and invalid complete combinations are rejected.

For each product, the base is its effective production configuration: global settings in global mode, or global settings plus its fixed mapping overrides in individual mode. **Grid values override that base for the experiment.** Fields absent from the grid retain their product's base values. Thus one candidate can have different non-varied settings across products when production is configured individually. Detailed scores contain its full effective model in `parameters`; `grid_parameters` describes the shared candidate override.

The experiment does not rewrite the mapping or promote a candidate. This simple workflow does not automatically select separate product winners or run different grid dictionaries per product. You can inspect each product separately or run a deliberately restricted mapping/configuration as another experiment.

## 6. What the metrics mean

Suppose three hidden actual prices are 100, 120 and 140. A candidate predicts 102, 116 and missing.

| Measure | Calculation | Result |
|---|---|---:|
| Cases | All selected hidden contracts | 3 |
| Predicted | Finite predictions | 2 |
| Missing | Cases without a prediction | 1 |
| Coverage | 2 / 3 | 66.7% |
| Errors | prediction minus actual | +2, -4 |
| MAE | (2 + 4) / 2 | 3 |
| RMSE | square root of ((4 + 16) / 2) | 3.162 |
| Bias | (2 - 4) / 2 | -1 |

MAE is an average absolute price error in the price's unit. RMSE gives more importance to large errors. Positive bias means overestimation on average; negative bias means underestimation. If no prediction is available, coverage is zero and error metrics are blank, not zero.

`summary.csv` groups by candidate and unit. EUR/MWh and GBP/MWh are never averaged together. Within one unit, cases have equal weight, so a more frequently observed product contributes more cases. `by_curve.csv` additionally separates exact product identity and contract family; use it to see whether a global aggregate hides a poorly served product or tenor family.

## 7. How to compare candidates honestly

There is **no automatic winner, coverage penalty or combined ranking score**. Lower MAE with lower coverage is not automatically better: a candidate may have avoided its hardest cases.

Consider another candidate that predicts all three example prices as 101, 119 and 160. Its coverage is 100% and its MAE is 7.333. On the first two shared cases, however, its MAE is 1, better than the first candidate's 3. The extra prediction has an error of 20. You must decide whether that additional estimate is useful for your purpose; a single average does not settle that trade-off.

Use `scores.csv` to compare common predicted cases when coverage differs, while retaining the coverage report for the full fixed examination sample. Common-case matching is a manual analysis in this version, not a hidden change to the reported metrics. Inspect errors by product/family, larger errors, missing outcomes and the complete curve's shape. Consistency of overlapping contracts is a separate diagnostic; it is not part of MAE.

## 8. Exploration dates and final evaluation

This version does **not** automatically split calibration and holdout dates or select a winner. The dates supplied are the dates measured.

A practical protocol is to explore grids on an earlier interval, choose and freeze settings, then run those fixed settings on a later untouched interval. Earlier observations can still provide the historical warmup that would exist operationally. If you change parameters after looking at the later results, those dates have become part of your research; reserve another untouched interval for a final assessment.

## 9. Files and notebooks

`notebooks/backtest.ipynb` runs the grid and saves `scores.csv`, `summary.csv` and `by_curve.csv`. Its editable settings contain the dates, grid dictionary, case fraction and seed. `notebooks/grid.example.json` is a small external grid example.

Detailed scores include the engine's normal trace plus `candidate_id`, `grid_parameters`, effective `parameters`, hidden `actual`, signed `error`, `absolute_error`, `squared_error`, `hidden_rows` and `hidden_aliases`. Missing predictions remain explicit rows. The backtest viewer shows these withheld cases; they are not a complete refilled curve.

`notebooks/inspect_curves.ipynb` reads an ordinary saved refill. It offers exact identity and date selection, all contract families or one family, and range means aligned by fixed physical delivery interval. Both output and EEX means use the same dates with both prices present; aliases count once per day.

## 10. Optional local MLflow

`notebooks/backtest_mlflow.ipynb` adds a local tracking service, using port 5055 by default, and persists each candidate's reports and configuration. Install the notebook and experiment extras. The service binds to 127.0.0.1; the last cell can stop its owned process tree without deleting results.

Each candidate also receives a separate **complete refill** for every calendar date in the requested interval. Originals remain visible there. The saved-run selector lets you inspect either these complete curves or the withheld predictions. Both can be loaded from artifacts without refitting; select the same storage directory to revisit old runs. The artifact loader does not need current own/EEX inputs, although the notebook's normal setup cell loads its configured dataset.

MLflow is a record and comparison interface. It does not certify the objective, automatically choose a winner or make synthetic results representative of real markets. Price-level artifacts are stored locally, so use an appropriate local directory for real data.
