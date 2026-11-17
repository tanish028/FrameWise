"""Midnight Studio: FrameWise's design tokens and the Qt stylesheet generated from them.

Every colour, radius and spacing value used by the UI comes from here, so the look can be adjusted in
one place. Widgets choose styles through object names (`setObjectName("primary")`) and dynamic
properties (`role="ai"`) instead of inline stylesheets.
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

# ---- colour tokens -----------------------------------------------------------------------------
BG = "#0b0f19"          # window background (deep navy/charcoal)
PANEL = "#121826"       # panels
PANEL_2 = "#182033"     # raised controls inside panels
PANEL_3 = "#202a40"     # hover
BORDER = "#253049"
TEXT = "#f3f5f9"
MUTED = "#8a93a8"
SUBTLE = "#5d667c"
ACCENT = "#3b82f6"      # electric blue: primary actions, selection
ACCENT_HOVER = "#5b9bff"
AI = "#8b5cf6"          # violet: AI features
AI_HOVER = "#a07bff"
WARNING = "#f2b84b"
DANGER = "#f0616d"
SUCCESS = "#34d399"
VIEWER_BG = "#06080e"

# ---- size tokens -------------------------------------------------------------------------------
RADIUS = 6
RADIUS_L = 10
SPACE = 8
FONT_PT = 10


def stylesheet() -> str:
    return f"""
* {{ outline: none; }}
QWidget {{ font-size: {FONT_PT}pt; color: {TEXT}; }}
QMainWindow, QDialog {{ background: {BG}; }}
QToolTip {{ background: {PANEL_2}; color: {TEXT}; border: 1px solid {BORDER}; padding: 6px; border-radius: {RADIUS}px; }}

/* panels */
QFrame#panel, QWidget#panel {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: {RADIUS_L}px; }}
QFrame#topbar {{ background: {PANEL}; border-bottom: 1px solid {BORDER}; }}
QLabel#brand {{ font-size: 13pt; font-weight: 700; letter-spacing: 0.5px; }}
QLabel#brandAccent {{ font-size: 13pt; font-weight: 700; color: {ACCENT}; }}
QLabel#heading {{ font-size: 11pt; font-weight: 600; }}
QLabel#muted, QLabel[role="muted"] {{ color: {MUTED}; }}
QLabel#warning {{ color: {WARNING}; }}
QLabel#badge {{ background: {PANEL_3}; color: {MUTED}; border-radius: 4px; padding: 1px 6px; font-size: 8pt; }}
QLabel#aiBadge {{ background: {AI}; color: white; border-radius: 4px; padding: 1px 6px; font-size: 8pt; font-weight: 600; }}

/* buttons */
QPushButton {{ background: {PANEL_2}; border: 1px solid {BORDER}; border-radius: {RADIUS}px; padding: 6px 12px; }}
QPushButton:hover {{ background: {PANEL_3}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:disabled, QToolButton:disabled {{ color: {SUBTLE}; background: {PANEL}; }}
QPushButton:checked {{ background: {ACCENT}; border-color: {ACCENT}; color: white; }}
QPushButton#primary {{ background: {ACCENT}; border-color: {ACCENT}; color: white; font-weight: 600; }}
QPushButton#primary:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#primary:disabled {{ background: {PANEL_2}; color: {SUBTLE}; border-color: {BORDER}; }}
QPushButton#ai {{ background: {AI}; border-color: {AI}; color: white; font-weight: 600; }}
QPushButton#ai:hover {{ background: {AI_HOVER}; }}
QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: {RADIUS}px; padding: 5px; }}
QToolButton:hover {{ background: {PANEL_3}; }}
QToolButton:checked {{ background: {PANEL_3}; border-color: {ACCENT}; }}
QToolButton#segment {{ background: {PANEL_2}; border: 1px solid {BORDER}; padding: 5px 12px; border-radius: 0px; }}
QToolButton#segment:checked {{ background: {ACCENT}; border-color: {ACCENT}; color: white; }}
QToolButton#segmentFirst {{ background: {PANEL_2}; border: 1px solid {BORDER}; padding: 5px 12px;
    border-top-left-radius: {RADIUS}px; border-bottom-left-radius: {RADIUS}px; }}
