"""Small read-only notebook viewer for saved curves and backtest predictions."""

from __future__ import annotations

from datetime import date
from html import escape
from pathlib import Path

import numpy as np
import pandas as pd


IDENTITY = ["product", "region", "unit"]


def prepare_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Validate saved output without opening inputs or recalculating prices."""
    required = {"date", *IDENTITY, "tenor", "kind", "delivery_start", "delivery_end", "price", "eex_price"}
    if required - set(frame):
        raise ValueError(f"Output lacks columns: {sorted(required - set(frame))}")
    result = frame.copy(deep=True)
    for name in (*IDENTITY, "tenor", "kind"):
        result[name] = result[name].fillna("").astype(str)
    for name in ("date", "delivery_start", "delivery_end"):
        result[name] = pd.to_datetime(result[name], errors="raise").dt.date
    if result[["date", "delivery_start", "delivery_end"]].isna().any().any():
        raise ValueError("Output dates and delivery boundaries must be present")
    for name in ("price", "eex_price", "actual"):
        if name in result:
            result[name] = pd.to_numeric(result[name], errors="coerce")
            result.loc[~np.isfinite(result[name]), name] = np.nan
    if "candidate_id" in result:
        if result.candidate_id.isna().any():
            raise ValueError("Backtest candidate_id must be present on every row")
        result["candidate_id"] = result.candidate_id.astype(str)
    return result.sort_values(["date", *IDENTITY, "delivery_start", "delivery_end", "tenor"]).reset_index(drop=True)


def load_output(path: str | Path) -> pd.DataFrame:
    """Read filled.csv or scores.csv; both use the same price/period columns."""
    return prepare_frame(pd.read_csv(path, keep_default_na=False,
                                    dtype={name: str for name in (*IDENTITY, "tenor", "kind", "candidate_id")}))


def select_rows(frame: pd.DataFrame, key: tuple[str, str, str], *, start: date, end: date,
                kind: str | None = None, candidate_id: str | None = None) -> pd.DataFrame:
    """Filter exact identity, inclusive dates, optional kind and one candidate."""
    if len(key) != 3 or any(not isinstance(value, str) for value in key):
        raise ValueError("Select an exact product, region and unit identity")
    if start > end:
        raise ValueError("The start date cannot be after the end date")
    selected = frame.date.between(start, end)
    for name, value in zip(IDENTITY, key):
        selected &= frame[name].eq(value)
    if kind is not None:
        selected &= frame.kind.eq(kind)
    if "candidate_id" in frame:
        if candidate_id is None:
            raise ValueError("Select a candidate_id before comparing backtest curves")
        selected &= frame.candidate_id.eq(candidate_id)
    return frame.loc[selected].copy()


def range_means(frame: pd.DataFrame) -> pd.DataFrame:
    """Compare means on paired dates for fixed physical delivery intervals.

    Relative labels that roll into the same physical interval are combined.
    Aliases on a date count once. Dates with either comparison price missing
    are excluded from BOTH means; row coverage remains visible separately.
    """
    keys = [*IDENTITY, "kind", "delivery_start", "delivery_end"]
    if "candidate_id" in frame:
        if frame.candidate_id.nunique() > 1:
            raise ValueError("Range means require one selected candidate")
        keys.insert(0, "candidate_id")
    records = []
    for physical, group in frame.groupby(keys, sort=True, dropna=False):
        daily = group.sort_values("tenor").drop_duplicates(["date", *keys])
        paired = daily.loc[daily.price.notna() & daily.eex_price.notna()]
        record = {**dict(zip(keys, physical)), "tenor": "/".join(sorted(set(group.tenor))),
                  "n_dates": daily.date.nunique(), "n_paired_dates": len(paired),
                  "price": paired.price.mean(), "eex_price": paired.eex_price.mean()}
        if "actual" in frame:
            record["actual"] = paired.actual.mean()
        records.append(record)
    return pd.DataFrame(records, columns=[*keys, "tenor", "n_dates", "n_paired_dates", "price", "eex_price",
                                         *(["actual"] if "actual" in frame else [])])


def create_viewer(frame: pd.DataFrame):
    """Return English widgets with single-date and paired range-mean charts."""
    try:
        import ipywidgets as widgets
        import plotly.graph_objects as go
        from IPython.display import display
    except ImportError as exc:
        raise ImportError("Notebook dependencies are missing; install the notebook optional dependencies") from exc
    data = prepare_frame(frame)
    if data.empty:
        raise ValueError("No saved rows to display")
    identities = sorted(set(data[IDENTITY].itertuples(index=False, name=None)))
    curve = widgets.Dropdown(options=[(" | ".join(key), key) for key in identities], description="Curve:",
                             layout=widgets.Layout(width="95%"))
    kinds = widgets.Dropdown(options=[("All contract types", None)], description="Kind:")
    candidates = sorted(data.candidate_id.unique()) if "candidate_id" in data else [None]
    candidate = widgets.Dropdown(options=[(value or "Saved refill", value) for value in candidates],
                                 description="Candidate:", layout=widgets.Layout(width="95%"))
    single_day = widgets.Dropdown(description="Date:")
    range_start = widgets.Dropdown(description="From:")
    range_end = widgets.Dropdown(description="To:")
    daily_out, range_out = widgets.Output(), widgets.Output()
    daily_tab = widgets.VBox([single_day, daily_out])
    range_tab = widgets.VBox([widgets.HBox([range_start, range_end]), range_out])
    tabs = widgets.Tab(children=[daily_tab, range_tab])
    tabs.set_title(0, "Single date")
    tabs.set_title(1, "Range means")
    updating = False

    def chart(rows, title):
        figure = go.Figure()
        rows = rows.sort_values(["delivery_start", "delivery_end", "kind", "tenor"])
        labels = [f"{tenor} ({start})" for tenor, start in zip(rows.tenor, rows.delivery_start)]
        rows = rows.assign(_label=labels)
        comparisons = [("price", "Final / predicted", "#0068b5", "solid"),
                       ("eex_price", "EEX reference", "#777777", "dash")]
        if "actual" in rows:
            comparisons.append(("actual", "Hidden actual", "#188332", "dot"))
        # Different families are separate traces: the connecting line does not
        # imply that a month, day and quarter form one delivery strip.
        for field, label, color, dash in comparisons:
            for index, (_, group) in enumerate(rows.groupby("kind", sort=False)):
                figure.add_trace(go.Scatter(x=group._label, y=group[field], mode="lines+markers",
                                           name=label, legendgroup=field, showlegend=index == 0,
                                           line={"color": color, "dash": dash}, connectgaps=False,
                                           customdata=group[["kind", "delivery_start", "delivery_end"]].astype(str),
                                           hovertemplate="%{x}<br>%{y}<br>%{customdata[0]}<br>[%{customdata[1]}, %{customdata[2]})<extra>%{fullData.name}</extra>"))
        figure.update_layout(title=title, template="plotly_white", height=480,
                             yaxis_title=curve.value[2], xaxis_title="Contract and delivery start",
                             xaxis={"type": "category", "categoryorder": "array", "categoryarray": labels},
                             legend={"orientation": "h", "y": -0.35})
        display(figure)

    def refresh_plots(change=None):
        if updating or single_day.value is None:
            return
        with daily_out:
            daily_out.clear_output(wait=True)
            rows = select_rows(data, curve.value, start=single_day.value, end=single_day.value,
                               kind=kinds.value, candidate_id=candidate.value)
            if rows.empty:
                print("No saved rows for this selection.")
            else:
                chart(rows, f"{' | '.join(curve.value)} - {single_day.value}")
                columns = [name for name in ("tenor", "kind", "price", "eex_price", "actual", "status", "method", "reason") if name in rows]
                display(rows[columns].reset_index(drop=True))
        with range_out:
            range_out.clear_output(wait=True)
            if range_start.value > range_end.value:
                print("Choose a start date on or before the end date.")
                return
            rows = select_rows(data, curve.value, start=range_start.value, end=range_end.value,
                               kind=kinds.value, candidate_id=candidate.value)
            if rows.empty:
                print("No saved rows for this selection.")
            else:
                means = range_means(rows)
                print("Fixed delivery intervals; both means use the same paired dates. Aliases count once per day.")
                chart(means, f"Paired means: {range_start.value} to {range_end.value}")
                display(means.reset_index(drop=True))

    def refresh_options(change=None):
        nonlocal updating
        updating = True
        try:
            selected = pd.Series(True, index=data.index)
            for name, value in zip(IDENTITY, curve.value):
                selected &= data[name].eq(value)
            if "candidate_id" in data:
                selected &= data.candidate_id.eq(candidate.value)
            rows = data.loc[selected]
            days = sorted(rows.date.unique())
            kinds.options = [("All contract types", None), *[(value, value) for value in sorted(rows.kind.unique())]]
            single_day.options = range_start.options = range_end.options = [(str(day), day) for day in days]
            if days:
                single_day.value = days[-1]
                range_start.value, range_end.value = days[0], days[-1]
        finally:
            updating = False
        refresh_plots()

    curve.observe(refresh_options, names="value")
    candidate.observe(refresh_options, names="value")
    for control in (kinds, single_day, range_start, range_end):
        control.observe(refresh_plots, names="value")
    explanation = ("Backtest rows show hidden-observation predictions, not complete refilled curves. "
                   "Error metrics and missing predictions must also be inspected."
                   if "candidate_id" in data else
                   "Only rows saved in the output are displayed. Missing contracts are not invented by the viewer.")
    refresh_options()
    return widgets.VBox([widgets.HTML(escape(explanation)), candidate, curve, kinds, tabs])
