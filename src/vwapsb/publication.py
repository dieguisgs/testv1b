"""Stage complete CSV outputs before replacement and serialize writers.

Each file replacement is atomic. The collection of files is not a filesystem
transaction: a power loss during publication can leave different generations.
Ordinary replacement failures trigger rollback; failed recovery copies are
retained and named in the exception instead of being silently discarded.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import tempfile

import pandas as pd

from vwapsb.log import get_logger

@contextmanager
def publication_lock(directory: Path):
    """Exclude concurrent read/merge/write phases using an OS-owned file lock."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".publication.lock"
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError(f"Another writer is publishing to {directory}; retry after it finishes") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class CsvBatch:
    """Serialize CSVs and accompanying text before replacing any destination."""

    def __init__(self):
        self.staged: dict[Path, Path] = {}
        self.backups: dict[Path, Path] = {}
        self.recovery_files: set[Path] = set()

    @staticmethod
    def _temporary(destination: Path, suffix: str) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=suffix,
                                           dir=destination.parent)
        os.close(descriptor)
        return Path(name)

    def stage(self, destination: Path, frame: pd.DataFrame) -> None:
        """Write one temporary CSV without changing its final destination."""
        temporary = self._stage_path(destination)
        frame.to_csv(temporary, index=False, encoding="utf-8-sig")

    def stage_text(self, destination: Path, text: str) -> None:
        """Include an accompanying audit JSON or text file in the same batch."""
        self._stage_path(destination).write_text(text, encoding="utf-8")

    def stage_excel(self, destination: Path, frame: pd.DataFrame) -> None:
        """Stage a mapping worksheet while retaining other workbook sheets.

        Only the first worksheet is the editable mapping table. Existing cell
        styles are retained where possible; columns are written as literal
        values, including strings starting with an equals sign.
        """
        from openpyxl import Workbook, load_workbook

        if destination.suffix.lower() != ".xlsx":
            raise ValueError("Excel mapping destinations must use the .xlsx extension")
        workbook = load_workbook(destination) if destination.exists() else Workbook()
        try:
            sheet = workbook.worksheets[0]
            previous_rows, previous_columns = sheet.max_row, sheet.max_column
            values = [list(frame.columns), *frame.where(pd.notna(frame), None).values.tolist()]
            for row_index, row in enumerate(values, 1):
                for column_index, value in enumerate(row, 1):
                    if hasattr(value, "item"):
                        value = value.item()
                    if value is not None and pd.isna(value):
                        value = None
                    cell = sheet.cell(row_index, column_index, value)
                    if value is None:
                        cell.value = None
                    elif isinstance(value, str):
                        cell.data_type = "s"
            for row_index in range(1, max(previous_rows, len(values)) + 1):
                for column_index in range(1, max(previous_columns, len(frame.columns)) + 1):
                    if row_index > len(values) or column_index > len(frame.columns):
                        sheet.cell(row_index, column_index).value = None
            sheet.freeze_panes = "D2"
            sheet.auto_filter.ref = sheet.dimensions
            workbook.save(self._stage_path(destination))
        finally:
            workbook.close()

    def _stage_path(self, destination: Path) -> Path:
        destination = destination.resolve()
        if destination in self.staged:
            raise ValueError(f"Duplicate output destination in one batch: {destination}")
        temporary = self._temporary(destination, ".stage.tmp")
        self.staged[destination] = temporary
        return temporary

    def _publish(self) -> None:
        for destination in self.staged:
            if destination.exists():
                backup = self._temporary(destination, ".recovery.tmp")
                self.backups[destination] = backup
                shutil.copy2(destination, backup)
        attempted = []
        # Keep every recovery copy protected until publication or a complete
        # rollback finishes. A second interrupt during recovery must not let
        # __exit__ discard copies that may still be needed.
        self.recovery_files.update(self.backups.values())
        try:
            for destination, temporary in self.staged.items():
                # Record before calling the OS: an interrupt may arrive after
                # a successful rename but before control returns to Python.
                attempted.append(destination)
                os.replace(temporary, destination)
        except BaseException as exc:
            failures = []
            failed_recovery_files = set()
            for destination in reversed(attempted):
                try:
                    if self.staged[destination].exists():
                        continue  # Atomic replace did not consume this staged file.
                    if destination in self.backups:
                        os.replace(self.backups[destination], destination)
                    else:
                        destination.unlink(missing_ok=True)
                except BaseException as recovery_error:
                    backup = self.backups.get(destination)
                    if backup is not None:
                        failed_recovery_files.add(backup)
                    failures.append(f"{destination}: {type(recovery_error).__name__}: {recovery_error}; "
                                    f"recovery copy: {backup}")
            self.recovery_files = failed_recovery_files
            if failures:
                raise OSError("Publication failed and rollback was incomplete. Retained recovery files: "
                              + "; ".join(failures)) from exc
            if isinstance(exc, OSError):
                raise OSError(f"Publication failed; previous output files were restored: {exc}") from exc
            raise  # Preserve KeyboardInterrupt/SystemExit after successful rollback.
        else:
            self.recovery_files.clear()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        primary_error = exc
        try:
            if exc_type is None:
                self._publish()
        except BaseException as failure:
            primary_error = failure
            raise
        finally:
            if self.recovery_files and primary_error is not None:
                message = "Retained publication recovery files: " + ", ".join(map(str, sorted(self.recovery_files)))
                primary_error.add_note(message)
                get_logger().warning(message)
            cleanup_interrupt = None
            for temporary in [*self.staged.values(), *self.backups.values()]:
                if temporary not in self.recovery_files:
                    try:
                        temporary.unlink(missing_ok=True)
                    except BaseException as cleanup_error:
                        message = f"Could not remove temporary publication file {temporary}: {cleanup_error}"
                        get_logger().warning(message)
                        if primary_error is not None:
                            primary_error.add_note(message)
                        elif not isinstance(cleanup_error, Exception):
                            # Finish best-effort cleanup, then respect a fresh
                            # interrupt without masking an earlier failure.
                            if cleanup_interrupt is None:
                                cleanup_interrupt = cleanup_error
                            else:
                                cleanup_interrupt.add_note(message)
            if cleanup_interrupt is not None:
                raise cleanup_interrupt
