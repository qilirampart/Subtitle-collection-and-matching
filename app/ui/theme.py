from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QIcon, QPalette
from PySide6.QtWidgets import QApplication

from app.config.settings import RESOURCE_ROOT

# Registers the embedded indicator icons for both source runs and packaged apps.
from app.ui import resources_rc  # noqa: F401


APP_STYLESHEET = """
QMainWindow, QDialog {
    background: #F4F6F8;
    color: #17212B;
    font-family: "Microsoft YaHei UI", "Noto Sans CJK SC", sans-serif;
    font-size: 13px;
}
QWidget { outline: none; }

QWidget#WorkspaceShell { background: #F4F6F8; }
QFrame#NavigationRail {
    background: #103E46;
    border: 0;
}
QLabel#NavBrand {
    color: #FFFFFF;
    font-size: 16px;
    font-weight: 800;
    line-height: 1.35;
    padding: 4px 8px 10px 8px;
}
QLabel#NavFooter {
    color: #A9CDC9;
    font-size: 11px;
    padding: 8px;
}
QPushButton[nav="true"] {
    color: #D6E8E7;
    background: transparent;
    border: 0;
    border-radius: 8px;
    min-height: 34px;
    padding: 5px 10px;
    text-align: left;
}
QPushButton[nav="true"]:hover { background: #1D5660; color: #FFFFFF; }
QPushButton[nav="true"][nav_active="true"] {
    color: #FFFFFF;
    background: #1A6E70;
    font-weight: 800;
}
QLabel#PageTitle { color: #17313A; font-size: 24px; font-weight: 800; }
QLabel#PageHint { color: #6C7B82; font-size: 12px; }
QLabel#WorkspaceStatus {
    color: #155C56;
    background: #E8F4F2;
    border: 1px solid #C7E6E1;
    border-radius: 8px;
    padding: 8px 12px;
    font-weight: 600;
}
QFrame#DashboardMetric, QFrame#QuickActionCard, QFrame#WorkspaceCard, QFrame#SettingsCard {
    background: #FFFFFF;
    border: 1px solid #DCE5E7;
    border-radius: 12px;
}
QLabel#MetricLabel { color: #587078; font-size: 12px; font-weight: 700; }
QLabel#MetricValue { color: #0F766E; font-size: 28px; font-weight: 800; }
QLabel#MetricHint { color: #86949A; font-size: 11px; }
QLabel#QuickActionTitle { color: #193941; font-size: 16px; font-weight: 800; }

QFrame#Hero {
    background: qlineargradient(x1: 0, y1: 0, x2: 1, y2: 1,
        stop: 0 #142A32, stop: 0.55 #194B50, stop: 1 #0F766E);
    border-radius: 16px;
}
QLabel#HeroEyebrow {
    color: #A8E5DD;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1.2px;
}
QLabel#HeroTitle {
    color: #FFFFFF;
    font-size: 25px;
    font-weight: 700;
}
QLabel#HeroSubtitle { color: #D6E8E7; font-size: 13px; }
QLabel#HeroChip {
    color: #D4FFF8;
    background: rgba(255, 255, 255, 0.14);
    border: 1px solid rgba(255, 255, 255, 0.24);
    border-radius: 14px;
    padding: 6px 11px;
    font-weight: 700;
}

QFrame#SetupCard, QFrame#ActionCard, QFrame#SelectionCard, QFrame#ResultCard {
    background: #FFFFFF;
    border: 1px solid #DEE5E8;
    border-radius: 12px;
}
QFrame#TopBar {
    background: #173942;
    border: 1px solid #173942;
    border-radius: 12px;
}
QLabel#BrandTitle { color: #FFFFFF; font-size: 17px; font-weight: 700; }
QLabel#BrandSubtitle { color: #A9CDC9; font-size: 11px; font-weight: 600; }
QFrame#EntryCard, QFrame#WorkflowCard, QFrame#QueueCard, QFrame#SidePanel {
    background: #FFFFFF;
    border: 1px solid #DCE5E7;
    border-radius: 11px;
}
QFrame#AdvancedPanel {
    background: #ECF5F3;
    border: 1px solid #CAE3DE;
    border-radius: 9px;
}
QFrame#WorkflowCard QLabel { color: #5A6D75; font-weight: 700; }
QFrame#QueueCard, QFrame#SidePanel { border-radius: 10px; }
QFrame#WorkflowStep {
    background: #F7FAF9;
    border: 1px solid #D8E7E4;
    border-radius: 9px;
}
QLabel#WorkflowNumber {
    color: #0E756C;
    background: #D9EFEB;
    border-radius: 12px;
    min-width: 24px;
    min-height: 24px;
    font-size: 11px;
    font-weight: 800;
}
QLabel#WorkflowTitle { color: #1C3138; font-weight: 800; }
QLabel#WorkflowHint { color: #74838A; font-size: 11px; font-weight: 400; }
QLabel#MetricCard {
    color: #23545A;
    background: #F1F7F6;
    border: 1px solid #D9E8E5;
    border-radius: 8px;
    min-height: 42px;
    font-size: 12px;
    font-weight: 700;
}
QLabel#SectionTitle {
    color: #20343D;
    font-size: 14px;
    font-weight: 700;
}
QLabel#SectionHint { color: #73818A; font-size: 12px; }
QLabel#StatusBar {
    background: #E8F4F2;
    color: #145B55;
    border: 1px solid #C7E6E1;
    border-radius: 8px;
    padding: 8px 11px;
    font-weight: 600;
}

QLabel { color: #26343E; }
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: #FBFCFC;
    border: 1px solid #C9D4D9;
    border-radius: 7px;
    min-height: 20px;
    padding: 3px 8px;
    selection-background-color: #B7E5DE;
    selection-color: #102328;
}
QPlainTextEdit { padding: 8px; }
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    background: #FFFFFF;
    border: 2px solid #159A8C;
}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QPushButton:disabled {
    color: #9AA7AE;
    background: #EEF1F3;
    border-color: #DCE3E6;
}
/* Keep numeric controls obvious: they are frequently used to constrain long-running tasks. */
QComboBox::drop-down {
    width: 28px;
    background: #E7F3F1;
    border: 0;
    border-left: 1px solid #B8C9CD;
    border-top-right-radius: 6px;
    border-bottom-right-radius: 6px;
}
QComboBox::drop-down:hover { background: #CDE8E3; }
QComboBox::drop-down:disabled { background: #EEF1F3; border-left-color: #DCE3E6; }
QComboBox::down-arrow {
    image: url(:/ui/icons/icons/chevron-down.svg);
    width: 14px;
    height: 8px;
}

QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
    width: 26px;
    background: #E7F3F1;
    border: 0;
    border-left: 1px solid #B8C9CD;
}
QSpinBox::up-button, QDoubleSpinBox::up-button {
    subcontrol-origin: border;
    subcontrol-position: top right;
    border-top-right-radius: 6px;
    border-bottom: 1px solid #B8C9CD;
}
QSpinBox::down-button, QDoubleSpinBox::down-button {
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    border-bottom-right-radius: 6px;
}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
    image: url(:/ui/icons/icons/chevron-up.svg);
    width: 12px;
    height: 7px;
}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
    image: url(:/ui/icons/icons/chevron-down.svg);
    width: 12px;
    height: 7px;
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover { background: #BFE4DE; }
QSpinBox::up-button:pressed, QSpinBox::down-button:pressed,
QDoubleSpinBox::up-button:pressed, QDoubleSpinBox::down-button:pressed { background: #99D4CB; }
QSpinBox::up-button:disabled, QSpinBox::down-button:disabled,
QDoubleSpinBox::up-button:disabled, QDoubleSpinBox::down-button:disabled {
    background: #EEF1F3;
    border-left-color: #DCE3E6;
}

QPushButton {
    min-height: 30px;
    border: 1px solid #C4D0D4;
    border-radius: 7px;
    background: #FFFFFF;
    color: #29414A;
    padding: 4px 11px;
    font-weight: 600;
}
QPushButton:hover { background: #EFF8F7; border-color: #6CBDB4; }
QPushButton:pressed { background: #D8EFEB; }
QPushButton:focus { border-color: #29988D; }
QPushButton[primary="true"] { background: #0F766E; color: #FFFFFF; border-color: #0F766E; }
QPushButton[primary="true"]:hover { background: #0A625D; border-color: #0A625D; }
QPushButton[primary="true"]:pressed { background: #084C48; border-color: #084C48; }
QPushButton[secondary="true"] { background: #E8F4F2; color: #106B63; border-color: #B8DED8; }
QPushButton[secondary="true"]:hover { background: #CDEBE6; color: #0A625D; border-color: #29988D; }
QPushButton[secondary="true"]:pressed { background: #A8D8D0; color: #074C47; border-color: #167B72; }
QPushButton[danger="true"] { color: #A33B32; background: #FFF7F5; border-color: #F0C6C0; }
QPushButton[danger="true"]:hover { background: #FDE4DF; border-color: #D8897E; }
QPushButton[danger="true"]:pressed { background: #F6C9C1; border-color: #B85B50; }
QProgressBar {
    min-height: 20px;
    border: 1px solid #B9DCD6;
    border-radius: 7px;
    background: #F4FAF9;
    color: #145B55;
    text-align: center;
    font-weight: 700;
}
QProgressBar::chunk { background: #159A8C; border-radius: 6px; }

QGroupBox {
    background: #FFFFFF;
    border: 1px solid #DCE4E7;
    border-radius: 10px;
    margin-top: 12px;
    padding: 10px;
    font-weight: 700;
    color: #25404A;
}
QGroupBox::title { subcontrol-origin: margin; left: 11px; padding: 0 5px; }
QCheckBox { spacing: 7px; }
QCheckBox::indicator { width: 15px; height: 15px; border: 1px solid #AAB8BE; border-radius: 4px; background: #FFFFFF; }
QCheckBox::indicator:checked { background: #0F766E; border-color: #0F766E; }

QTableWidget {
    background: #FFFFFF;
    border: 1px solid #D8E1E5;
    border-radius: 9px;
    gridline-color: #E8EEF0;
    selection-background-color: #E4F5F2;
    selection-color: #17212B;
    alternate-background-color: #F8FAFA;
}
QTableWidget::item { padding: 5px 7px; border-bottom: 1px solid #E8EEF0; }
QHeaderView::section {
    background: #EAF1F2;
    color: #40545E;
    border: 0;
    border-bottom: 1px solid #D3E0E2;
    padding: 7px;
    font-weight: 700;
}

QTabWidget::pane { border: 1px solid #D8E1E5; border-radius: 9px; top: -1px; background: #FFFFFF; }
QTabBar::tab { background: #E7ECEE; color: #61727A; border: 0; border-top-left-radius: 8px; border-top-right-radius: 8px; padding: 8px 14px; margin-right: 4px; font-weight: 600; }
QTabBar::tab:selected { background: #FFFFFF; color: #0B7067; }
QTabBar::tab:hover { color: #0B7067; }

QScrollBar:vertical { width: 10px; background: transparent; margin: 4px; }
QScrollBar::handle:vertical { background: #B9C9CD; min-height: 32px; border-radius: 5px; }
QScrollBar::handle:vertical:hover { background: #8BA5AA; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QSplitter::handle { background: #D8E1E4; height: 5px; }
QSplitter::handle:hover { background: #75B9B0; }
QSplitter#EvidenceComparison::handle { background: transparent; }
QSplitter#EvidenceComparison::handle:hover { background: transparent; }
"""


def load_app_icon() -> QIcon:
    """Load the generated multi-size icon for source and packaged builds."""
    for name in ("app_icon.ico", "app_icon.png"):
        candidate = RESOURCE_ROOT / name
        if candidate.is_file():
            return QIcon(str(candidate))
    return QIcon()


def apply_app_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setWindowIcon(load_app_icon())
    palette = app.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#F4F6F8"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#FFFFFF"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#17212B"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#0F766E"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    app.setPalette(palette)
    app.setStyleSheet(APP_STYLESHEET)
