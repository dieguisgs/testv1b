# VWAPSb output dictionary

[Español](OUTPUT.es.md) · [README](README.md)

The production commands write `filled.csv`, `enriched.csv`, `consistency.csv` and `run_manifest.json` under `[paths].output_dir`. These files answer different questions: the canonical curve, the original input with refill information, delivery consistency and the latest execution's settings.

## 1. Canonical curves: `filled.csv`

Each row identifies one reference date, exact `(product, region, unit)` and tenor. Prices use the row's `unit`; dates use `YYYY-MM-DD`. Delivery is the interval **[delivery_start, delivery_end)**: the start is included and the end is excluded.

The engine produces configured target tenors and recognized original tenors available that day for enabled mappings. A relative target that cannot resolve to a delivery period does not produce a row. For example, BOW/BOM can cease to exist at the end of the applicable period. Missing rows therefore cannot be inferred from the count of CSV rows alone.

| Column | Values and interpretation |
| --- | --- |
| `date` | Reference date for which the curve is calculated. This is not necessarily the EEX publication date. |
| `product` | Exact product identifier from the mapping/input. |
| `region` | Exact region identifier; may be empty if that is the configured identity. |
| `unit` | Exact unit identifier, such as `EUR/MWh`. Prices from different units must not be pooled. |
| `tenor` | Relative label, for example `D+1`, `M+1`, `Q+2`, `Cal+1`. Interpret it together with `date` and the delivery dates. |
| `kind` | Resolved family: `Day`, `Weekend`, `Week`, `Month`, `Quarter`, `Season`, `Year`, `BOW` or `BOM`. |
| `delivery_start` | First delivery date, inclusive. |
| `delivery_end` | First date outside delivery, exclusive. |
| `price` | Final finite price, or empty when no price was obtained. Zero and negative values are valid prices. |
| `status` | `original`, `estimated` or `missing`; see the method table below. |
| `method` | `original`, `own_reused`, `local`, `history`, `eex_ewma`, `eex_simple` or `missing`. |
| `eex_price` | Target's EEX reference in the selected publication, if available. It can be an exact contract, a constructed strip or a residual. It is **not** the EEX moving average. |
| `eex_date` | Selected EEX publication date for this curve and reference date. It can be present even when this particular target has no EEX reference. |
| `eex_cutoff` | Latest allowed publication date: `date + eex_offset_days`. An offset of `-1` moves the information cutoff one calendar day back; it does not move delivery. |
| `eex_age_days` | Calendar days between `date` and `eex_date`; empty when there is no selected publication. |
| `adjustment` | Additive own-minus-EEX adjustment used by `local` or `history`, in the same unit as the price. Empty for other methods. |
| `local_count` | Number of eligible LOCAL anchors found if that stage was reached. It can be positive even if too few anchors were found and another method was selected. Zero also occurs when LOCAL was not evaluated. |
| `history_days` | Number of eligible distinct historical own dates found if HISTORY was reached. It can be positive but below `history_min_days`. Zero does not prove that the input contains no history: an earlier stage may have succeeded. |
| `history_start` | Earliest eligible historical date found; empty when none was found or history was not evaluated. It can be populated even when HISTORY ultimately failed its minimum count. |
| `history_end` | Latest eligible historical date found, with the same interpretation. This is not the latest date in the input file. |
| `reason` | Final selection or failure reason, listed below. Earlier failed stages are recorded in `details`. |
| `details` | JSON object containing effective settings, stage decisions and calculation evidence. Parse as JSON rather than splitting on commas. |
| `configuration_id` | Deterministic 64-character SHA-256 identifier of this curve's effective settings and relevant mapping/calendar settings. It is not a hash of input data or a proof that two executions used identical source files. |

### Status, method and final reason

Stages are attempted in this order and stop at the first successful result. There is no blend of LOCAL and HISTORY in VWAPSb.

| `status` | `method` | Meaning | Successful `reason` |
| --- | --- | --- | --- |
| `original` | `original` | A finite own observation exists for this target label. Duplicate observations can be aggregated in the canonical curve. | `observed_original_period` |
| `original` | `own_reused` | An own observation exists under another label for the same delivery interval and delivery hours. This reuses an observed price; it does not extrapolate a nearby contract. | `equivalent_original_delivery_period` |
| `estimated` | `local` | Current target EEX plus the median additive difference of eligible same-family own anchors on the reference date. | `same_kind_local_median` |
| `estimated` | `history` | Current target EEX plus the median of eligible past dates' median own-minus-EEX differences. | `historical_median_of_daily_medians` |
| `estimated` | `eex_simple` | Arithmetic mean over the required EEX publication window, always following the target's fixed delivery interval. | `complete_fixed_contract_eex_window` |
| `estimated` | `eex_ewma` | Normalized exponentially weighted mean over that complete fixed-contract publication window. More recent observations receive more weight. | `complete_fixed_contract_eex_window` |
| `missing` | `missing` | No applicable stage produced a price, or the delivery interval has zero delivery hours. | A failure reason below. |

