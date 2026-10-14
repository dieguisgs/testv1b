"""Optional tracking preserves scores and provides saved complete candidate curves."""

import json
import socket

import pandas as pd
import pytest

from test_backtest import dataset
from vwapsb import mlflow_support as tracking


def test_external_tracking_uri_is_rejected_without_connecting():
    with pytest.raises(ValueError, match="local"):
        tracking._client("https://external.invalid")


def test_foreign_occupied_port_is_not_adopted_or_killed(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        with pytest.raises(ValueError, match="occupied"):
            tracking.start_mlflow_server(tmp_path / "mlflow", port=port)
        assert listener.fileno() >= 0


def test_real_tracking_saves_complete_curves_separately_from_hidden_predictions(dataset, tmp_path, monkeypatch):
    pytest.importorskip("mlflow")
    cfg, mapping, days, own, books = dataset
    uri = "sqlite:///" + (tmp_path / "tracking.sqlite").as_posix()
    result = tracking.run_tracked_backtest(
        own, books, [mapping], cfg, days[0], days[-1], {"local_max_steps": [1, 2]},
        tracking_uri=uri, output_dir=tmp_path / "results")
    assert len(result.candidate_runs) == 2
    assert result.reports["summary"].coverage.eq(1.0).all()
    catalog = tracking.list_candidate_runs(uri)
    assert len(catalog) == 2 and catalog.status.eq("FINISHED").all()
    monkeypatch.setattr(tracking, "fill_curves", lambda *args: pytest.fail("Saved inspection must not recalculate"))
    child = result.candidate_runs["candidate_001"]
    full = tracking.load_candidate_curves(uri, child, cache_dir=tmp_path / "cache")
    hidden = tracking.load_candidate_curves(uri, child, cache_dir=tmp_path / "cache", withheld=True)
    assert full.status.eq("original").all()
    assert hidden.status.eq("estimated").all()
    assert "actual" in hidden and "actual" not in full
    artifact = json.loads((result.output_dir / "candidate_001" / "effective_models.json").read_text())
    assert artifact[0]["model"]["local_max_steps"] == 1
    assert not list((tmp_path / "cache").iterdir())


def test_engine_failure_marks_tracked_parent_failed(dataset, tmp_path, monkeypatch):
    pytest.importorskip("mlflow")
    cfg, mapping, days, own, books = dataset
    uri = "sqlite:///" + (tmp_path / "failed.sqlite").as_posix()

    def fail(*args, **kwargs):
        raise RuntimeError("deliberate calculation failure")

    monkeypatch.setattr(tracking, "run_backtest", fail)
    with pytest.raises(RuntimeError, match="deliberate calculation failure"):
        tracking.run_tracked_backtest(own, books, [mapping], cfg, days[0], days[-1],
                                     tracking_uri=uri, output_dir=tmp_path / "failed")
    client = tracking._client(uri)
    experiment = client.get_experiment_by_name("vwapsb")
    runs = client.search_runs([experiment.experiment_id])
    assert len(runs) == 1 and runs[0].info.status == "FAILED"
