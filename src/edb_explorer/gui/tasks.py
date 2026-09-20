"""Background task registry + the 'Tasks' side panel showing a progress bar per running job."""

from __future__ import annotations

import itertools
import time
from typing import Any

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from edb_explorer.gui.icons import icon


class Task(QObject):
    """Handle given to a worker; thread-safe (signals are queued to the GUI thread)."""

    updated = Signal(object)
    finished = Signal(object)

    _ids = itertools.count(1)

    def __init__(self, title: str, cancel: Any = None, detail: str = "") -> None:
        super().__init__()
        self.id = next(self._ids)
        self.title = title
        self.detail = detail
        self.value: int | None = None  # None = indeterminate
        self.maximum: int = 0
        self.done = False
        self.failed = False
        self.started = time.time()
        self._cancel = cancel

    def progress(self, value: int | None = None, maximum: int = 0, detail: str | None = None) -> None:
        self.value = value
        self.maximum = maximum
        if detail is not None:
            self.detail = detail
        self.updated.emit(self)

    def finish(self, detail: str | None = None, failed: bool = False) -> None:
        if self.done:
            return
        self.done = True
        self.failed = failed
        if detail is not None:
            self.detail = detail
        self.finished.emit(self)

    @property
    def cancellable(self) -> bool:
        return self._cancel is not None

    def cancel(self) -> None:
        if self._cancel is not None:
            self._cancel()


class TaskManager(QObject):
    task_added = Signal(object)
    task_finished = Signal(object)
    count_changed = Signal(int)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._tasks: dict[int, Task] = {}

    def start(self, title: str, cancel: Any = None, detail: str = "") -> Task:
        task = Task(title, cancel, detail)
        self._tasks[task.id] = task
        task.finished.connect(self._on_finished)
        self.task_added.emit(task)
        self.count_changed.emit(self.running_count)
        return task

    def _on_finished(self, task: Task) -> None:
        self.task_finished.emit(task)
        self.count_changed.emit(self.running_count)

    @property
    def running_count(self) -> int:
        return sum(1 for t in self._tasks.values() if not t.done)

    def running(self) -> list[Task]:
        return [t for t in self._tasks.values() if not t.done]

    def cancel_all(self) -> None:
        for t in self.running():
            t.cancel()

    def forget(self, task_id: int) -> None:
        self._tasks.pop(task_id, None)


class _TaskRow(QFrame):
    def __init__(self, task: Task, manager: TaskManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.task = task
        self.manager = manager
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(3)
        top = QHBoxLayout()
        self.title = QLabel(task.title)
        self.title.setStyleSheet("font-weight: 600;")
        self.title.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        top.addWidget(self.title, 1)
        self.cancel_btn = QToolButton()
        self.cancel_btn.setIcon(icon("cancel"))
        self.cancel_btn.setToolTip("Cancel")
        self.cancel_btn.setAutoRaise(True)
        self.cancel_btn.setVisible(task.cancellable)
        self.cancel_btn.clicked.connect(task.cancel)
        top.addWidget(self.cancel_btn)
        self.close_btn = QToolButton()
        self.close_btn.setIcon(icon("close"))
        self.close_btn.setToolTip("Dismiss")
        self.close_btn.setAutoRaise(True)
        self.close_btn.hide()
        self.close_btn.clicked.connect(self._dismiss)
        top.addWidget(self.close_btn)
        layout.addLayout(top)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setTextVisible(True)
        self.bar.setFixedHeight(14)
        layout.addWidget(self.bar)
        self.detail = QLabel(task.detail)
        self.detail.setObjectName("dim")
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)
        task.updated.connect(self.refresh)
        task.finished.connect(self.on_finished)
        self.refresh(task)

    def refresh(self, task: Task) -> None:
        if task.value is None:
            self.bar.setRange(0, 0)
            self.bar.setFormat("")
        else:
            self.bar.setRange(0, max(1, task.maximum))
            self.bar.setValue(min(task.value, max(1, task.maximum)))
            self.bar.setFormat(f"{task.value:,} / {task.maximum:,}" if task.maximum else f"{task.value:,}")
        self.detail.setText(task.detail)

    def on_finished(self, task: Task) -> None:
        self.bar.setRange(0, 1)
        self.bar.setValue(1)
        self.bar.setFormat("failed" if task.failed else f"done in {time.time() - task.started:.1f}s")
        if task.failed:
            self.bar.setStyleSheet("QProgressBar::chunk { background: #c0392b; }")
        self.detail.setText(task.detail)
        self.cancel_btn.hide()
        self.close_btn.show()
        if not task.failed:
            QTimer.singleShot(6000, self._dismiss)

    def _dismiss(self) -> None:
        self.manager.forget(self.task.id)
        self.setParent(None)
        self.deleteLater()


class TaskPanel(QWidget):
    """Dock content: one row per task, newest on top."""

    def __init__(self, manager: TaskManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.manager = manager
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        head = QHBoxLayout()
        self.summary = QLabel("No background tasks")
        self.summary.setObjectName("dim")
        head.addWidget(self.summary, 1)
        cancel_all = QPushButton("Cancel all")
        cancel_all.clicked.connect(manager.cancel_all)
        head.addWidget(cancel_all)
        layout.addLayout(head)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.container = QWidget()
        self.rows = QVBoxLayout(self.container)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(6)
        self.rows.addStretch(1)
        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll, 1)
        manager.task_added.connect(self._add)
        manager.count_changed.connect(self._update_summary)

    def _add(self, task: Task) -> None:
        row = _TaskRow(task, self.manager, self.container)
        self.rows.insertWidget(0, row)

    def _update_summary(self, running: int) -> None:
        self.summary.setText("No background tasks" if running == 0 else f"{running} task(s) running")
        self.summary.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