Final missing reasons are `zero_delivery_hours`, `no_eex_book`, `no_publication_within_cutoff_and_age`, `insufficient_publications`, `missing_contract_in_window` or `degenerate_averaging_weights`. The last one means numerical underflow left fewer than two positive averaging weights; the engine does not silently fall back to one raw EEX price.

For example, target EEX `120` and accepted LOCAL differences `-5`, `3`, `7` give `adjustment=3`, `price=123`, `status=estimated`, `method=local`, `local_count=3`. The `eex_price` remains `120` so the calculation can be checked.

An EEX average can happen to equal the latest settlement when all prices in its window are equal. That numerical equality does not mean raw EEX substitution occurred: the observations and weights in `details` distinguish the calculation.

### Duplicate observations and equivalent labels

If several own observations describe the same physical delivery interval and hours, the canonical own value is the positive-volume-weighted mean when at least one positive volume exists. In that case observations without positive volume do not contribute to that mean. If none has positive volume, the mean is arithmetic. `details.own.observations` keeps the contributing source observations available for inspection.

The engine also avoids calculating different estimates for equivalent target aliases. One deterministic representative is calculated and its result is reused for the other equivalent labels. The copied row keeps its own `tenor`/`kind` and includes `details.equivalent_target` with the representative label, family and rule `same_delivery_interval_and_hours`. This reuse preserves the selected estimation method; it is not the `own_reused` method unless the underlying source is an own observation.

## 2. Reading `details` step by step

Every row includes `settings`, `eex_reference_method` and `stages`. Missing JSON values are `null`; an empty list means no items were recorded.

* `settings.model`: all ten effective model parameters, including inherited defaults and individual overrides. `settings` also includes targets, configuration mode, EEX cutoff/staleness settings, calendar conventions and mapping metadata. This is the settings snapshot for this row.
* `eex_reference_method`: `exact`, `strip`, `residual` or `null`. `strip` combines a complete set of smaller delivery contracts using delivery-hour weights. `residual` derives the uncovered part from a larger contract and known portions. These describe construction of the **EEX reference**, not a separate own-price refill method.
* `stages`: decisions in execution order. Stages after a selected result are absent because they were not evaluated.
* `own`, when applicable: own labels, physical period, price, volume, source observations and aggregation rule.
* `equivalent_target`, when applicable: the representative estimated alias described above.

| Stage | Evidence and possible outcomes |
| --- | --- |
| `original` | `selected`, `equivalent_alias`, `unavailable` or `zero_delivery_hours`. |
| `local` | `required_anchors`, accepted `anchors` and `rejected` anchors. Outcomes: `disabled`, `target_eex_unavailable`, `insufficient_anchors`, `selected`. Accepted anchors include own/EEX prices, EEX construction method, additive adjustment and `distance_steps`. Rejection reasons: `different_kind`, `outside_radius`, `volume_below_minimum_or_unknown`, `missing_eex_reference`. |
| `history` | `window_start`, `required_days`, `target_lead_steps`, `radius_steps`, matching rule and `days`. Each date records its accepted anchors and `daily_median`. Outcomes: `disabled`, `target_eex_unavailable`, `insufficient_days`, `selected`. |
| `eex_average` | Requested publication count, `average`, `halflife_observations`, fixed delivery dates, available `observations` and `missing_contract_dates`. Each available observation includes date, price and EEX construction method; a complete window also includes normalized `weight`. Outcomes: `selected` or an EEX averaging failure reason listed above. |

LOCAL uses calendar distances within the same contract family. HISTORY compares the target's relative delivery lead with each past observation's relative lead; it does not simply follow the text `M+1` as though that always meant the same month. Historical pairs require own and EEX observations on the same past date, strictly before the reference date and no later than the current EEX cutoff. Each eligible past date contributes one daily median, regardless of how many anchors that date contains.

The averaging window contains the most recent `eex_window` publications up to the selected EEX date. It cannot skip a publication merely because the target contract is missing there. The entire window must be present for that fixed delivery interval. EWMA age and half-life are measured in **publication observations**, not calendar days; `weight ∝ 2^(-observation_age / eex_halflife)`.

## 3. Original input plus refill: `enriched.csv`

This is the file to use when the original columns must remain available. It preserves raw input columns and adds the 23 canonical fields with a `refill_` prefix, plus:

| Additional column | Values and meaning |
| --- | --- |
| `refill_generated` | `False` for a retained input row; `True` for an added target row that was absent from the input. It describes row origin, not whether its price is estimated. |
| `refill_input_row` | One-based position in the loaded raw input frame for retained rows; empty for generated rows. This does not include an Excel header row and is not a permanent identifier across different input files/runs. |

Important distinctions:

1. Each finite raw own price is retained individually as `refill_price`, `refill_status=original`, `refill_method=original`, `refill_reason="Original input row preserved"` and `refill_details={"retained_input_row":true}`. This also applies to originals outside processing scope. Other trace fields may be empty when there is no canonical match. Canonical duplicate aggregation does not overwrite these raw observations.
2. A raw row without a finite own price receives its matching canonical result when one exists. Without a canonical match, it has empty `refill_price`, `refill_status=missing`, `refill_method=not_processed`, and reason `No matching canonical output row`. `not_processed` exists only in enriched output; it is not another estimator.
3. A generated row copies identity/date/tenor into the corresponding raw columns and contains the full prefixed result. Its raw price column remains empty. Read **`refill_price`** for the completed price.
4. A generated row can have `refill_status=original` and `refill_method=own_reused` when another own label represents exactly the same physical contract.

