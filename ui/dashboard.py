"""Dashboard: drop zone, stats, folder monitoring, job queue."""
from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from models.job import POLLABLE, REMOTE_ACTIVE, JobStatus
from ui.style import STATUS_COLORS
from ui.widgets import DropZone, ProgressDelegate, StatCard

COLUMNS = ["#", "Asset", "Status", "Progress", "Est.", "Used", "Task ID", "Details"]


class Dashboard(QWidget):
    monitor_toggled = Signal(bool)
    auto_submit_toggled = Signal(bool)

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)

        # --- stats
        stats = QHBoxLayout()
        self.card_balance = StatCard("Meshy credits")
        self.card_completed = StatCard("Completed jobs")
        self.card_active = StatCard("Generating")
        self.card_queued = StatCard("Queued")
        self.card_failed = StatCard("Failed")
        for c in (self.card_balance, self.card_completed, self.card_active, self.card_queued, self.card_failed):
            stats.addWidget(c)
        root.addLayout(stats)

        self.banner = QLabel()
        self.banner.setObjectName("banner")
        self.banner.setWordWrap(True)
        self.banner.hide()
        root.addWidget(self.banner)

        # --- drop zone + watcher side by side
        top = QHBoxLayout()
        self.drop = DropZone()
        self.drop.files_dropped.connect(self.add_files)
        self.drop.browse_clicked.connect(self.browse)
        top.addWidget(self.drop, 3)

        watch = QGroupBox("Folder monitoring")
        wl = QGridLayout(watch)
        self.lbl_input = QLabel()
        self.lbl_input.setObjectName("hint")
        self.lbl_input.setWordWrap(True)
        self.chk_monitor = QCheckBox("Monitor input folder")
        self.chk_auto = QCheckBox("Auto-submit detected images (uses credits, respects budget)")
        self.chk_existing = QCheckBox("Include images already in the folder")
        self.chk_existing.setChecked(ctx.settings.process_existing_on_enable)
        self.chk_monitor.toggled.connect(self._monitor_toggled)
        self.chk_auto.toggled.connect(self._auto_toggled)
        btn_open_in = QPushButton("Open input folder")
        btn_open_in.clicked.connect(lambda: self._open(ctx.settings.input_dir, create=True))
        wl.addWidget(self.lbl_input, 0, 0, 1, 2)
        wl.addWidget(self.chk_monitor, 1, 0, 1, 2)
        wl.addWidget(self.chk_auto, 2, 0, 1, 2)
        wl.addWidget(self.chk_existing, 3, 0, 1, 2)
        wl.addWidget(btn_open_in, 4, 0)
        top.addWidget(watch, 2)
        root.addLayout(top)

        # --- toolbar
        bar = QHBoxLayout()
        self.btn_submit = QPushButton("Submit queued...")
        self.btn_submit.setObjectName("primary")
        self.btn_submit.clicked.connect(self.submit_queued)
        self.btn_pause = QPushButton("Pause queue")
        self.btn_pause.clicked.connect(self.toggle_pause)
        self.btn_retry = QPushButton("Retry")
        self.btn_retry.clicked.connect(self.retry_selected)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self.cancel_selected)
        self.btn_remove = QPushButton("Remove")
        self.btn_remove.setObjectName("danger")
        self.btn_remove.clicked.connect(self.remove_selected)
        self.btn_open = QPushButton("Open files")
        self.btn_open.clicked.connect(self.open_selected)
        self.btn_balance = QPushButton("Refresh balance")
        self.btn_balance.clicked.connect(ctx.qm.request_balance_refresh)
        for b in (self.btn_submit, self.btn_pause, self.btn_retry, self.btn_cancel, self.btn_remove, self.btn_open):
            bar.addWidget(b)
        bar.addStretch()
        bar.addWidget(self.btn_balance)
        root.addLayout(bar)

        # --- table
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setItemDelegateForColumn(3, ProgressDelegate(self.table))
        self.table.doubleClicked.connect(lambda _: self.open_selected())
        h = self.table.horizontalHeader()
        for i, mode in enumerate([QHeaderView.ResizeMode.ResizeToContents, QHeaderView.ResizeMode.Interactive,
                                  QHeaderView.ResizeMode.Interactive, QHeaderView.ResizeMode.Fixed,
                                  QHeaderView.ResizeMode.ResizeToContents, QHeaderView.ResizeMode.ResizeToContents,
                                  QHeaderView.ResizeMode.Interactive, QHeaderView.ResizeMode.Stretch]):
            h.setSectionResizeMode(i, mode)
        self.table.setColumnWidth(1, 150)
        self.table.setColumnWidth(2, 185)
        self.table.setColumnWidth(3, 130)
        self.table.setColumnWidth(6, 110)
        root.addWidget(self.table, 1)
        self.refresh()

    # ============================================================== refresh
    def refresh(self):
        s = self.ctx.settings
        self.lbl_input.setText(f"Input: {s.input_dir}\nOutput: {s.output_dir}")
        qm = self.ctx.qm
        if qm.balance:
            bal = qm.balance
            resets = bal.get("monthly_resets_at")
            tip = f"Monthly: {bal.get('monthly_credits', '?')}  Permanent: {bal.get('permanent_credits', '?')}"
            if resets:
                tip += f"\nMonthly credits reset {datetime.fromtimestamp(resets / 1000):%Y-%m-%d}"
            self.card_balance.set(f"{bal.get('balance', 0):,}", tip)
        elif qm.balance_error:
            self.card_balance.set("error", qm.balance_error)
        else:
            self.card_balance.set("-" if self.ctx.api_key else "no key", "")
        st = self.ctx.db.stats()
        self.card_completed.set(str(st.get(JobStatus.COMPLETED, 0)))
        self.card_active.set(str(sum(st.get(x, 0) for x in (*REMOTE_ACTIVE, JobStatus.DOWNLOADING))))
        self.card_queued.set(str(st.get(JobStatus.QUEUED, 0) + st.get(JobStatus.APPROVED, 0)))
        self.card_failed.set(str(st.get(JobStatus.FAILED, 0) + st.get(JobStatus.DOWNLOAD_FAILED, 0)))
        self.btn_pause.setText("Resume queue" if qm.paused else "Pause queue")
        banners = []
        if not self.ctx.api_key:
            banners.append("No Meshy API key set. Add it in Settings.")
        if qm.paused and qm.pause_reason:
            banners.append(qm.pause_reason)
        self.banner.setText("  ".join(banners))
        self.banner.setVisible(bool(banners))
        self._fill_table()

    def _fill_table(self):
        selected = set(self.selected_ids())
        jobs = self.ctx.db.list_jobs(limit=500)
        self.table.setRowCount(len(jobs))
        for row, j in enumerate(jobs):
            label = j.status_label
            if j.status in POLLABLE and j.submitted_at:
                label += f" ({int((time.time() - j.submitted_at) // 60)}m)"
            values = [str(j.id), j.name, label, "", "" if j.est_credits is None else str(j.est_credits),
                      "" if j.consumed_credits is None else str(j.consumed_credits),
                      (j.task_id or "")[:13], j.message]
            for col, v in enumerate(values):
                item = self.table.item(row, col)
                if item is None:
                    item = QTableWidgetItem()
                    self.table.setItem(row, col, item)
                item.setText(v)
            self.table.item(row, 0).setData(Qt.ItemDataRole.UserRole + 1, j.id)
            self.table.item(row, 2).setForeground(QColor(STATUS_COLORS.get(j.status, "#e6e6ef")))
            show_bar = j.task_id and j.status not in (JobStatus.FAILED, JobStatus.CANCELED)
            self.table.item(row, 3).setData(Qt.ItemDataRole.UserRole,
                                            (j.progress, STATUS_COLORS.get(j.status, "#7c5cff")) if show_bar else None)
            self.table.item(row, 6).setToolTip(j.task_id or "")
            self.table.item(row, 7).setToolTip(j.message)
            self.table.item(row, 1).setToolTip(j.source_path)
        self.table.clearSelection()
        mode = self.table.selectionMode()
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).data(Qt.ItemDataRole.UserRole + 1) in selected:
                self.table.selectRow(row)
        self.table.setSelectionMode(mode)

    def selected_ids(self) -> list[int]:
        rows = {i.row() for i in self.table.selectedIndexes()}
        return [self.table.item(r, 0).data(Qt.ItemDataRole.UserRole + 1) for r in sorted(rows)
                if self.table.item(r, 0)]

    # ============================================================== actions
    def browse(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Select reference images", "",
                                                "Images (*.png *.jpg *.jpeg *.webp)")
        if files:
            self.add_files([Path(f) for f in files])

    def add_files(self, paths: list[Path]):
        results = self.ctx.qm.add_images(paths)
        added = [r.job_id for r in results if r.job_id]
        problems = [f"{r.path.name}: {r.error}" for r in results if r.error]
        if problems:
            QMessageBox.warning(self, "Some images were not added", "\n".join(problems[:15]))
        self.refresh()
        if added:
            self.submit_queued(added)

    def submit_queued(self, job_ids=None):
        if not isinstance(job_ids, list):
            sel = [i for i in self.selected_ids()
                   if (j := self.ctx.db.get_job(i)) and j.status == JobStatus.QUEUED]
            job_ids = sel or None
        if not self.ctx.api_key:
            QMessageBox.information(self, "API key needed", "Add your Meshy API key in Settings first.")
            return
        gen = self.ctx.settings.generation
        errors = gen.validate()
        if errors:
            QMessageBox.warning(self, "Invalid settings", "\n".join(errors))
            return
        count, total = self.ctx.qm.estimate(job_ids)
        if not count:
            QMessageBox.information(self, "Nothing to submit", "There are no queued images.")
            return
        budget = self.ctx.settings.max_credits_per_batch
        bal = self.ctx.qm.balance.get("balance") if self.ctx.qm.balance else None
        lines = [f"Submit {count} image(s) to Meshy Image-to-3D?", "",
                 f"Settings: {self.ctx.settings.generation.model_type}, {gen.describe()}",
                 f"Estimated cost: {total} credits ({gen.estimate_credits()} each)" if total is not None
                 else "Estimated cost: unknown for this configuration",
                 "(Estimate from Meshy's published pricing; the actual charge is shown when each job finishes.)",
                 f"Current balance: {bal:,}" if bal is not None else "Current balance: unknown (checked before each submission)",
                 f"Batch budget: {budget} credits" if budget else "Batch budget: no limit"]
        if budget and total is not None and total > budget:
            lines.append(f"\nOnly about {budget // max(1, gen.estimate_credits() or 1)} job(s) fit the budget; "
                         "the rest will stay queued.")
        if self.ctx.qm.paused:
            lines.append("\nThe queue is paused; jobs will start when you resume it.")
        ans = QMessageBox.question(self, "Confirm submission", "\n".join(lines),
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                   QMessageBox.StandardButton.Yes)
        if ans != QMessageBox.StandardButton.Yes:
            return
        try:
            self.ctx.qm.approve(job_ids)
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot submit", str(exc))
        self.refresh()

    def toggle_pause(self):
        if self.ctx.qm.paused:
            self.ctx.qm.resume()
        else:
            self.ctx.qm.pause("Paused by you. Running Meshy tasks are still tracked and downloaded.")
        self.refresh()

    def _each_selected(self, fn):
        ids = self.selected_ids()
        if not ids:
            QMessageBox.information(self, "Select jobs", "Select one or more jobs in the list first.")
            return []
        return [fn(i) for i in ids]

    def retry_selected(self):
        msgs = self._each_selected(self.ctx.qm.retry)
        if msgs:
            self.refresh()
            if any(m.startswith("Re-queued") for m in msgs):
                self.submit_queued([i for i in self.selected_ids()
                                    if self.ctx.db.get_job(i).status == JobStatus.QUEUED])
            others = [m for m in msgs if not m.startswith("Re-queued")]
            if others:
                QMessageBox.information(self, "Retry", "\n".join(sorted(set(others))))

    def cancel_selected(self):
        self._each_selected(self.ctx.qm.cancel)

    def remove_selected(self):
        ids = self.selected_ids()
        if not ids:
            return
        if QMessageBox.question(self, "Remove jobs", f"Remove {len(ids)} job(s) from the list? "
                                "Generated files are not deleted.") != QMessageBox.StandardButton.Yes:
            return
        msgs = [self.ctx.qm.remove(i) for i in ids]
        bad = [m for m in msgs if m != "Removed."]
        if bad:
            QMessageBox.information(self, "Remove", "\n".join(sorted(set(bad))))
        self.refresh()

    def open_selected(self):
        ids = self.selected_ids()
        if not ids:
            return
        job = self.ctx.db.get_job(ids[0])
        target = job.unity_dir or job.output_dir
        if target and Path(target).exists():
            self._open(target)
        else:
            QMessageBox.information(self, "No files yet", "This job has no downloaded files yet.")

    @staticmethod
    def _open(path, create=False):
        p = Path(path)
        if create:
            p.mkdir(parents=True, exist_ok=True)
        if p.exists():
            os.startfile(str(p))  # noqa: S606 - opens Explorer

    # =========================================================== monitoring
    def _monitor_toggled(self, on: bool):
        self.ctx.settings.process_existing_on_enable = self.chk_existing.isChecked()
        self.chk_existing.setEnabled(not on)
        self.monitor_toggled.emit(on)

    def _auto_toggled(self, on: bool):
        if on:
            s = self.ctx.settings
            budget = f"{s.max_credits_per_batch} credits" if s.max_credits_per_batch else "NO LIMIT"
            ans = QMessageBox.question(
                self, "Enable auto-submit",
                "New images placed in the input folder will be submitted to Meshy automatically, without asking.\n\n"
                f"Each costs about {s.generation.estimate_credits()} credits ({s.generation.describe()}).\n"
                f"Budget for this monitoring session: {budget}.\n\nEnable auto-submit?")
            if ans != QMessageBox.StandardButton.Yes:
                self.chk_auto.blockSignals(True)
                self.chk_auto.setChecked(False)
                self.chk_auto.blockSignals(False)
                return
        self.auto_submit_toggled.emit(on)
