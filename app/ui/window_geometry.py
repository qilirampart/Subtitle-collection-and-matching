from __future__ import annotations

from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication, QWidget


def apply_responsive_window_geometry(
    widget: QWidget,
    *,
    preferred_width: int,
    preferred_height: int,
    minimum_width: int,
    minimum_height: int,
    margin: int = 32,
) -> None:
    """Size a top-level window within the current screen's usable area."""
    screen = widget.screen() or QApplication.primaryScreen()
    available = screen.availableGeometry() if screen is not None else QRect()
    if available.isNull():
        widget.setMinimumSize(minimum_width, minimum_height)
        widget.resize(preferred_width, preferred_height)
        return

    max_width = max(1, available.width() - margin)
    max_height = max(1, available.height() - margin)
    width = min(preferred_width, max_width)
    height = min(preferred_height, max_height)
    widget.setMinimumSize(min(minimum_width, max_width), min(minimum_height, max_height))
    widget.resize(width, height)
    widget.move(
        available.x() + max(0, (available.width() - width) // 2),
        available.y() + max(0, (available.height() - height) // 2),
    )