Input files that already contain `refill_` columns are rejected. Feed the original input to subsequent runs so estimates cannot become new own observations.

## 4. Delivery relationships: `consistency.csv`

This report checks complete, non-overlapping delivery covers, such as a quarter covered by three months. It **reports differences without changing any price**.

| Column | Meaning |
| --- | --- |
| `date`, `product`, `region`, `unit` | Reference date and exact curve identity. |
| `parent_tenor` | Label of the larger delivery period being checked. |
| `parent_price` | Final price of that larger period. |
| `children_price` | Delivery-hour-weighted mean of the selected smaller periods. |
| `difference` | `parent_price - children_price`, in the row's unit. Positive means the parent is higher; negative means it is lower. |
| `children` | JSON list containing each component's `tenor`, `kind`, delivery dates, `price`, `hours` and normalized `weight`. |

The audit uses the mapping's delivery calendar and timezone, and verifies that the children's delivery hours cover the parent. It excludes missing prices and incompatible delivery calendars. Equivalent aliases are counted once. When several complete covers exist, the choice is deterministic and prefers the fewest components; this is not a report of every possible combination.

If a parent is `100` and its complete weighted child average is `103`, `difference=-3`. Equal values give zero, subject to floating-point precision. If a required child is missing and no alternative complete cover exists, there is **no audit row** for that parent; absence is not evidence of consistency. This audit measures internal agreement, not accuracy against an unknown true price.

## 5. Execution record: `run_manifest.json`

The manifest describes the **latest execution only**, while the CSVs can retain results from earlier executions and other date ranges. Fields include:

* `schema_version`, `project`, `mode`, `created_utc`: format version, project name, command mode and UTC execution timestamp.
* `dates`: reference dates requested in this execution.
* `eex_offset_days`, `max_stale_days`, `configuration_mode`: run settings.
* `rows_written`: new or recalculated canonical rows in this execution; `total_rows`: total canonical rows after publication.
* `curves`: each active curve's exact identity, `configuration_id` and full effective `model`.
* `note`: reminder that retained rows keep their own configuration identifiers.

The configuration identifier includes effective model, target list, identity and relevant EEX/mapping/calendar settings. Paths such as the project root or output directory are excluded so copying the project does not itself change the identifier. It does not hash source contents or code versions. Catchup checks saved configuration identifiers in the requested scope and rejects incompatible settings; use explicit refill to recalculate that scope.

## 6. Backtest result tables

The underlying backtest returns `scores`, `summary` and `by_curve` tables. CLI/notebook output locations are configured by those workflows. MLflow can record these evaluation results; tracking does not change the meaning of their metrics.

Each evaluated case hides the own observations and aliases for one physical contract on one reference date. It then predicts that case using the remaining information. Cases are hidden **one at a time**. `mask_fraction` samples the case list once using `seed`, and the same sampled cases are used for every candidate. It does not remove that percentage of the curve simultaneously.

The score table includes the engine's output fields and these evaluation fields:

| Column | Meaning |
| --- | --- |
| `candidate_id` | Run-local label such as `candidate_001`; not a stable configuration identifier across searches. |
| `grid_parameters` | JSON of the candidate's explicit grid values. |
| `parameters` | JSON of all ten effective model parameters for this particular curve and candidate. |
| `actual` | Aggregated hidden own price, using the same duplicate aggregation rule as the engine. |
| `error` | `price - actual`; positive means overestimation. Empty when the prediction is missing. |
| `absolute_error` | Absolute value of the prediction error. |
| `squared_error` | Squared prediction error, in squared price units. |
| `hidden_rows` | Count of own rows removed for this case, not a count of historical days or grid candidates. |
| `hidden_aliases` | JSON list of hidden labels describing that physical delivery period. |

Summary is grouped by `candidate_id` and `unit`. The more detailed report is grouped by candidate, exact curve identity and `kind`. Both include `grid_parameters` and:

| Metric | Meaning |
| --- | --- |
| `n_cases` | Number of selected hidden contracts evaluated, including failures to predict. |
| `n_predicted` | Cases receiving a finite prediction. |
| `n_missing` | Cases with no prediction: `n_cases - n_predicted`. |
| `coverage` | `n_predicted / n_cases`, between 0 and 1, not a percentage string. |
| `mae` | Mean absolute error over predicted cases, in price units. |
| `rmse` | Root mean squared error over predicted cases, in price units; emphasizes large errors. |
| `bias` | Mean signed error over predicted cases. |

If no cases are predicted, coverage is zero and error metrics are empty, not zero. Compare error **and** coverage: refusing difficult cases can make MAE appear better. Units are kept separate, and every candidate uses the same selected cases. These results test recovery of observed prices; they cannot prove that genuinely unobserved prices have the same difficulty or distribution. A search does not automatically install a winning production configuration.
