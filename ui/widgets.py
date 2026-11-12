"""Reusable widgets: drop zone, stat card, progress delegate."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QStyledItemDelegate, QVBoxLayout

from utils.images import is_supported_path


def expand_paths(paths) -> list[Path]:
    """Files as-is; folders -> their supported images (non-recursive)."""
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            out += sorted(f for f in p.iterdir() if f.is_file() and is_supported_path(f))
        elif p.is_file() and is_supported_path(p):
            out.append(p)
    return out


class DropZone(QFrame):
    files_dropped = Signal(list)
    browse_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("dropZone")
        self.setAcceptDrops(True)
        self.setMinimumHeight(120)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel("Drop reference images here")
        title.setStyleSheet("font-size: 13pt; font-weight: 600; background: transparent;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint = QLabel("PNG, JPG, JPEG or WebP. You'll confirm the cost before anything is submitted.")
        hint.setObjectName("hint")
        hint.setStyleSheet("background: transparent;")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        btn = QPushButton("Browse images...")
        btn.setObjectName("primary")
        btn.clicked.connect(self.browse_clicked)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(btn)
        row.addStretch()
        lay.addWidget(title)
        lay.addWidget(hint)
        lay.addLayout(row)

    def _set_active(self, on: bool):
        self.setProperty("active", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._set_active(True)

    def dragLeaveEvent(self, e):
        self._set_active(False)

    def dropEvent(self, e):
        self._set_active(False)
        paths = expand_paths(u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile())
        if paths:
            self.files_dropped.emit(paths)
        e.acceptProposedAction()


class StatCard(QFrame):
    def __init__(self, title: str, value: str = "-", parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        t = QLabel(title)
        t.setObjectName("cardTitle")
        self.value = QLabel(value)
        self.value.setObjectName("cardValue")
        lay.addWidget(t)
        lay.addWidget(self.value)

    def set(self, value: str, tooltip: str = ""):
        self.value.setText(value)
        self.setToolTip(tooltip)


class ProgressDelegate(QStyledItemDelegate):
    """Paints (progress 0-100, color) stored in UserRole as a slim rounded bar with a label."""

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        data = index.data(Qt.ItemDataRole.UserRole)
        if not data:
            return
        value, color = data
        r = option.rect.adjusted(8, 0, -8, 0)
        bar = QRect(r.left(), r.center().y() - 3, max(0, r.width() - 40), 6)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#2a2b36"))
        painter.drawRoundedRect(bar, 3, 3)
        if value > 0:
            fill = QRect(bar)
            fill.setWidth(max(6, int(bar.width() * min(100, value) / 100)))
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(fill, 3, 3)
        painter.setPen(QColor("#b9b9cc"))
        painter.drawText(QRect(bar.right() + 6, r.top(), 34, r.height()),
                         int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), f"{int(value)}%")
        painter.restore()
