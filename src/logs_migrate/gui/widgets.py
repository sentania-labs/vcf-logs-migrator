"""Lattice building blocks for the screens: cards, banners, tables, the staged-progress card,
the timeline and the page scaffold with its one-sentence footer."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from html import escape

from PySide6.QtCore import QElapsedTimer, QEvent, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import theme


def repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def set_prop(widget: QWidget, name: str, value) -> None:
    if widget.property(name) != value:
        widget.setProperty(name, value)
        repolish(widget)


def label(
    text: str = "", kind: str = "", status: str = "", wrap: bool = False, rich: bool = False
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
        widget.setMinimumWidth(40)
    widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


def rich(text: str, kind: str = "text", on_link=None, status: str = "") -> QLabel:
    """Wrapped rich text; links call `on_link(href)` and can be reached with the keyboard."""
    widget = label(text, kind, status, wrap=True, rich=True)
    if on_link is not None:
        widget.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByKeyboard
        )
        widget.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        widget.linkActivated.connect(on_link)
    return widget


def set_status(widget: QWidget, status: str) -> None:
    set_prop(widget, "status", status)


def button(text: str, variant: str = "", slot=None) -> QPushButton:
    widget = QPushButton(text)
    if variant:
        widget.setProperty("variant", variant)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    widget.setAccessibleName(text.replace("→", "").replace("←", "").replace("…", "").strip())
    if slot is not None:
        widget.clicked.connect(lambda _checked=False: slot())
    return widget


def link(text: str, slot) -> QPushButton:
    widget = button(text, "", slot)
    widget.setProperty("lattice", "link")
    widget.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    return widget


def badge(text: str, status: str = "") -> QLabel:
    widget = label(text.upper(), "badge", status)
    widget.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    return widget


def badge_html(status: str, text: str) -> str:
    """A badge inside a sentence of rich text."""
    colour = theme.TOKENS[theme.current_theme()]
    ink = colour.get(f"{status}-ink", colour["ink-muted"])
    soft = colour.get(f"{status}-soft", colour["surface-sunken"])
    return (
        f'<span style="background:{soft};color:{ink};font-size:10.5px;font-weight:500">'
        f"&nbsp;{escape(text.upper())}&nbsp;</span>"
    )


def subtle_html(text: str) -> str:
    colour = theme.TOKENS[theme.current_theme()]["ink-subtle"]
    return f'<span style="font-size:12px;color:{colour}">{text}</span>'


def card(
    title: str = "", tag: tuple[str, str] | None = None, note: str = "", edge: str = ""
) -> tuple[QFrame, QVBoxLayout]:
    """A surface with an optional uppercase title, a badge beside it and a plain note."""
    frame = QFrame()
    frame.setProperty("lattice", "card")
    if edge:
        frame.setProperty("edge", edge)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 11, 14, 11)
    layout.setSpacing(6)
    if title:
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(label(title.upper(), "card-title"))
        if tag:
            head.addWidget(badge(tag[1], tag[0]))
        if note:
            head.addWidget(label(note, "card-note"))
        head.addStretch(1)
        layout.addLayout(head)
    return frame, layout


def banner(status: str, title: str, text: str = "", on_link=None) -> QLabel:
    """A coloured strip: one line in larger type, then a plain sentence or two."""
    body = f'<span style="font-size:16px;font-weight:650">{title}</span>'
    if text:
        body += f"<br>{text}"
    widget = rich(body, "banner", on_link, status)
    widget.setAccessibleName(title)
    return widget


def verdict(status: str, symbol: str, title: str, text: str) -> QWidget:
    """The one large statement on a screen: a round mark, a headline and what it means."""
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(14)
    mark = label(symbol, "mark", status if status != "info" else "")
    mark.setFixedSize(44, 44)
    mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(mark, 0, Qt.AlignmentFlag.AlignTop)
    words = QVBoxLayout()
    words.setSpacing(2)
    heading = label(title, "verdict-title", wrap=True)
    words.addWidget(heading)
    words.addWidget(rich(text, "soft"))
    layout.addLayout(words, 1)
    holder.setAccessibleName(title)
    return holder


def facts(items: Sequence[tuple[str, str]]) -> QWidget:
    """Three plain figures side by side."""
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    for key, value in items:
        tile = QFrame()
        tile.setProperty("lattice", "fact")
        inner = QVBoxLayout(tile)
        inner.setContentsMargins(12, 7, 12, 7)
        inner.setSpacing(1)
        inner.addWidget(label(key, "fact-key"))
        inner.addWidget(label(value, "fact-value", wrap=True))
        layout.addWidget(tile, 1)
    return holder


def columns(
    *widgets: QWidget,
    stretch: Sequence[int] | None = None,
    top: bool = False,
    exact: bool = False,
) -> QWidget:
    """Side by side. `exact` holds the widths to the stretch ratio whatever is inside, so a
    narrow window squeezes both columns instead of starving one."""
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(12)
    for index, widget in enumerate(widgets):
        factor = (stretch or [1] * len(widgets))[index]
        if exact:
            widget.setSizePolicy(QSizePolicy.Policy.Ignored, widget.sizePolicy().verticalPolicy())
        if top:
            layout.addWidget(widget, factor, Qt.AlignmentFlag.AlignTop)
        else:
            layout.addWidget(widget, factor)
    return holder


def row(*widgets: QWidget, spacing: int = 8, stretch: bool = True) -> QWidget:
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    for widget in widgets:
        layout.addWidget(widget)
    if stretch:
        layout.addStretch(1)
    return holder


def stack(*widgets: QWidget, spacing: int = 6) -> QWidget:
    holder = QWidget()
    layout = QVBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    for widget in widgets:
        layout.addWidget(widget)
    return holder


def field(caption: str, widget: QWidget) -> tuple[QWidget, QLabel]:
    """A labelled input. Returns the holder and its caption, so the caption can change."""
    holder = QWidget()
    layout = QVBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(3)
    caption_label = label(caption, "label")
    caption_label.setBuddy(widget)
    widget.setAccessibleName(caption)
    layout.addWidget(caption_label)
    layout.addWidget(widget)
    return holder, caption_label


def empty(title: str, text: str, action: QWidget | None = None) -> QFrame:
    """A screen with nothing on it yet says what will appear and what to press."""
    frame, layout = card()
    layout.addStretch(1)
    heading = label(title, "big")
    heading.setAlignment(Qt.AlignmentFlag.AlignHCenter)
    layout.addWidget(heading)
    body = rich(text, "soft")
    body.setAlignment(Qt.AlignmentFlag.AlignHCenter)
    layout.addWidget(body)
    if action is not None:
        layout.addSpacing(6)
        layout.addWidget(action, 0, Qt.AlignmentFlag.AlignHCenter)
    layout.addStretch(1)
    frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    frame.setMinimumHeight(170)
    return frame


class Slot(QWidget):
    """A place whose content is replaced as the state changes. Widgets marked `persistent`
    (a form, the progress card) are set aside and reused; everything else is deleted."""

    persistent = True

    def __init__(self, margins: tuple[int, int, int, int] = (0, 0, 0, 0), spacing: int = 10):
        super().__init__()
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(*margins)
        self.box.setSpacing(spacing)

    def set(self, *widgets: QWidget | None, fill: bool = False) -> None:
        """Without `fill` the content sits at the top."""
        keep = {id(widget) for widget in widgets if widget is not None}
        while self.box.count():
            item = self.box.takeAt(0)
            widget = item.widget()
            if widget is None:
                continue
            if id(widget) in keep:
                continue
            widget.setParent(None)
            if not getattr(widget, "persistent", False):
                widget.deleteLater()
        for widget in widgets:
            if widget is not None:
                self.box.addWidget(widget)
                widget.show()
        if not fill:
            self.box.addStretch(1)
        self.box.invalidate()


class FitArea(QScrollArea):
    """The body of a screen. It is given the height of the window, and its content is asked
    to fit: lists that are long by nature shrink and scroll inside their own box. Only when
    the content cannot shrink any further does the body itself scroll."""

    def __init__(self, content: QWidget):
        super().__init__()
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWidgetResizable(False)
        self.content = content
        self.setWidget(content)
        content.installEventFilter(self)

    def fit(self) -> None:
        width = self.viewport().width()
        layout = self.content.layout()
        need = (
            layout.minimumHeightForWidth(width)
            if layout.hasHeightForWidth()
            else layout.minimumSize().height()
        )
        height = max(self.viewport().height(), need)
        if self.content.width() != width or self.content.height() != height:
            self.content.resize(width, height)

    def resizeEvent(self, event):  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self.fit()

    def eventFilter(self, watched, event):  # noqa: N802 (Qt override)
        if watched is self.content and event.type() == QEvent.Type.LayoutRequest:
            self.fit()
        return super().eventFilter(watched, event)


class GridTable(QWidget):
    """A table of wrapping text. Cells are rich text, `("badge", status, text[, after])`
    or widgets; nothing is cut off and nothing scrolls sideways."""

    def __init__(
        self,
        headers: Sequence[str],
        stretch: Sequence[int] | None = None,
        right: Sequence[int] = (),
        nowrap: Sequence[int] = (0,),
        lead: int = 0,
    ):
        super().__init__()
        self.headers = list(headers)
        self.right = set(right)
        self.nowrap = set(nowrap)
        self.lead = lead
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(0)
        self.grid.setVerticalSpacing(0)
        for column, factor in enumerate(stretch or [1] * len(self.headers)):
            self.grid.setColumnStretch(column, factor)
        self._texts: list[tuple[str, ...]] = []
        self.set_rows([])

    def _align(self, column: int) -> Qt.AlignmentFlag:
        side = Qt.AlignmentFlag.AlignRight if column in self.right else Qt.AlignmentFlag.AlignLeft
        return side | Qt.AlignmentFlag.AlignTop

    def set_rows(self, rows: Sequence[Sequence]) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        for column, text in enumerate(self.headers):
            head = label(text.upper(), "th")
            head.setAlignment(self._align(column))
            self.grid.addWidget(head, 0, column)
        self._texts = []
        for index, cells in enumerate(rows, start=1):
            texts = []
            for column, cell in enumerate(cells):
                if isinstance(cell, QWidget):
                    holder = QWidget()
                    holder.setProperty("lattice", "td")
                    holder.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
                    inner = QHBoxLayout(holder)
                    inner.setContentsMargins(8, 4, 8, 4)
                    inner.addWidget(cell, 0, self._align(column))
                    self.grid.addWidget(holder, index, column)
                    texts.append(cell.accessibleName() or "")
                    continue
                if isinstance(cell, tuple):
                    _tag, status, text, *after = cell
                    html = badge_html(status, text)
                    if after:
                        html += " " + after[0]
                    plain = text + (" " + after[0] if after else "")
                else:
                    html, plain = str(cell), str(cell)
                widget = label(html, "td", wrap=column not in self.nowrap, rich=True)
                widget.setAlignment(self._align(column))
                if column == self.lead:
                    widget.setProperty("lead", "true")
                if column in self.right:
                    widget.setProperty("num", "true")
                self.grid.addWidget(widget, index, column)
                texts.append(plain)
            self._texts.append(tuple(texts))

    def rows(self) -> list[tuple[str, ...]]:
        """The displayed cell texts (markup included), for tests."""
        return list(self._texts)


class ScrollBox(QWidget):
    """A list that is long by nature scrolls inside its own box; the page never does. While
    part of it is out of sight, a strip under the box says so.

    The box asks for the height of its whole list and gives it up when the page is short.
    It places its two children itself: a widget with a layout is sized by that layout, and
    a scroll area's layout would always ask for the list's full height.
    """

    def __init__(self, widget: QWidget, max_height: int = 16777215, what: str = "More"):
        super().__init__()
        self.area = QScrollArea(self)
        self.area.setWidgetResizable(True)
        self.area.setFrameShape(QFrame.Shape.NoFrame)
        self.area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.area.setWidget(widget)
        self.cue = label(f"↓ {what} below. Scroll the list to see the rest.", "more")
        self.cue.setParent(self)
        self.cue.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cue.hide()
        bar = self.area.verticalScrollBar()
        bar.rangeChanged.connect(lambda *_: self._update())
        bar.valueChanged.connect(lambda *_: self._update())
        self.limit = max_height
        self._hinted = -1
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        widget.installEventFilter(self)

    def eventFilter(self, watched, event):  # noqa: N802 (Qt override)
        if event.type() == QEvent.Type.LayoutRequest:
            self.updateGeometry()  # The list grew or shrank; ask for the new height.
        return super().eventFilter(watched, event)

    def natural(self, width: int) -> int:
        """The height of the whole list at this width."""
        inner = self.area.widget()
        height = inner.heightForWidth(width) if width > 0 and inner.hasHeightForWidth() else -1
        if height < 0:
            height = inner.sizeHint().height()
        return min(self.limit, height + 2)

    def sizeHint(self):  # noqa: N802 (Qt override)
        hint = self.area.widget().sizeHint()
        self._hinted = self.natural(self.width())
        hint.setHeight(self._hinted)
        return hint

    def minimumSizeHint(self):  # noqa: N802 (Qt override)
        hint = self.area.widget().minimumSizeHint()
        hint.setHeight(min(96, self.natural(self.width())))
        return hint

    def resizeEvent(self, event):  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self._place()
        if self.natural(self.width()) != self._hinted:
            self.updateGeometry()  # Text wraps differently at this width.

    def _place(self) -> None:
        strip = self.cue.sizeHint().height() if not self.cue.isHidden() else 0
        self.area.setGeometry(0, 0, self.width(), max(0, self.height() - strip))
        self.cue.setGeometry(0, self.height() - strip, self.width(), strip)

    def _update(self) -> None:
        bar = self.area.verticalScrollBar()
        more = bar.maximum() - bar.value() > 4
        if more != (not self.cue.isHidden()):
            self.cue.setVisible(more)
            self._place()

    def hidden(self) -> int:
        """How many pixels of the list are out of sight below."""
        bar = self.area.verticalScrollBar()
        return bar.maximum() - bar.value()


def scrolling(widget: QWidget, max_height: int = 16777215, what: str = "More") -> ScrollBox:
    return ScrollBox(widget, max_height, what)


class Activity(QFrame):
    """Staged progress: the step in hand, how many remain, a moving bar and a timer, so
    waiting never looks like a stall. Shows where it stopped when a step fails."""

    persistent = True

    def __init__(self):
        super().__init__()
        self.setProperty("lattice", "activity")
        self.setAccessibleName("Progress")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 13, 16, 13)
        layout.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(10)
        self.title = label("", "big", wrap=True)
        self.count = label("", "subtle")
        self.cancel = button("Cancel")
        head.addWidget(self.title, 1)
        head.addWidget(self.count)
        head.addWidget(self.cancel)
        layout.addLayout(head)
        self.text = rich("", "text")
        self.text.hide()
        layout.addWidget(self.text)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        self.bar.setAccessibleName("Working")
        layout.addWidget(self.bar)
        self.stages = QVBoxLayout()
        self.stages.setSpacing(3)
        layout.addLayout(self.stages)
        self.note = label("", "subtle", wrap=True)
        layout.addWidget(self.note)
        self.clock = QElapsedTimer()
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._tick)
        self.labels: list[str] = []
        self.index = 0
        self._cancel = None
        self.cancel.clicked.connect(self._cancelled)

    def start(self, labels: Sequence[str], note: str = "", on_cancel=None) -> None:
        self.labels = list(labels)
        self.index = 0
        self._cancel = on_cancel
        self.cancel.setVisible(on_cancel is not None)
        self.cancel.setEnabled(True)
        self.cancel.setText("Cancel")
        self.note.setText(note)
        self.note.setVisible(bool(note))
        set_status(self, "")
        self.text.hide()
        self.bar.show()
        self.clock.start()
        self.timer.start()
        self._render()

    def advance(self, index: int) -> None:
        if index > self.index and index < len(self.labels):
            self.index = index
            self._render()

    def cancelling(self) -> None:
        self.cancel.setEnabled(False)
        self.cancel.setText("Cancelling after this step…")

    def fail(self, title: str, text: str) -> None:
        """Stop where it is and say what happened; the finished steps stay ticked."""
        self.timer.stop()
        set_status(self, "bad")
        self.bar.hide()
        self.cancel.hide()
        self.count.setText("")
        self.title.setText(title)
        self.text.setText(text)
        self.text.show()
        self.note.hide()
        self._render(failed=True)

    def stop(self) -> None:
        self.timer.stop()

    def _cancelled(self) -> None:
        if self._cancel is not None:
            self._cancel()

    def _tick(self) -> None:
        seconds = self.clock.elapsed() // 1000
        self.count.setText(
            f"Step {self.index + 1} of {len(self.labels)} · "
            f"{seconds // 60}:{seconds % 60:02d} so far"
        )

    def _render(self, failed: bool = False) -> None:
        while self.stages.count():
            item = self.stages.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not failed:
            self.title.setText(self.labels[self.index] + "…")
            self._tick()
        for index, text in enumerate(self.labels):
            if index < self.index:
                line = label("✓ " + text, "stage", "done", wrap=True)
            elif index == self.index:
                line = (
                    label("✕ " + text, "stage", "bad", wrap=True)
                    if failed
                    else label("◌ " + text + "…", "stage", "now", wrap=True)
                )
            else:
                line = label("○ " + text, "stage", wrap=True)
            self.stages.addWidget(line)

    def stage_texts(self) -> list[str]:
        return [
            self.stages.itemAt(i).widget().text()
            for i in range(self.stages.count())
            if self.stages.itemAt(i).widget()
        ]


class Timeline(QWidget):
    """One bar for the source's whole history: your dates, what is already on the
    destination, broken copies, and the newest logs that are still being written."""

    def __init__(self):
        super().__init__()
        self.first: date | None = None
        self.today: date | None = None
        self.selected: tuple[date, date] | None = None
        self.had: list[tuple[date, date]] = []
        self.broken: list[tuple[date, date]] = []
        self.newest: date | None = None
        self.setFixedHeight(22)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName("Timeline of the source's logs with your dates highlighted")

    def set(self, first, today, selected, had=(), broken=(), newest=None) -> None:
        self.first, self.today, self.selected = first, today, selected
        self.had, self.broken, self.newest = list(had), list(broken), newest
        self.update()

    def _x(self, day: date, width: float) -> float:
        total = max(1, (self.today - self.first).days + 1)
        return width * min(total, max(0, (day - self.first).days)) / total

    def paintEvent(self, event):  # noqa: N802 (Qt override)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        area = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(theme.token("line"), 1))
        painter.setBrush(theme.token("surface-sunken"))
        painter.drawRoundedRect(area, 4, 4)
        if self.first is None or self.today is None:
            painter.end()
            return
        width = area.width()
        painter.setPen(Qt.PenStyle.NoPen)

        def span(first: date, last: date, colour: QColor, top: float, height: float, least=2.0):
            left = self._x(first, width)
            right = self._x(date.fromordinal(last.toordinal() + 1), width)
            painter.setBrush(colour)
            painter.drawRect(
                QRectF(area.left() + left, area.top() + top, max(least, right - left), height)
            )

        full = area.height()
        if self.selected:
            colour = QColor(theme.token("accent"))
            colour.setAlphaF(0.85)
            span(*self.selected, colour, 0, full, 4.0)
        for first, last in self.had:
            span(first, last, theme.token("ok"), 0, full / 2)
        for first, last in self.broken:
            span(first, last, theme.token("bad"), 0, full, 4.0)
        if self.newest:
            left = min(self._x(self.newest, width), width - 12)
            painter.setBrush(QBrush(theme.token("warn"), Qt.BrushStyle.BDiagPattern))
            painter.drawRect(QRectF(area.left() + left, area.top(), width - left, full))
        painter.end()


def legend(items: Sequence[tuple[str, str]]) -> QWidget:
    """Colour keys for the timeline: `(token, text)`; the token `newest` draws hatching."""
    colours = theme.TOKENS[theme.current_theme()]
    parts = []
    for token, text in items:
        colour = colours["warn"] if token == "newest" else colours[token]
        mark = "▨" if token == "newest" else "■"
        parts.append(f'<span style="color:{colour}">{mark}</span> {escape(text)}')
    return rich(" &nbsp;&nbsp; ".join(parts), "subtle")


class PlanLines(QWidget):
    """The plan as short lines: a mark, a one-word key, the answer, and a smaller note."""

    def __init__(self, lines: Sequence[dict], on_link=None):
        super().__init__()
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(7)
        grid.setColumnStretch(2, 1)
        grid.setColumnMinimumWidth(1, 84)
        self.texts: list[tuple[str, str, str]] = []
        for index, line in enumerate(lines):
            icon = label(line["icon"], "icon", line["kind"])
            key = label(line["key"], "key")
            body = line["text"]
            if line.get("small"):
                colour = theme.TOKENS[theme.current_theme()]["ink-subtle"]
                body += f'<br><span style="font-size:12px;color:{colour}">{line["small"]}</span>'
            text = rich(body, "text", on_link)
            grid.addWidget(icon, index, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(key, index, 1, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(text, index, 2)
            self.texts.append((line["key"], line["text"], line.get("small", "")))


class ProgressCard(QFrame):
    """The live copy: percent, size, a bar, three facts and a row per destination server.
    Figures change in place; nothing here is rebuilt while the copy runs."""

    persistent = True

    def __init__(self):
        super().__init__()
        self.setProperty("lattice", "card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 11, 14, 11)
        layout.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(12)
        self.percent = label("0%", "huge")
        self.amount = label("", "text", rich=True)
        self.live = label("", "subtle", rich=True)
        self.live.setAccessibleName("How fresh these figures are")
        head.addWidget(self.percent, 0, Qt.AlignmentFlag.AlignBaseline)
        head.addWidget(self.amount, 0, Qt.AlignmentFlag.AlignBaseline)
        head.addStretch(1)
        head.addWidget(self.live, 0, Qt.AlignmentFlag.AlignBaseline)
        layout.addLayout(head)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(12)
        self.bar.setAccessibleName("Copy progress")
        layout.addWidget(self.bar)
        self.fact_values: list[tuple[QLabel, QLabel]] = []
        tiles = QHBoxLayout()
        tiles.setSpacing(10)
        for _ in range(3):
            tile = QFrame()
            tile.setProperty("lattice", "fact")
            inner = QVBoxLayout(tile)
            inner.setContentsMargins(12, 7, 12, 7)
            inner.setSpacing(1)
            key, value = label("", "fact-key"), label("", "fact-value")
            inner.addWidget(key)
            inner.addWidget(value)
            tiles.addWidget(tile, 1)
            self.fact_values.append((key, value))
        layout.addLayout(tiles)
        self.servers = QGridLayout()
        self.servers.setHorizontalSpacing(12)
        self.servers.setVerticalSpacing(5)
        self.servers.setColumnStretch(1, 1)
        holder = QWidget()
        holder.setLayout(self.servers)
        # More servers than fit scroll inside the card, never the page.
        self.server_area = QScrollArea()
        self.server_area.setWidgetResizable(True)
        self.server_area.setFrameShape(QFrame.Shape.NoFrame)
        self.server_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.server_area.setWidget(holder)
        layout.addWidget(self.server_area)
        self.server_rows: dict[str, tuple[QProgressBar, QLabel, QLabel]] = {}
        self.latest = label("", "subtle", wrap=True)
        layout.addWidget(self.latest)
        self.updated = QElapsedTimer()
        self.mode = "live"
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

    def show_view(self, view: dict, fresh: bool = True) -> None:
        self.mode = view["mode"]
        self.percent.setText(f"{view['percent']}%")
        self.amount.setText(view["size"])
        self.bar.setValue(int(view["fraction"] * 1000))
        set_prop(self.bar, "still", "false" if view["mode"] == "live" else "true")
        for (key, value), (name, text) in zip(self.fact_values, view["facts"], strict=True):
            key.setText(name)
            value.setText(text)
        names = [server["name"] for server in view["servers"]]
        if names != list(self.server_rows):
            while self.servers.count():
                item = self.servers.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            self.server_rows = {}
            for index, name in enumerate(names):
                bar = QProgressBar()
                bar.setRange(0, 1000)
                bar.setTextVisible(False)
                bar.setFixedHeight(8)
                bar.setAccessibleName(f"Progress on {name}")
                amount, state = label("", "soft"), label("", "badge")
                state.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
                self.servers.addWidget(label(name, "text"), index, 0)
                self.servers.addWidget(bar, index, 1)
                self.servers.addWidget(amount, index, 2)
                self.servers.addWidget(state, index, 3)
                self.server_rows[name] = (bar, amount, state)
        self.server_area.setVisible(bool(names))
        self.server_area.setFixedHeight(min(6, len(names)) * 27)
        for server in view["servers"]:
            bar, amount, state = self.server_rows[server["name"]]
            bar.setValue(int((server["fraction"] or 0) * 1000))
            set_prop(bar, "still", "false" if view["mode"] == "live" else "true")
            amount.setText(server["text"])
            state.setText(server["badge"][1].upper())
            set_status(state, server["badge"][0])
        self.latest.setText(view["latest"])
        self.latest.setVisible(bool(view["latest"]))
        self.lost_at = view.get("lost_at", "")
        if fresh:
            self.updated.start()
        self._tick()

    def _tick(self) -> None:
        seconds = self.updated.elapsed() // 1000 if self.updated.isValid() else 0
        colours = theme.TOKENS[theme.current_theme()]
        if self.mode == "live":
            self.live.setText(
                f'<span style="color:{colours["ok"]}">●</span> Live · last update {seconds} s ago'
            )
        elif self.mode == "quiet":
            self.live.setText(
                f'<span style="color:{colours["warn"]}">●</span> Connected · asked {seconds} s ago'
            )
        else:
            self.live.setText(f"○ As it was at {self.lost_at}, when contact was lost")


class Page(QWidget):
    """A heading, a body and a footer: one sentence on what happens next, then the buttons,
    with the main one last (bottom right)."""

    def __init__(self, app, title: str, subtitle: str = ""):
        super().__init__()
        self.app = app
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        head = QVBoxLayout()
        self.head = head
        head.setContentsMargins(22, 14, 22, 0)
        head.setSpacing(2)
        self.title = label(title, "page-title")
        self.subtitle = label(subtitle, "page-sub", wrap=True)
        head.addWidget(self.title)
        head.addWidget(self.subtitle)
        outer.addLayout(head)
        self.body = Slot((22, 12, 22, 12), 10)
        self.fit_area = FitArea(self.body)
        outer.addWidget(self.fit_area, 1)
        self.activity = Activity()
        self.footer = QFrame()
        self.footer.setProperty("lattice", "footer")
        self.footer.setMinimumHeight(56)
        self.foot = QHBoxLayout(self.footer)
        self.foot.setContentsMargins(22, 9, 22, 9)
        self.foot.setSpacing(12)
        self.hint = rich("", "soft")
        self.hint.setAccessibleName("What happens next")
        self.foot.addWidget(self.hint, 1)
        self.extra = QHBoxLayout()
        self.foot.addLayout(self.extra)
        self.action_row = QHBoxLayout()
        self.action_row.setSpacing(8)
        self.foot.addLayout(self.action_row)
        outer.addWidget(self.footer)
        self.buttons: dict[str, QPushButton] = {}
        self._signature: tuple = ()
        self.primary_button: QPushButton | None = None

    def set_heading(self, title: str, subtitle: str) -> None:
        self.title.setText(title)
        self.subtitle.setText(subtitle)

    def resizeEvent(self, event):  # noqa: N802 (Qt override)
        """A small window gets tighter margins, so every screen still fits without scrolling."""
        super().resizeEvent(event)
        small = self.width() < 900
        side = 16 if small else 22
        self.head.setContentsMargins(side, 9 if small else 14, side, 0)
        self.body.box.setContentsMargins(side, 8 if small else 12, side, 8 if small else 12)
        self.body.box.setSpacing(8 if small else 10)
        self.foot.setContentsMargins(side, 7 if small else 9, side, 7 if small else 9)
        self.compact(small)

    def compact(self, small: bool) -> None:
        """A screen may rearrange itself for a small window."""

    def set_body(self, *widgets: QWidget | None, fill: bool = False) -> None:
        self.body.set(*widgets, fill=fill)

    def set_footer(self, hint: str, actions: Sequence[tuple] = ()) -> None:
        """`actions` are `(text, slot[, variant[, enabled]])`. Buttons are rebuilt only when
        their names change, so a figure that updates never takes a click away."""
        self.hint.setText(hint)
        signature = tuple((action[0], action[2] if len(action) > 2 else "") for action in actions)
        if signature != self._signature:
            while self.action_row.count():
                item = self.action_row.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.setParent(None)
                    widget.deleteLater()
            self.buttons = {}
            self.primary_button = None
            for action in actions:
                text, slot = action[0], action[1]
                variant = action[2] if len(action) > 2 else ""
                widget = button(text, variant, slot)
                widget.setProperty("lattice", "foot")
                self.action_row.addWidget(widget)
                self.buttons[text] = widget
                if variant == "primary":
                    self.primary_button = widget
            self._signature = signature
        for action in actions:
            self.buttons[action[0]].setEnabled(action[3] if len(action) > 3 else True)

    def set_extra(self, widget: QWidget | None) -> None:
        """Something that belongs beside the main button, such as the tick box it waits on."""
        while self.extra.count():
            old = self.extra.takeAt(0).widget()
            if old is not None and old is not widget:
                old.setParent(None)
                old.deleteLater()
        if widget is not None:
            self.extra.addWidget(widget)
            self.hint.setVisible(bool(self.hint.text()))
        else:
            self.hint.setVisible(True)

    def focus_primary(self) -> None:
        """Keyboard users land on the screen's main action, or its first input."""
        target = self.primary_button
        if target is None or not target.isEnabled() or not target.isVisible():
            target = self.body.nextInFocusChain()
        if target is not None:
            target.setFocus(Qt.FocusReason.OtherFocusReason)

    def overflow(self) -> int:
        """How many pixels of body do not fit, for the no-scrolling check."""
        self.fit_area.fit()
        return max(0, self.body.height() - self.fit_area.viewport().height())

    def refresh(self) -> None:  # pragma: no cover - every screen overrides this
        raise NotImplementedError


