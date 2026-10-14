"""Optional local MLflow tracking and saved candidate-curve inspection.

MLflow records experiments; it does not select a winner or change production
settings. Full refill curves retain originals and are saved separately from
the held-out predictions used to measure accuracy.
"""

from __future__ import annotations

import atexit
from dataclasses import asdict, dataclass, replace
from datetime import timedelta
import hashlib
from html import escape
import json
import math
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from urllib.parse import urlparse
from urllib.request import urlopen

import pandas as pd

from vwapsb.backtest import configured_candidates, run_backtest
from vwapsb.config import effective_model, model_from_overrides
from vwapsb.engine import fill_curves
from vwapsb.viewer import create_viewer, load_output


_SERVERS = {}


def _local_uri(uri: str) -> bool:
    parsed = urlparse(uri)
    return (parsed.scheme in ("http", "https") and parsed.hostname in ("127.0.0.1", "localhost", "::1")
            or parsed.scheme in ("sqlite", "file") and parsed.netloc in ("", "localhost"))


def _client(uri):
    if not isinstance(uri, str) or not _local_uri(uri):
        raise ValueError("Use a local SQLite/file store or loopback MLflow server")
    try:
        from mlflow import MlflowClient
    except ImportError as exc:
        raise ImportError('Install MLflow support with: pip install -e ".[notebook,experiment]"') from exc
    return MlflowClient(tracking_uri=uri)


def _windows_job(process):
    """Kill the complete service tree when its owning Python process exits."""
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                    ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                    ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                    ("scheduling", wintypes.DWORD)]

    class Counters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in
                    ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

    class Extended(ctypes.Structure):
        _fields_ = [("basic", Basic), ("io", Counters), ("process_memory", ctypes.c_size_t),
                    ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = Extended()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
    if (not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
            or not kernel.AssignProcessToJobObject(job, wintypes.HANDLE(int(process._handle)))):
        error = ctypes.WinError(ctypes.get_last_error())
        kernel.CloseHandle(job)
        raise error
    return lambda: kernel.CloseHandle(job)


@dataclass
class LocalMlflowServer:
    url: str
    storage_dir: Path
    log_path: Path
    process: subprocess.Popen
    _close_job: object = None
    _log_handle: object = None

    def stop(self):
        """Stop only this owned service and its workers; keep stored results."""
        if self._close_job is not None:
            self._close_job()
            self._close_job = None
        elif self.process.poll() is None:
            if os.name != "nt":
                os.killpg(self.process.pid, signal.SIGTERM)
            else:
                self.process.terminate()
        try:
            self.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                os.killpg(self.process.pid, signal.SIGKILL)
            else:
                self.process.kill()
            self.process.wait(timeout=5)
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
        for key, server in list(_SERVERS.items()):
            if server is self:
                del _SERVERS[key]


def start_mlflow_server(storage_dir, *, port=5000, timeout=45) -> LocalMlflowServer:
    """Start a hidden, owned loopback service or reuse this kernel's service."""
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError("port must be an integer from 1024 to 65535")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 60:
        raise ValueError("timeout must be greater than 0 and at most 60 seconds")
    root = Path(storage_dir).expanduser().resolve()
    key = (str(root), port)
    existing = _SERVERS.get(key)
    if existing is not None and existing.process.poll() is None:
        return existing
    with socket.socket() as connection:
        connection.settimeout(0.3)
        if connection.connect_ex(("127.0.0.1", port)) == 0:
            raise ValueError(f"Port {port} is occupied by another service; choose another port")
    root.mkdir(parents=True, exist_ok=True)
    _client("sqlite:///" + (root / "tracking.sqlite").as_posix())
    log_path = root / "server.log"
    log_handle = log_path.open("ab")
    command = [sys.executable, "-m", "mlflow", "server", "--host", "127.0.0.1", "--port", str(port),
               "--backend-store-uri", "sqlite:///" + (root / "tracking.sqlite").as_posix(),
               "--serve-artifacts", "--artifacts-destination", str(root / "artifacts"), "--workers", "1"]
    process = None
    server = None
    try:
        process = subprocess.Popen(command, cwd=root, stdout=log_handle, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                   start_new_session=os.name != "nt")
        server = LocalMlflowServer(f"http://127.0.0.1:{port}", root, log_path, process,
                                   _log_handle=log_handle)
        if os.name == "nt":
            server._close_job = _windows_job(process)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"MLflow exited during startup. See {log_path}")
            try:
                with urlopen(server.url + "/health", timeout=0.5) as response:
                    if response.status == 200:
                        _SERVERS[key] = server
                        atexit.register(server.stop)
                        return server
            except (OSError, TimeoutError):
                pass
            time.sleep(0.15)
        raise TimeoutError(f"MLflow did not become ready within {timeout} seconds. See {log_path}")
    except BaseException:
        if server is not None:
            server.stop()
        else:
            log_handle.close()
        raise


def _json(path: Path, content):
    path.write_text(json.dumps(content, ensure_ascii=False, sort_keys=True, indent=2, default=str,
                               allow_nan=False) + "\n", encoding="utf-8")


