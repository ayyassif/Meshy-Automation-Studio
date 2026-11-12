"""Main window: tabs, tray notifications, thread-safe event bridge, folder watcher control."""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import (QApplication, QLabel, QMainWindow, QMessageBox, QPlainTextEdit, QSystemTrayIcon,
                               QTabWidget)

from models.job import POLLABLE, REMOTE_ACTIVE, JobStatus
from services.folder_watcher import FolderWatcher
from ui.dashboard import Dashboard
from ui.settings_page import SettingsPage
from ui.style import app_icon
from utils.logging_setup import FORMAT, RedactingFilter

log = logging.getLogger(__name__)


class Bridge(QObject):
    """Signals emitted from worker threads; Qt delivers them on the GUI thread."""
    event = Signal(str, int, str)
    log_line = Signal(str)


class QtLogHandler(logging.Handler):
    def __init__(self, bridge: Bridge):
        super().__init__(logging.INFO)
        self.bridge = bridge
        self.setFormatter(logging.Formatter(FORMAT, "%H:%M:%S"))
        self.addFilter(RedactingFilter())

    def emit(self, record):
        try:
            self.bridge.log_line.emit(self.format(record))
        except Exception:  # noqa: BLE001
            pass


class MainWindow(QMainWindow):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.watcher: FolderWatcher | None = None
        self.setWindowTitle("Meshy Automation Studio")
        self.setWindowIcon(app_icon())
        self.resize(1250, 820)

        self.bridge = Bridge()
        self.bridge.event.connect(self._on_event)
        ctx.event_sink = self.bridge.event.emit
        self._log_handler = QtLogHandler(self.bridge)
        logging.getLogger().addHandler(self._log_handler)

        tabs = QTabWidget()
        self.dashboard = Dashboard(ctx)
        self.settings_page = SettingsPage(ctx)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.bridge.log_line.connect(self.log_view.appendPlainText)
        tabs.addTab(self.dashboard, "Dashboard")
        tabs.addTab(self.settings_page, "Settings")
        tabs.addTab(self.log_view, "Log")
        self.setCentralWidget(tabs)

        self.status_label = QLabel()
        self.statusBar().addWidget(self.status_label, 1)

        self.dashboard.monitor_toggled.connect(self._set_monitoring)
        self.dashboard.auto_submit_toggled.connect(ctx.qm.set_watch_auto_submit)
        self.settings_page.saved.connect(self._settings_saved)
        self.settings_page.key_changed.connect(self._key_changed)

        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(app_icon(), self)
            self.tray.setToolTip("Meshy Automation Studio")
            self.tray.activated.connect(lambda *_: (self.showNormal(), self.activateWindow()))
            self.tray.show()

        self._dirty = True
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(700)
        self._tick()

    # ================================================================ events
    def _on_event(self, kind: str, job_id: int, message: str):
        self._dirty = True
        if kind == "completed":
            self.notify("Model ready", message)
        elif kind == "failed":
            job = self.ctx.db.get_job(job_id)
            self.notify("Generation failed", f"{job.name if job else ''}: {message}"[:250], error=True)
        elif kind == "budget":
            self.notify("Batch budget reached", message)
        elif kind == "info" and message:
            QMessageBox.information(self, "Meshy Automation Studio", message)

    def notify(self, title: str, message: str, error: bool = False):
        if not self.ctx.settings.notifications:
            return
        if self.tray:
            icon = QSystemTrayIcon.MessageIcon.Critical if error else QSystemTrayIcon.MessageIcon.Information
            self.tray.showMessage(title, message, icon, 6000)
        QApplication.alert(self)

    def _tick(self):
        if self._dirty:
            self._dirty = False
            self.dashboard.refresh()
        self._update_status()

    def _update_status(self):
        active = self.ctx.db.jobs_by_status((*REMOTE_ACTIVE, JobStatus.DOWNLOADING))
        parts = []
        for j in active[:3]:
            pct = f" {j.progress}%" if j.status in POLLABLE else ""
            parts.append(f"{j.name}: {j.status_label}{pct}")
        text = " | ".join(parts) if parts else "Idle"
        if self.ctx.qm.paused:
            text = "[Queue paused]  " + text
        if self.watcher and self.watcher.running:
            text += f"   Monitoring {self.ctx.settings.input_dir}" + (" (auto-submit ON)"
                                                                       if self.ctx.qm.watch_auto_submit else "")
        self.status_label.setText(text)

    # ============================================================ monitoring
    def _set_monitoring(self, on: bool):
        if self.watcher:
            self.watcher.stop()
            self.watcher = None
        if on:
            self.watcher = FolderWatcher(self.ctx.settings.input_dir, self.ctx.qm.handle_watched_file,
                                         process_existing=self.ctx.settings.process_existing_on_enable)
            self.watcher.start()
        log.info("Folder monitoring %s", "enabled" if on else "disabled")
        self._dirty = True

    def _settings_saved(self):
        if self.watcher and str(self.watcher.folder) != self.ctx.settings.input_dir:
            self._set_monitoring(True)  # restart on the new folder
        self._dirty = True

    def _key_changed(self):
        self.ctx.qm.reset_client()
        self.ctx.qm.request_balance_refresh()
        self._dirty = True

    # ================================================================= close
    def closeEvent(self, event):
        running = self.ctx.db.count_status((*REMOTE_ACTIVE, JobStatus.DOWNLOADING))
        if running:
            ans = QMessageBox.question(
                self, "Quit", f"{running} job(s) are still generating or downloading.\n\nMeshy keeps working on "
                "them; they will be picked up again automatically the next time you open the app "
                "(within Meshy's 3-day retention). Quit now?")
            if ans != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        if self.watcher:
            self.watcher.stop()
        logging.getLogger().removeHandler(self._log_handler)
        if self.tray:
            self.tray.hide()
        self.ctx.shutdown()
        event.accept()
