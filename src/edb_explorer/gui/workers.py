"""Background threads so the UI never blocks on file I/O."""

from __future__ import annotations

import logging
import threading
import traceback
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QThread, Signal

from edb_explorer.core import EdbDatabase
from edb_explorer.core.rowstore import DiskRowStore, FilterSpec, RowStoreCancelledError, SortSpec

log = logging.getLogger(__name__)


class RecordLoader(QThread):
    """Streams records of one table to the UI in growing batches.

    The first ``memory_rows`` rows go to the model as Python dicts (fast to sort and filter in place).  If
    the table is bigger, everything shown so far is written to a :class:`DiskRowStore`, ``spilled`` hands
    that store to the model, and the remaining rows stream straight into it - the table is always loaded
    completely, whatever its size, and memory stays flat.
    """

    chunk_ready = Signal(list, list)  # row indices, row dicts (memory mode)
    spilled = Signal(object)  # DiskRowStore holding every row emitted so far
    store_grew = Signal(int, list)  # rows in the store, column positions seen for the first time (disk mode)
    finished_ok = Signal(int)  # rows loaded
    failed = Signal(str)

    def __init__(self, db: EdbDatabase, table: str, memory_rows: int = 250_000, parent: Any = None) -> None:
        super().__init__(parent)
        self.db = db
        self.table = table
        self.memory_rows = memory_rows
        self.store: DiskRowStore | None = None
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def run(self) -> None:
        indices: list[int] = []
        rows: list[dict[str, Any]] = []
        # everything handed to the model, kept until we know whether the table fits in memory
        kept_indices: list[int] = []
        kept_rows: list[dict[str, Any]] = []
        batch = 200
        loaded = 0
        store: DiskRowStore | None = None
        try:
            for index, values in self.db.iter_records(self.table, bytes_mode="raw", include_nulls=False):
                if self._cancel.is_set():
                    break
                indices.append(index)
                rows.append(values)
                loaded += 1
                if len(rows) >= batch:
                    if store is None:
                        self.chunk_ready.emit(indices, rows)
                        kept_indices.extend(indices)
                        kept_rows.extend(rows)
                    else:
                        self.store_grew.emit(store.count, sorted(store.append(indices, rows)))
                    indices, rows = [], []
                    batch = min(batch * 2, 5000)
                if store is None and loaded >= self.memory_rows:
                    if rows:
                        self.chunk_ready.emit(indices, rows)
                        kept_indices.extend(indices)
                        kept_rows.extend(rows)
                        indices, rows = [], []
                    store = self._spill(kept_indices, kept_rows)
                    if store is None:
                        break  # cancelled while spilling: the model keeps what it has
                    kept_indices, kept_rows = [], []
            if rows:
                if store is None:
                    self.chunk_ready.emit(indices, rows)
                else:
                    self.store_grew.emit(store.count, sorted(store.append(indices, rows)))
            self.finished_ok.emit(loaded)
        except Exception as exc:
            log.error("Loading %s failed:\n%s", self.table, traceback.format_exc())
            self.failed.emit(f"{exc.__class__.__name__}: {exc}")

    def _spill(self, indices: list[int], rows: list[dict[str, Any]]) -> DiskRowStore | None:
        """Write the rows shown so far to a new store and hand it to the model."""
        store = DiskRowStore(self.db.table(self.table).columns)
        try:
            for i in range(0, len(rows), 5000):
                if self._cancel.is_set():
                    store.close()
                    return None
                store.append(indices[i : i + 5000], rows[i : i + 5000])
        except BaseException:
            store.close()
            raise
        self.store = store
        self.spilled.emit(store)
        return store


class ViewBuilder(QThread):
    """Builds one filtered/sorted view of a :class:`DiskRowStore` off the GUI thread."""

    done = Signal(int)  # view id
    failed = Signal(str)

    def __init__(
        self, store: DiskRowStore, filter_spec: FilterSpec | None, sort_spec: SortSpec | None, parent: Any = None
    ) -> None:
        super().__init__(parent)
        self.store = store
        self.filter_spec = filter_spec
        self.sort_spec = sort_spec
        self._stop = threading.Event()

    def cancel(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            vid = self.store.build_view(self.filter_spec, self.sort_spec, self._stop.is_set)
        except RowStoreCancelledError:
            return
        except Exception as exc:
            if self.store.closed:
                return
            log.error("View build failed:\n%s", traceback.format_exc())
            self.failed.emit(f"{exc.__class__.__name__}: {exc}")
            return
        if self._stop.is_set():
            self.store.drop_view(vid)
        else:
            self.done.emit(vid)


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
