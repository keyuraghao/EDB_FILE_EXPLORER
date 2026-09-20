"""Background threads so the UI never blocks on file I/O."""

from __future__ import annotations

import logging
import threading
import traceback
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QThread, Signal

from edb_explorer.core import EdbDatabase

log = logging.getLogger(__name__)


class RecordLoader(QThread):
    """Streams records of one table to the UI in growing batches."""

    chunk_ready = Signal(list, list)  # row indices, row dicts
    finished_ok = Signal(int)  # rows loaded
    failed = Signal(str)

    def __init__(self, db: EdbDatabase, table: str, max_rows: int = 1_000_000, parent: Any = None) -> None:
        super().__init__(parent)
        self.db = db
        self.table = table
        self.max_rows = max_rows
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def run(self) -> None:
        indices: list[int] = []
        rows: list[dict[str, Any]] = []
        batch = 200
        loaded = 0
        try:
            for index, values in self.db.iter_records(self.table, bytes_mode="raw", include_nulls=False):
                if self._cancel.is_set():
                    break
                indices.append(index)
                rows.append(values)
                loaded += 1
                if len(rows) >= batch:
                    self.chunk_ready.emit(indices, rows)
                    indices, rows = [], []
                    batch = min(batch * 2, 5000)
                if loaded >= self.max_rows:
                    break
            if (rows and not self._cancel.is_set()) or rows:
                self.chunk_ready.emit(indices, rows)
            self.finished_ok.emit(loaded)
        except Exception as exc:
            log.error("Loading %s failed:\n%s", self.table, traceback.format_exc())
            self.failed.emit(f"{exc.__class__.__name__}: {exc}")


class FunctionWorker(QThread):
    """Run any callable in a thread.  The callable may accept ``progress`` and ``should_stop`` kwargs."""

    result = Signal(object)
    failed = Signal(str)
    progress = Signal(object)

    def __init__(self, fn: Callable[..., Any], *args: Any, parent: Any = None, **kwargs: Any) -> None:
        super().__init__(parent)
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self._stop = threading.Event()

    def cancel(self) -> None:
        self._stop.set()

    def should_stop(self) -> bool:
        return self._stop.is_set()

    def run(self) -> None:
        try:
            out = self._fn(*self._args, progress=self.progress.emit, should_stop=self.should_stop, **self._kwargs)
            self.result.emit(out)
        except Exception as exc:
            log.error("Worker failed:\n%s", traceback.format_exc())
            self.failed.emit(f"{exc.__class__.__name__}: {exc}")
