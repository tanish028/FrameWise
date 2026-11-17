"""Render queue view: progress, ETA and controls for every job."""
from __future__ import annotations

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QProgressBar,
                               QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..media.probe import format_duration
from ..render.queue import Job, JobStatus, RenderQueue

COLS = ("Name", "Status", "Progress", "Time left")
ID_ROLE = Qt.UserRole


class QueueBridge(QObject):
    """Re-emits queue events (from the worker thread) as a Qt signal delivered on the UI thread."""
    event = Signal(str, object)

    def __init__(self, queue: RenderQueue, parent: QObject | None = None) -> None:
        super().__init__(parent)
        queue.subscribe(lambda e, j: self.event.emit(e, j))


class QueuePanel(QWidget):
    job_finished = Signal(object)  # Job

    def __init__(self, queue: RenderQueue, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.queue = queue
        self.bridge = QueueBridge(queue, self)
        self.bridge.event.connect(self._on_event)
        title = QLabel("Render queue")
        title.setObjectName("heading")
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(COLS)
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        h = self.tree.header()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 3):
            h.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        h.resizeSection(2, 220)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.tree.itemDoubleClicked.connect(lambda *_: self._open_output())
        self.btn_cancel = QPushButton("Cancel")
        self.btn_retry = QPushButton("Retry / resume")
        self.btn_remove = QPushButton("Remove")
        self.btn_open = QPushButton("Show file")
        self.btn_clear = QPushButton("Clear finished")
        self.btn_cancel.clicked.connect(lambda: self._with_job(lambda j: self.queue.cancel(j.id)))
        self.btn_retry.clicked.connect(lambda: self._with_job(lambda j: self.queue.retry(j.id)))
        self.btn_remove.clicked.connect(lambda: self._with_job(lambda j: self.queue.remove(j.id)))
        self.btn_open.clicked.connect(self._open_output)
        self.btn_clear.clicked.connect(self.queue.clear_finished)
        row = QHBoxLayout()
        for b in (self.btn_cancel, self.btn_retry, self.btn_remove, self.btn_open):
            row.addWidget(b)
        row.addStretch()
        row.addWidget(self.btn_clear)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addWidget(title)
        lay.addWidget(self.tree, 1)
        lay.addLayout(row)
        self._update_buttons()

    # -- helpers ------------------------------------------------------------
    def _row(self, job_id: str) -> QTreeWidgetItem | None:
        for n in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(n)
            if it.data(0, ID_ROLE) == job_id:
                return it
        return None

    def selected_job(self) -> Job | None:
        items = self.tree.selectedItems()
        return self.queue.get(items[0].data(0, ID_ROLE)) if items else None

    def _with_job(self, fn) -> None:
        job = self.selected_job()
        if job is not None:
            fn(job)

    def _open_output(self) -> None:
        job = self.selected_job()
        if job and job.status == JobStatus.DONE and job.spec.output.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(job.spec.output.parent)))

    def _update_buttons(self) -> None:
        job = self.selected_job()
        st = job.status if job else None
        self.btn_cancel.setEnabled(st in (JobStatus.QUEUED, JobStatus.RUNNING))
        self.btn_retry.setEnabled(st in (JobStatus.FAILED, JobStatus.CANCELLED))
        self.btn_remove.setEnabled(job is not None and st != JobStatus.RUNNING)
        self.btn_open.setEnabled(st == JobStatus.DONE)

    # -- events -------------------------------------------------------------
    def _on_event(self, event: str, job: Job) -> None:
        row = self._row(job.id)
        if event == "removed":
            if row is not None:
                self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(row))
            self._update_buttons()
            return
        if row is None:
            row = QTreeWidgetItem([("Preview: " if job.preview else "") + job.name, "", "", ""])
            row.setData(0, ID_ROLE, job.id)
            row.setToolTip(0, str(job.spec.output))
            self.tree.addTopLevelItem(row)
            bar = QProgressBar()
            bar.setRange(0, 1000)
            self.tree.setItemWidget(row, 2, bar)
        self._paint(row, job)
        if event == "updated" and job.status.finished:
            self.job_finished.emit(job)
        self._update_buttons()

    def _paint(self, row: QTreeWidgetItem, job: Job) -> None:
        p = job.progress
        status = job.status.value
        if job.status == JobStatus.RUNNING and p:
            status = f"{p.stage}" + (f" · {p.detail}" if p.detail else "")
        row.setText(1, status)
        row.setToolTip(1, job.error or status)
        bar: QProgressBar = self.tree.itemWidget(row, 2)
        if bar is not None:
            frac = 1.0 if job.status == JobStatus.DONE else (p.fraction if p else 0.0)
            bar.setValue(int(frac * 1000))
            bar.setFormat(f"{frac:.0%}" + (f"  ({p.done}/{p.total} frames)" if p and p.total > 1 else ""))
        eta = job.eta_seconds()
        if eta is not None:
            row.setText(3, format_duration(eta))
        elif job.status.finished and job.started_at and job.finished_at:
            row.setText(3, f"took {format_duration(job.finished_at - job.started_at)}")
        else:
            row.setText(3, "")