def _terminate(client, run_id, status):
    """Avoid SDK emoji URLs that fail in Windows cp1252 notebook subprocesses."""
    name = "MLFLOW_SUPPRESS_PRINTING_URL_TO_STDOUT"
    previous = os.environ.get(name)
    os.environ[name] = "true"
    try:
        client.set_terminated(run_id, status)
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


@dataclass
class TrackedBacktest:
    run_id: str
    run_url: str
    reports: dict[str, pd.DataFrame]
    candidate_runs: dict[str, str]
    output_dir: Path


def run_tracked_backtest(observations, books, mappings, cfg, start, end, grid=None,
                         mask_fraction=1.0, seed=42, *, tracking_uri, experiment_name="vwapsb",
                         output_dir=None) -> TrackedBacktest:
    """Log honest coverage/errors and complete curves for every candidate.

    Full curves are an additional refill with originals visible. They are not
    the predictions used in the error metrics. This workflow writes local
    price artifacts; choose a local storage directory suitable for your data.
    """
    candidates = configured_candidates(cfg, mappings, grid)
    client = _client(tracking_uri)
    if not isinstance(experiment_name, str) or not experiment_name.strip():
        raise ValueError("experiment_name must be a nonempty string")
    root = Path(output_dir or cfg.output_dir / "backtest_mlflow").resolve()
    root.mkdir(parents=True, exist_ok=True)
    experiment = client.get_experiment_by_name(experiment_name)
    if experiment is None:
        artifact = (root / "artifacts" / hashlib.sha256(experiment_name.encode()).hexdigest()[:16]).as_uri()
        experiment_id = client.create_experiment(experiment_name, artifact_location=artifact
                                                 if urlparse(tracking_uri).scheme in ("sqlite", "file") else None)
    else:
        experiment_id = experiment.experiment_id
    parent = client.create_run(experiment_id, tags={"mlflow.runName": f"VWAPSb {start} to {end}",
                                                   "vwapsb.kind": "backtest", "vwapsb.selection": "none"})
    parent_id = parent.info.run_id
    folder = root / parent_id
    folder.mkdir()
    open_runs = {parent_id}
    child_ids = {}

    def artifact(run_id, path, group="audit"):
        client.log_artifact(run_id, str(path), artifact_path=group)

    try:
        client.log_param(parent_id, "start", str(start))
        client.log_param(parent_id, "end", str(end))
        client.log_param(parent_id, "eex_offset_days", cfg.eex_offset_days)
        client.log_param(parent_id, "mask_fraction", mask_fraction)
        client.log_param(parent_id, "seed", seed)
        _json(folder / "configuration.json", {"config": asdict(cfg), "mapping": [asdict(m) for m in mappings],
                                              "grid": grid or {}, "automatic_selection": False})
        artifact(parent_id, folder / "configuration.json")
        reports = run_backtest(observations, books, mappings, cfg, start, end, grid, mask_fraction, seed)
        for name, frame in reports.items():
            path = folder / f"{name}.csv"
            frame.to_csv(path, index=False, encoding="utf-8-sig")
            artifact(parent_id, path, "reports")
        dates = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
        for number, overrides in enumerate(candidates, 1):
            candidate_id = f"candidate_{number:03d}"
            child = client.create_run(experiment_id, tags={"mlflow.parentRunId": parent_id,
                                                          "mlflow.runName": candidate_id,
                                                          "vwapsb.candidate_id": candidate_id,
                                                          "vwapsb.selection": "none"})
            run_id = child.info.run_id
            open_runs.add(run_id)
            child_ids[candidate_id] = run_id
            directory = folder / candidate_id
            directory.mkdir()
            for name, value in overrides.items():
                client.log_param(run_id, name, value)
            client.log_param(run_id, "eex_offset_days", cfg.eex_offset_days)
            trial_mappings = [replace(mapping, parameter_overrides=asdict(
                model_from_overrides(effective_model(cfg, mapping), overrides))) if mapping.active else mapping
                for mapping in mappings]
            _json(directory / "effective_models.json", [{"product": mapping.product, "region": mapping.region,
                "unit": mapping.unit, "model": mapping.parameter_overrides}
                for mapping in trial_mappings if mapping.active])
            artifact(run_id, directory / "effective_models.json")
            for name, frame in reports.items():
                part = frame.loc[frame.candidate_id.eq(candidate_id)]
                path = directory / f"{name}.csv"
                part.to_csv(path, index=False, encoding="utf-8-sig")
                artifact(run_id, path, "reports")
            metrics = reports["summary"].loc[reports["summary"].candidate_id.eq(candidate_id)]
            unit_keys = {}
            for row in metrics.to_dict("records"):
                unit_key = hashlib.sha256(row["unit"].encode()).hexdigest()[:12]
                unit_keys[unit_key] = row["unit"]
                for name in ("n_cases", "n_predicted", "n_missing", "coverage", "mae", "rmse", "bias"):
                    value = float(row[name])
                    if math.isfinite(value):
                        client.log_metric(run_id, f"unit_{unit_key}.{name}", value)
            _json(directory / "metric_units.json", unit_keys)
            artifact(run_id, directory / "metric_units.json")
            full = fill_curves(observations, books, trial_mappings,
                               replace(cfg, configuration_mode="individual"), dates)
            full_path = directory / "filled.csv"
            full.to_csv(full_path, index=False, encoding="utf-8-sig")
            artifact(run_id, full_path, "curves")
            _terminate(client, run_id, "FINISHED")
            open_runs.remove(run_id)
        _terminate(client, parent_id, "FINISHED")
        open_runs.remove(parent_id)
    except BaseException as exc:
        for run_id in open_runs:
            try:
                _terminate(client, run_id, "KILLED" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "FAILED")
            except Exception as cleanup:
                exc.add_note(f"Could not terminate MLflow run {run_id}: {cleanup}")
        raise
    base = tracking_uri.rstrip("/") if urlparse(tracking_uri).scheme in ("http", "https") else "http://127.0.0.1:5000"
    return TrackedBacktest(parent_id, f"{base}/#/experiments/{experiment_id}/runs/{parent_id}", reports, child_ids, folder)


