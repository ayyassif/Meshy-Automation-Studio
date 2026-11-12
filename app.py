"""Meshy Automation Studio - entry point.

    .venv\\Scripts\\python app.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import APP_NAME, ApiKeyStore, AppSettings, api_base_url, data_dir  # noqa: E402
from database.db import Database  # noqa: E402
from services.meshy_client import MeshyClient  # noqa: E402
from services.queue_manager import QueueManager  # noqa: E402
from utils.logging_setup import register_secret, setup_logging  # noqa: E402

log = logging.getLogger("app")


class AppContext:
    """Wires settings, key storage, database and the queue engine together."""

    def __init__(self, home: Path | None = None):
        self.home = home or data_dir()
        self.log_file = setup_logging(self.home / "logs")
        self.settings_path = self.home / "settings.json"
        self.settings = AppSettings.load(self.settings_path)
        self.keys = ApiKeyStore()
        self.api_key = self.keys.get()
        register_secret(self.api_key)
        self.db = Database(self.home / "jobs.db")
        self.event_sink = None
        self.qm = QueueManager(self.db, lambda: self.settings, self._make_client, on_event=self._event)

    def _event(self, kind: str, job_id: int, message: str) -> None:
        if self.event_sink:
            self.event_sink(kind, job_id, message)

    def _make_client(self) -> MeshyClient | None:
        if not self.api_key:
            return None
        return MeshyClient(self.api_key, base_url=api_base_url())

    def set_api_key(self, key: str) -> None:
        self.keys.set(key)
        self.api_key = key.strip()
        register_secret(self.api_key)

    def delete_api_key(self) -> None:
        self.keys.delete()
        self.api_key = None
        self.qm.reset_client()

    def save_settings(self) -> None:
        self.settings.save(self.settings_path)

    def start(self) -> None:
        for d in (self.settings.input_dir, self.settings.output_dir):
            Path(d).mkdir(parents=True, exist_ok=True)
        self.qm.start()
        log.info("%s started (data: %s, API: %s)", APP_NAME, self.home, api_base_url())

    def shutdown(self) -> None:
        self.qm.stop()
        self.db.close()
        log.info("Shut down cleanly")


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from ui.main_window import MainWindow
    from ui.style import STYLESHEET, app_icon

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(True)

    ctx = AppContext()
    if not ctx.settings_path.exists():
        ctx.save_settings()  # write defaults so they are visible/editable
    ctx.start()
    win = MainWindow(ctx)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
