"""Dark theme stylesheet and app icon."""
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QBrush, QColor, QIcon, QLinearGradient, QPainter, QPixmap, QPolygonF

ACCENT = "#7c5cff"
STATUS_COLORS = {
    "COMPLETED": "#3ecf8e", "FAILED": "#ff5c7a", "DOWNLOAD_FAILED": "#ffb547", "UNCERTAIN": "#ffb547",
    "IN_PROGRESS": "#5cb8ff", "PENDING": "#5cb8ff", "SUBMITTING": "#5cb8ff", "DOWNLOADING": "#5cb8ff",
    "APPROVED": "#c8c8d8", "QUEUED": "#9a9ab0", "CANCELED": "#777788",
}

STYLESHEET = f"""
QWidget {{ background: #15161c; color: #e6e6ef; font-family: 'Segoe UI'; font-size: 10pt; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: transparent; padding: 10px 18px; color: #9a9ab0; border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: #ffffff; border-bottom: 2px solid {ACCENT}; }}
QGroupBox {{ border: 1px solid #2a2b36; border-radius: 8px; margin-top: 14px; padding: 12px 10px 10px 10px;
             font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #b9b9cc; }}
QPushButton {{ background: #262733; border: 1px solid #343545; border-radius: 6px; padding: 7px 14px; }}
QPushButton:hover {{ background: #303142; }}
QPushButton:disabled {{ color: #66667a; }}
QPushButton#primary {{ background: {ACCENT}; border: none; color: white; font-weight: 600; }}
QPushButton#primary:hover {{ background: #8d71ff; }}
QPushButton#danger {{ color: #ff8da1; }}
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{ background: #1d1e26; border: 1px solid #343545;
             border-radius: 6px; padding: 5px 7px; selection-background-color: {ACCENT}; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox QAbstractItemView {{ background: #1d1e26; selection-background-color: {ACCENT}; }}
QTableWidget {{ background: #1a1b22; border: 1px solid #2a2b36; border-radius: 8px; gridline-color: transparent;
               alternate-background-color: #1d1e26; }}
QTableWidget::item {{ padding: 4px; }}
QTableWidget::item:selected {{ background: #2e2a4f; color: white; }}
QHeaderView::section {{ background: #1a1b22; color: #8a8aa0; border: none; border-bottom: 1px solid #2a2b36;
                        padding: 6px; font-weight: 600; }}
QFrame#card {{ background: #1d1e26; border: 1px solid #2a2b36; border-radius: 10px; }}
QLabel#cardTitle {{ color: #8a8aa0; font-size: 9pt; background: transparent; }}
QLabel#cardValue {{ font-size: 18pt; font-weight: 700; background: transparent; }}
QFrame#dropZone {{ background: #1a1b24; border: 2px dashed #3a3b50; border-radius: 12px; }}
QFrame#dropZone[active="true"] {{ border-color: {ACCENT}; background: #201d33; }}
QLabel#banner {{ background: #3a2e12; color: #ffd27a; border-radius: 6px; padding: 6px 10px; }}
QLabel#hint {{ color: #8a8aa0; }}
QCheckBox {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator {{ width: 15px; height: 15px; border: 1px solid #55566a; border-radius: 4px; background: #1d1e26; }}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border: 3px solid #1d1e26; outline: 1px solid {ACCENT}; }}
QCheckBox:disabled, QLabel:disabled, QComboBox:disabled, QSpinBox:disabled {{ color: #5a5a6e; }}
QCheckBox::indicator:disabled {{ border-color: #33343f; background: #191a20; }}
QScrollArea {{ border: none; }}
QStatusBar {{ color: #9a9ab0; }}
QProgressBar {{ background: #262733; border: none; border-radius: 4px; text-align: center; color: white; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
"""


def app_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    grad = QLinearGradient(0, 0, 64, 64)
    grad.setColorAt(0, QColor("#8d71ff"))
    grad.setColorAt(1, QColor("#3ec6ff"))
    p.setBrush(QBrush(grad))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(2, 2, 60, 60, 14, 14)
    p.setBrush(QColor(255, 255, 255, 230))
    p.drawPolygon(QPolygonF([QPointF(32, 12), QPointF(50, 22), QPointF(32, 32), QPointF(14, 22)]))
    p.setBrush(QColor(255, 255, 255, 150))
    p.drawPolygon(QPolygonF([QPointF(14, 24), QPointF(31, 34), QPointF(31, 53), QPointF(14, 43)]))
    p.setBrush(QColor(255, 255, 255, 90))
    p.drawPolygon(QPolygonF([QPointF(50, 24), QPointF(33, 34), QPointF(33, 53), QPointF(50, 43)]))
    p.end()
    return QIcon(pm)
