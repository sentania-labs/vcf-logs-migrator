"""Lattice building blocks for the workspace pages."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from html import escape

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import theme


def repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def label(
    text: str, kind: str = "", status: str = "", wrap: bool = False, rich: bool = False
) -> QLabel:
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.RichText if rich else Qt.TextFormat.PlainText)
    if kind:
        widget.setProperty("lattice", kind)
    if status:
        widget.setProperty("status", status)
    widget.setWordWrap(wrap)
    if wrap:
        # Let layouts shrink wrapped text below its one-line hint; otherwise a long sentence
        # widens the page past a narrow window instead of wrapping.
        widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


def plain_tip(text: str) -> str:
    return "<p>" + escape(text).replace("\n", "<br>") + "</p>"


def set_status(widget: QLabel, status: str) -> None:
    widget.setProperty("status", status)
    repolish(widget)


def section(text: str) -> QLabel:
    return label(text.upper(), "section-label")


def button(text: str, variant: str = "", slot=None) -> QPushButton:
    widget = QPushButton(text)
    if variant:
        widget.setProperty("variant", variant)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    if slot is not None:
        widget.clicked.connect(slot)
    return widget


def card(title: str = "") -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setProperty("lattice", "card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 16, 16, 16)
    layout.setSpacing(8)
    if title:
        layout.addWidget(section(title))
    return frame, layout


def banner(title: str, text: str, status: str = "info") -> QLabel:
    widget = label("", "banner", status, wrap=True)
    set_banner(widget, title, text, status)
    return widget


def set_banner(widget: QLabel, title: str, text: str, status: str) -> None:
    widget.setTextFormat(Qt.TextFormat.RichText)
    body = escape(text).replace("\n", "<br>")
    widget.setText(f"<b>{escape(title)}</b><br>{body}")
    set_status(widget, status)


def badge(text: str, status: str = "") -> QLabel:
    widget = label(text, "badge", status)
    widget.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    return widget


def metric(title: str, value: str, caption: str) -> tuple[QFrame, QLabel, QLabel]:
    frame, layout = card(title)
    value_label = label(value, "metric")
    caption_label = label(caption, "hint", wrap=True)
    layout.addWidget(value_label)
    layout.addWidget(caption_label)
    layout.addStretch(1)
    return frame, value_label, caption_label


class KeyValues(QWidget):
    """Muted keys on the left, values right aligned, as every mockup card shows them."""

    def __init__(self):
        super().__init__()
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(16)
        self.grid.setVerticalSpacing(6)
        self.grid.setColumnStretch(1, 1)

    def set_rows(self, rows: Iterable[tuple[str, str]]) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        for row, (key, value) in enumerate(rows):
            key_label = label(key, "muted")
            value_label = label(value, "value", wrap=True)
            value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            value_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
                | Qt.TextInteractionFlag.TextSelectableByKeyboard
            )
            value_label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self.grid.addWidget(key_label, row, 0, Qt.AlignmentFlag.AlignTop)
            self.grid.addWidget(value_label, row, 1)

    def rows(self) -> list[tuple[str, str]]:
        """The displayed pairs, for tests and reports."""
        pairs = []
        for row in range(self.grid.rowCount()):
            key = self.grid.itemAtPosition(row, 0)
            value = self.grid.itemAtPosition(row, 1)
            if key and value and key.widget() and value.widget():
                pairs.append((key.widget().text(), value.widget().text()))
        return pairs


class Table(QTableWidget):
    """Read-only table with Lattice headers; rows are plain tuples of strings."""

    def __init__(self, headers: Sequence[str], fit: Sequence[int] = ()):
        super().__init__(0, len(headers))
        self.setHorizontalHeaderLabels([h.upper() for h in headers])
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setShowGrid(False)
        self.setAlternatingRowColors(False)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setWordWrap(False)
        self.setMinimumWidth(0)
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setMinimumSectionSize(40)
        for column in fit:
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setHighlightSections(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

    def set_rows(
        self,
        rows: Iterable[Sequence[str]],
        widgets: dict[int, QWidget] | None = None,
        tips: Sequence[str] | None = None,
    ) -> None:
        rows = list(rows)
        self.setRowCount(len(rows))
        for r, values in enumerate(rows):
            for c, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                # Narrow windows truncate cells; the full text stays one hover away.
                item.setToolTip(plain_tip(tips[r] if tips and tips[r] else str(value)))
                self.setItem(r, c, item)
        for (r, c), widget in (widgets or {}).items():
            self.setCellWidget(r, c, widget)
        self.resizeRowsToContents()
        self._fit()

    def rows(self) -> list[tuple[str, ...]]:
        """The displayed cell texts, for tests and reports."""
        return [
            tuple(
                self.item(r, c).text() if self.item(r, c) else "" for c in range(self.columnCount())
            )
            for r in range(self.rowCount())
        ]

    def _fit(self) -> None:
        height = self.horizontalHeader().height() + 2
        for r in range(self.rowCount()):
            height += self.rowHeight(r)
        self.setFixedHeight(height)


class TimelineBar(QWidget):
    """One labelled span row of the plan timeline, painted with palette tokens."""

    def __init__(self, caption: str):
        super().__init__()
        self.caption = caption
        self.extent: tuple[int, int] | None = None
        self.span: tuple[int, int] | None = None
        self.text = ""
        self.style_name = "solid"  # solid, dashed, soft, marker
        self.setFixedHeight(20)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_span(self, extent, span, text: str, style_name: str = "solid") -> None:
        self.extent, self.span, self.text, self.style_name = extent, span, text, style_name
        self.setToolTip(plain_tip(f"{self.caption}: {text}"))
        self.update()

    def paintEvent(self, event):  # noqa: N802 (Qt override)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect().adjusted(0, 2, -1, -3)
        painter.fillRect(rect, theme.token("surface-sunken"))
        if self.extent and self.span and self.extent[1] > self.extent[0]:
            lo, hi = self.extent
            start = max(lo, min(hi, self.span[0]))
            end = max(start, min(hi, self.span[1]))
            x0 = rect.left() + rect.width() * (start - lo) / (hi - lo)
            x1 = rect.left() + rect.width() * (end - lo) / (hi - lo)
            width = max(8.0 if self.style_name == "marker" else 2.0, x1 - x0)
            span_rect = rect.adjusted(0, 0, 0, 0)
            span_rect.setLeft(int(x0))
            span_rect.setWidth(int(width))
            if self.style_name == "marker":
                pen = QPen(theme.token("ink-muted"), 1)
                painter.setPen(pen)
                painter.setBrush(theme.token("surface"))
                painter.drawRect(span_rect.adjusted(0, 0, 0, 0))
            elif self.style_name == "dashed":
                pen = QPen(theme.token("accent"), 1, Qt.PenStyle.DashLine)
                painter.setPen(pen)
                painter.setBrush(theme.token("accent-soft"))
                painter.drawRect(span_rect)
            elif self.style_name == "soft":
                painter.setPen(QPen(theme.token("accent"), 1))
                painter.setBrush(theme.token("accent-soft"))
                painter.drawRect(span_rect)
            else:
                painter.setPen(QPen(theme.token("ink-muted"), 1))
                painter.setBrush(theme.token("surface"))
                painter.drawRect(span_rect)
            painter.setPen(
                theme.token("accent-ink") if self.style_name != "solid" else theme.token("ink")
            )
            metrics = QFontMetrics(painter.font())
            text = metrics.elidedText(
                self.text, Qt.TextElideMode.ElideRight, max(0, span_rect.width() - 6)
            )
            painter.drawText(span_rect, Qt.AlignmentFlag.AlignCenter, text)
        else:
            painter.setPen(theme.token("ink-subtle"))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.text)
        painter.end()


class Page(QWidget):
    """Title and subtitle, a scrolling body, and a footer with a note and action buttons."""

    def __init__(self, title: str):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        self.body = QVBoxLayout(content)
        self.body.setContentsMargins(24, 16, 24, 16)
        self.body.setSpacing(10)
        self.title = label(title, "page-title")
        self.subtitle = label("", "muted", wrap=True)
        self.body.addWidget(self.title)
        self.body.addWidget(self.subtitle)
        self.scroll.setWidget(content)
        outer.addWidget(self.scroll, 1)
        self.footer = QFrame()
        self.footer.setProperty("lattice", "footer")
        footer_layout = QHBoxLayout(self.footer)
        footer_layout.setContentsMargins(24, 12, 24, 12)
        footer_layout.setSpacing(8)
        self.note = label("", "hint", wrap=True)
        footer_layout.addWidget(self.note, 1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        footer_layout.addLayout(self.actions)
        outer.addWidget(self.footer)

        self.primary_button: QPushButton | None = None

    def set_heading(self, title: str, subtitle: str) -> None:
        self.title.setText(title)
        self.subtitle.setText(subtitle)

    def add_action(self, text: str, variant: str = "", slot=None) -> QPushButton:
        widget = button(text, variant, slot)
        widget.setAccessibleName(text)
        self.actions.addWidget(widget)
        if variant == "primary":
            self.primary_button = widget
        return widget

    def focus_primary(self) -> None:
        """Keyboard users land on the page's main action, or its first input."""
        target = self.primary_button
        if target is None or not target.isEnabled() or not target.isVisible():
            target = self.scroll.widget().nextInFocusChain()
        if target is not None:
            target.setFocus(Qt.FocusReason.OtherFocusReason)

    def finish_body(self) -> None:
        self.body.addStretch(1)


def columns(*widgets: QWidget, stretch: Sequence[int] | None = None) -> QWidget:
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(16)
    for index, widget in enumerate(widgets):
        layout.addWidget(widget, (stretch or [1] * len(widgets))[index])
    return holder


def row(*widgets: QWidget, spacing: int = 8) -> QWidget:
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    for widget in widgets:
        layout.addWidget(widget)
    layout.addStretch(1)
    return holder


def field(caption: str, widget: QWidget, hint: QLabel | None = None) -> QWidget:
    """A labelled input: the label is the widget's buddy and its accessible name."""
    holder = QWidget()
    layout = QVBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    caption_label = label(caption, "label")
    caption_label.setBuddy(widget)
    widget.setAccessibleName(caption)
    layout.addWidget(caption_label)
    layout.addWidget(widget)
    if hint is not None:
        layout.addWidget(hint)
    return holder


def elide(widget: QLabel, text: str, width: int = 640) -> None:
    """Show a long path in the middle-elided form, keeping the full text as a tooltip."""
    metrics = QFontMetrics(widget.font())
    widget.setText(metrics.elidedText(text, Qt.TextElideMode.ElideMiddle, width))
    widget.setToolTip(plain_tip(text))