QToolButton#segmentFirst:checked, QToolButton#segmentLast:checked {{ background: {ACCENT}; border-color: {ACCENT}; color: white; }}
QToolButton#segmentLast {{ background: {PANEL_2}; border: 1px solid {BORDER}; padding: 5px 12px;
    border-top-right-radius: {RADIUS}px; border-bottom-right-radius: {RADIUS}px; }}

/* sections */
QToolButton#sectionHeader {{ border: none; border-radius: 0; padding: 8px 4px; font-weight: 600; text-align: left; }}
QToolButton#sectionHeader:hover {{ background: transparent; color: white; }}
QToolButton#sectionHeader:checked {{ background: transparent; border: none; }}
QFrame#sectionLine {{ background: {BORDER}; max-height: 1px; min-height: 1px; border: none; }}

/* inputs */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTimeEdit {{ background: {PANEL_2}; border: 1px solid {BORDER};
    border-radius: {RADIUS}px; padding: 4px 6px; selection-background-color: {ACCENT}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QAbstractSpinBox QLineEdit {{ border: none; padding: 0px; margin: 0px; background: transparent; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{ background: {PANEL_2}; border: 1px solid {BORDER}; selection-background-color: {ACCENT}; }}
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 0px; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {BORDER}; background: {PANEL_2}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QCheckBox[role="ai"]::indicator:checked {{ background: {AI}; border-color: {AI}; }}

/* sliders */
QSlider::groove:horizontal {{ height: 4px; background: {BORDER}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {TEXT}; width: 12px; height: 12px; margin: -5px 0; border-radius: 6px; }}
QSlider::handle:horizontal:hover {{ background: white; }}
QSlider[role="ai"]::sub-page:horizontal {{ background: {AI}; }}

/* lists, trees, tabs */
QListWidget, QTreeWidget {{ background: transparent; border: none; }}
QListWidget::item {{ padding: 6px; border-radius: {RADIUS}px; margin: 1px 0; }}
QListWidget::item:hover {{ background: {PANEL_2}; }}
QListWidget::item:selected, QTreeWidget::item:selected {{ background: {PANEL_3}; border: 1px solid {ACCENT}; }}
QHeaderView::section {{ background: {PANEL}; color: {MUTED}; border: none; border-bottom: 1px solid {BORDER}; padding: 4px; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 6px 14px; border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom-color: {ACCENT}; }}
QProgressBar {{ background: {PANEL_2}; border: 1px solid {BORDER}; border-radius: 4px; text-align: center; height: 14px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: {PANEL_3}; border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: {PANEL_3}; border-radius: 5px; min-width: 30px; }}
QSplitter::handle {{ background: {BG}; }}
QStatusBar {{ background: {PANEL}; color: {MUTED}; border-top: 1px solid {BORDER}; }}
QMenuBar {{ background: {PANEL}; }}
QMenuBar::item:selected, QMenu::item:selected {{ background: {PANEL_3}; }}
QMenu {{ background: {PANEL_2}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 5px 18px; border-radius: 4px; }}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    p = QPalette()
    roles = {
        QPalette.Window: BG, QPalette.WindowText: TEXT, QPalette.Base: PANEL_2, QPalette.AlternateBase: PANEL,
        QPalette.ToolTipBase: PANEL_2, QPalette.ToolTipText: TEXT, QPalette.Text: TEXT, QPalette.Button: PANEL_2,
        QPalette.ButtonText: TEXT, QPalette.Highlight: ACCENT, QPalette.HighlightedText: "#ffffff",
        QPalette.PlaceholderText: SUBTLE, QPalette.Link: ACCENT, QPalette.Mid: BORDER, QPalette.Dark: BG,
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
        p.setColor(QPalette.Disabled, role, QColor(SUBTLE))
    app.setPalette(p)
    font = QFont(app.font())
    font.setPointSize(FONT_PT)
    app.setFont(font)
    app.setStyleSheet(stylesheet())


# Back-compat for code written against the Phase 1 theme module
STYLESHEET = stylesheet()
