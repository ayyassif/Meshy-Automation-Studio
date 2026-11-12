"""Polling folder watcher.

Polling (instead of OS change notifications) is deliberate: it is simple, works
on network/cloud-synced folders, and lets us wait until a file has stopped
growing so half-copied images are never submitted.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

from utils.images import is_supported_path

log = logging.getLogger(__name__)


class FolderWatcher:
    def __init__(self, folder: str | Path, on_ready: Callable[[Path], None], *, interval: float = 2.0,
                 process_existing: bool = False):
        self.folder = Path(folder)
        self.on_ready = on_ready
        self.interval = interval
        self.process_existing = process_existing
        self._handled: set[Path] = set()
        self._pending: dict[Path, tuple[int, float]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _candidates(self) -> list[Path]:
        try:
            return sorted(p for p in self.folder.iterdir() if p.is_file() and is_supported_path(p))
        except OSError:
            return []

    def start(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self._handled.clear()
        self._pending.clear()
        if not self.process_existing:
            self._handled.update(self._candidates())  # baseline: ignore what is already there
            if self._handled:
                log.info("Folder watcher ignoring %d existing image(s)", len(self._handled))
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="FolderWatcher", daemon=True)
        self._thread.start()
        log.info("Watching %s", self.folder)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception:  # noqa: BLE001
                log.exception("Folder scan failed")
            self._stop.wait(self.interval)

    def scan_once(self) -> list[Path]:
        """One polling pass. A file is reported when its size/mtime is unchanged
        across two consecutive scans and it can be opened."""
        current = self._candidates()
        present = set(current)
        self._handled &= present  # forget files that were moved away
        for p in list(self._pending):
            if p not in present:
                del self._pending[p]
        ready: list[Path] = []
        for p in current:
            if p in self._handled:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            sig = (st.st_size, st.st_mtime)
            if st.st_size > 0 and self._pending.get(p) == sig:
                try:
                    with open(p, "rb"):
                        pass
                except OSError:
                    continue  # still locked by the writer
                del self._pending[p]
                self._handled.add(p)
                ready.append(p)
            else:
                self._pending[p] = sig
        for p in ready:
            log.info("Detected new image: %s", p.name)
            try:
                self.on_ready(p)
            except Exception:  # noqa: BLE001
                log.exception("Failed to handle %s", p.name)
        return ready
