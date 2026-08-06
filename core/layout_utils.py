from __future__ import annotations

from PySide6.QtWidgets import QFrame, QSplitter


def detach_widget_from_splitter(splitter: QSplitter, widget, placeholder: QFrame) -> bool:
    """Move a widget out of a splitter and replace it with a placeholder."""
    if splitter is None or widget is None or placeholder is None:
        return False

    idx = splitter.indexOf(widget)
    if idx < 0:
        return False

    splitter.replaceWidget(idx, placeholder)
    return True


def reattach_widget_to_splitter(splitter: QSplitter, widget, placeholder: QFrame) -> bool:
    """Restore a widget to its original splitter slot and remove the placeholder."""
    if splitter is None or widget is None or placeholder is None:
        return False

    idx = splitter.indexOf(placeholder)
    if idx < 0:
        return False

    splitter.replaceWidget(idx, widget)
    return True