class Overlay(QWidget):
    """A question asked inside the window: the screen behind is dimmed and waits."""

    closed = Signal()

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 30, 40, 30)
        self.box = QFrame()
        self.box.setProperty("lattice", "modal")
        self.box.setMaximumWidth(680)
        self.inner = QVBoxLayout(self.box)
        self.inner.setContentsMargins(20, 18, 20, 18)
        self.inner.setSpacing(10)
        layout.addWidget(self.box, 0, Qt.AlignmentFlag.AlignCenter)
        self.title = ""
        self.escape = None
        self.buttons: dict[str, QPushButton] = {}
        self.hide()

    def paintEvent(self, event):  # noqa: N802 (Qt override)
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(20, 24, 32, 115))
        painter.end()

    def open(self, title: str, body: Sequence[QWidget], buttons: Sequence[tuple]) -> None:
        """`buttons` are `(text, slot[, variant])`; the first one answers the Escape key."""
        self.clear()
        self.title = title
        self.setAccessibleName(title)
        self.inner.addWidget(label(title, "modal-title", wrap=True))
        for widget in body:
            self.inner.addWidget(widget)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addStretch(1)
        focus = None
        for index, action in enumerate(buttons):
            text, slot = action[0], action[1]
            variant = action[2] if len(action) > 2 else ""
            widget = button(text, variant, slot)
            widget.setProperty("lattice", "foot")
            actions.addWidget(widget)
            self.buttons[text] = widget
            if index == 0:
                self.escape = slot
            if variant == "primary" or focus is None:
                focus = widget
        self.inner.addLayout(actions)
        self.setGeometry(self.parentWidget().rect())
        self.box.setFixedWidth(max(320, min(680, self.parentWidget().width() - 80)))
        self.show()
        self.raise_()
        if focus is not None:
            focus.setFocus(Qt.FocusReason.OtherFocusReason)

    def clear(self) -> None:
        while self.inner.count():
            item = self.inner.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
            elif item.layout():
                while item.layout().count():
                    child = item.layout().takeAt(0)
                    inner = child.widget()
                    if inner is not None:
                        inner.setParent(None)
                        inner.deleteLater()
        self.buttons = {}
        self.escape = None

    def close_box(self) -> None:
        self.hide()
        self.clear()
        self.title = ""
        self.closed.emit()

    def keyPressEvent(self, event):  # noqa: N802 (Qt override)
        if event.key() == Qt.Key.Key_Escape and self.escape is not None:
            self.escape()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):  # noqa: N802 (Qt override)
        event.accept()  # Clicks on the dimmed screen go nowhere.


class Ack(QCheckBox):
    """The tick box beside the button it unlocks."""

    def __init__(self, text: str):
        super().__init__(text)
        self.setProperty("lattice", "ack")
        self.setAccessibleName(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