def list_candidate_runs(tracking_uri, experiment_name="vwapsb") -> pd.DataFrame:
    """List up to 1000 recent saved candidates without recreating any output."""
    client = _client(tracking_uri)
    experiment = client.get_experiment_by_name(experiment_name)
    columns = ["run_id", "candidate_id", "parent_id", "status", "started", "parameters"]
    if experiment is None:
        return pd.DataFrame(columns=columns)
    runs = client.search_runs([experiment.experiment_id], order_by=["attributes.start_time DESC"], max_results=1000)
    return pd.DataFrame([{"run_id": run.info.run_id, "candidate_id": run.data.tags["vwapsb.candidate_id"],
                          "parent_id": run.data.tags.get("mlflow.parentRunId", ""), "status": run.info.status,
                          "started": pd.to_datetime(run.info.start_time, unit="ms", utc=True),
                          "parameters": run.data.params}
                         for run in runs if "vwapsb.candidate_id" in run.data.tags], columns=columns)


def load_candidate_curves(tracking_uri, run_id, *, cache_dir, withheld=False):
    """Load saved complete curves or withheld predictions, without refitting."""
    client = _client(tracking_uri)
    run = client.get_run(run_id)
    if "vwapsb.candidate_id" not in run.data.tags:
        raise ValueError("Select a saved VWAPSb candidate run")
    uri = urlparse(run.info.artifact_uri)
    if not ((uri.scheme == "file" and uri.netloc in ("", "localhost"))
            or (uri.scheme == "mlflow-artifacts" and uri.hostname in (None, "localhost", "127.0.0.1", "::1"))):
        raise ValueError("Only local artifact stores are supported")
    root = Path(cache_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="vwapsb-view-", dir=root) as temporary:
        path = Path(client.download_artifacts(run_id, "reports/scores.csv" if withheld else "curves/filled.csv",
                                             dst_path=temporary)).resolve()
        if not path.is_relative_to(Path(temporary).resolve()):
            raise ValueError("Artifact path escaped its temporary download directory")
        return load_output(path)


def create_experiment_viewer(tracking_uri, *, experiment_name="vwapsb", cache_dir):
    """Select saved candidate runs and inspect full curves or hidden cases."""
    import ipywidgets as widgets
    from IPython.display import display

    catalog = list_candidate_runs(tracking_uri, experiment_name)
    selector = widgets.Dropdown(options=[(f"{row.started} | {row.candidate_id} | {row.status} | {row.run_id[:8]}", row.run_id)
                                         for row in catalog.itertuples()], description="Run:",
                                layout=widgets.Layout(width="95%"))
    mode = widgets.Dropdown(options=[("Complete refill (originals visible)", False),
                                     ("Withheld predictions (accuracy cases)", True)], description="View:",
                            layout=widgets.Layout(width="95%"))
    button = widgets.Button(description="Load saved curves", disabled=catalog.empty)
    details = widgets.HTML()
    output = widgets.Output()
    state = {"viewer": None}

    def close_viewer(widget):
        if widget is not None:
            for child in getattr(widget, "children", ()):
                close_viewer(child)
            widget.close()

    def load(_):
        with output:
            output.clear_output(wait=True)
            try:
                frame = load_candidate_curves(tracking_uri, selector.value, cache_dir=cache_dir, withheld=mode.value)
                close_viewer(state["viewer"])
                state["viewer"] = create_viewer(frame)
                display(state["viewer"])
            except Exception as exc:
                print(f"Cannot load saved candidate: {exc}")

    def clear(_):
        close_viewer(state["viewer"])
        state["viewer"] = None
        output.clear_output(wait=True)
        selected = catalog.loc[catalog.run_id.eq(selector.value)]
        details.value = ("<pre>" + escape(json.dumps(selected.iloc[0]["parameters"], indent=2, sort_keys=True))
                         + "</pre>") if not selected.empty else "No saved candidates in this experiment."

    selector.observe(clear, names="value")
    mode.observe(clear, names="value")
    button.on_click(load)
    clear(None)
    return widgets.VBox([widgets.HTML("Full curves retain originals; use withheld-case reports to evaluate accuracy."),
                         selector, mode, details, button, output])
