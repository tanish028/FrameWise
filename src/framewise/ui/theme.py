"""Dark theme built on Qt's Fusion style."""
from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

ACCENT = "#4c8dff"
STYLESHEET = f"""
QWidget {{ font-size: 10pt; }}
QToolBar {{ border: none; spacing: 4px; padding: 4px; }}
QToolButton {{ padding: 4px 8px; border-radius: 4px; }}
QToolButton:hover {{ background: #3a3d44; }}
QPushButton {{ padding: 5px 12px; border-radius: 4px; background: #3a3d44; border: 1px solid #4a4d55; }}
QPushButton:hover {{ background: #454952; }}
QPushButton:disabled {{ color: #777; background: #2e3036; }}
QPushButton#primary {{ background: {ACCENT}; border-color: {ACCENT}; color: white; font-weight: 600; }}
QPushButton#primary:hover {{ background: #6a9fff; }}
QGroupBox {{ border: 1px solid #3a3d44; border-radius: 6px; margin-top: 14px; padding-top: 6px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; color: #aab; }}
QListWidget, QTreeWidget {{ border: 1px solid #3a3d44; border-radius: 4px; }}
QListWidget::item {{ padding: 4px; }}
QListWidget::item:selected, QTreeWidget::item:selected {{ background: #2f4a7a; }}
QProgressBar {{ border: 1px solid #4a4d55; border-radius: 3px; text-align: center; height: 14px; }}
QProgressBar::chunk {{ background: {ACCENT}; }}
QLabel#muted {{ color: #9a9ca3; }}
QLabel#warning {{ color: #f0b84c; }}
QLabel#heading {{ font-size: 12pt; font-weight: 600; }}
QSplitter::handle {{ background: #26282c; }}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    p = QPalette()
    base, window, text = QColor("#1e2023"), QColor("#2a2c31"), QColor("#e6e6e6")
    for role, color in {
        QPalette.Window: window, QPalette.WindowText: text, QPalette.Base: base,
        QPalette.AlternateBase: QColor("#25272b"), QPalette.ToolTipBase: window, QPalette.ToolTipText: text,
        QPalette.Text: text, QPalette.Button: window, QPalette.ButtonText: text,
        QPalette.Highlight: QColor(ACCENT), QPalette.HighlightedText: QColor("white"),
        QPalette.PlaceholderText: QColor("#808288"), QPalette.Link: QColor(ACCENT),
    }.items():
        p.setColor(role, color)
    for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
        p.setColor(QPalette.Disabled, role, QColor("#6c6e74"))
    app.setPalette(p)
    app.setStyleSheet(STYLESHEET)
