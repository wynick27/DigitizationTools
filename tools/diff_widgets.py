"""Reusable Qt text-difference editors used by normal and entry views."""

from __future__ import annotations

import bisect
import re

from PyQt6.QtCore import QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QSyntaxHighlighter, QTextCharFormat, QTextCursor, QTextFormat
from PyQt6.QtWidgets import QApplication, QPlainTextEdit, QTextEdit, QWidget

from tools.diff_engine import DEFAULT_DIFF_ENGINE


def to_qt_pos(text, position):
    return len(str(text or "")[:max(0, int(position))].encode("utf-16-le")) // 2


def to_py_pos(text, position):
    raw = str(text or "").encode("utf-16-le")[:max(0, int(position)) * 2]
    return len(raw.decode("utf-16-le", errors="ignore"))


class SharedDiffHighlighter(QSyntaxHighlighter):
    def __init__(self, document):
        super().__init__(document)
        self.diff_ranges = []
        self.diff_starts = []
        self.regex_pattern = None
        self.regex_group = 0
        self.diff_format = QTextCharFormat()
        self.diff_format.setForeground(QColor("#b42318"))
        self.diff_format.setBackground(QColor("#ffebe9"))
        self.regex_format = QTextCharFormat()
        self.regex_format.setBackground(QColor("#dceeff"))
        self.both_format = QTextCharFormat(self.diff_format)
        self.both_format.setBackground(QColor("#dceeff"))

    def set_diff_data(self, opcodes, is_left):
        text = self.document().toPlainText()
        ranges = []
        for tag, i1, i2, j1, j2 in opcodes or ():
            if tag == "equal":
                continue
            start, end = (i1, i2) if is_left else (j1, j2)
            if start < end:
                ranges.append((to_qt_pos(text, start), to_qt_pos(text, end)))
        self.diff_ranges = sorted(ranges)
        self.diff_starts = [start for start, _end in self.diff_ranges]
        self.rehighlight()

    def set_regex(self, pattern, group=0):
        try:
            self.regex_pattern = re.compile(pattern) if pattern else None
        except re.error:
            self.regex_pattern = None
        self.regex_group = group
        self.rehighlight()

    def highlightBlock(self, text):
        if not text:
            return
        block_start = self.currentBlock().position()
        block_end = block_start + len(text)
        diff = [False] * len(text)
        regex = [False] * len(text)
        if self.diff_ranges:
            end_index = bisect.bisect_right(self.diff_starts, block_end)
            start_index = max(0, bisect.bisect_right(self.diff_starts, block_start) - 1)
            for start, end in self.diff_ranges[start_index:end_index]:
                left = max(start, block_start) - block_start
                right = min(end, block_end) - block_start
                if left < right:
                    diff[left:right] = [True] * (right - left)
        if self.regex_pattern:
            for index, match in enumerate(self.regex_pattern.finditer(text)):
                if index >= 100:
                    break
                try:
                    start, end = match.span(self.regex_group)
                except (IndexError, KeyError):
                    start, end = match.span(0)
                if start < end:
                    regex[start:end] = [True] * (end - start)
        start = 0
        state = (diff[0], regex[0])
        for index in range(1, len(text) + 1):
            next_state = (diff[index], regex[index]) if index < len(text) else None
            if next_state != state:
                if state[0] or state[1]:
                    fmt = self.both_format if all(state) else self.diff_format if state[0] else self.regex_format
                    self.setFormat(start, index - start, fmt)
                start, state = index, next_state


class _LineNumberArea(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):
        return QSize(self.editor.line_number_area_width(), 0)

    def paintEvent(self, event):
        self.editor.paint_line_numbers(event)


class SharedDiffTextEdit(QPlainTextEdit):
    focus_in_signal = pyqtSignal()
    apply_patch_signal = pyqtSignal(tuple, str)
    push_patch_signal = pyqtSignal(tuple, str)
    zoom_signal = pyqtSignal(int)

    def __init__(self, side="left", parent=None):
        super().__init__(parent)
        self.side = side
        self.diff_opcodes = ()
        self.other_text_content = ""
        self.setFont(QFont("Consolas", 11))
        self.setMouseTracking(True)
        self.line_number_area = _LineNumberArea(self)
        self.blockCountChanged.connect(self.update_line_number_area_width)
        self.updateRequest.connect(self.update_line_number_area)
        self.update_line_number_area_width()

    def focusInEvent(self, event):
        self.focus_in_signal.emit()
        super().focusInEvent(event)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom_signal.emit(event.angleDelta().y())
            event.accept()
            return
        super().wheelEvent(event)

    def line_number_area_width(self):
        digits = len(str(max(1, self.blockCount())))
        return 8 + self.fontMetrics().horizontalAdvance("9") * digits

    def update_line_number_area_width(self, _count=0):
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def update_line_number_area(self, rect, dy):
        if dy:
            self.line_number_area.scroll(0, dy)
        else:
            self.line_number_area.update(0, rect.y(), self.line_number_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self.update_line_number_area_width()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        rect = self.contentsRect()
        self.line_number_area.setGeometry(QRect(rect.left(), rect.top(), self.line_number_area_width(), rect.height()))

    def paint_line_numbers(self, event):
        painter = QPainter(self.line_number_area)
        painter.fillRect(event.rect(), QColor("#f3f4f6"))
        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        bottom = top + self.blockBoundingRect(block).height()
        painter.setPen(QColor("#4b5563"))
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.drawText(
                    0, int(top), self.line_number_area.width() - 3,
                    self.fontMetrics().height(), Qt.AlignmentFlag.AlignRight, str(number + 1),
                )
            block = block.next()
            top = bottom
            bottom = top + self.blockBoundingRect(block).height()
            number += 1

    def highlight_line_at_index(self, index):
        cursor = self.textCursor()
        cursor.setPosition(max(0, int(index)))
        selection = QTextEdit.ExtraSelection()
        selection.format.setBackground(QColor("#fff3a3"))
        selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        selection.cursor = cursor
        selection.cursor.clearSelection()
        self.setExtraSelections([selection])

    def clear_highlight(self):
        self.setExtraSelections([])

    def set_diff_data(self, opcodes, other_text):
        self.diff_opcodes = tuple(opcodes or ())
        self.other_text_content = str(other_text or "")

    def get_opcode_at_position(self, point):
        index = to_py_pos(self.toPlainText(), self.cursorForPosition(point).position())
        for opcode in self.diff_opcodes:
            tag, i1, i2, j1, j2 = opcode
            if tag == "equal":
                continue
            start, end = (i1, i2) if self.side == "left" else (j1, j2)
            if start <= index <= end:
                return opcode
        return None

    def mouseMoveEvent(self, event):
        modifiers = QApplication.keyboardModifiers()
        actionable = modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
        self.viewport().setCursor(
            Qt.CursorShape.PointingHandCursor
            if actionable and self.get_opcode_at_position(event.pos())
            else Qt.CursorShape.IBeamCursor
        )
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        modifiers = QApplication.keyboardModifiers()
        if event.button() == Qt.MouseButton.LeftButton and not self.isReadOnly():
            opcode = self.get_opcode_at_position(event.pos())
            if opcode and modifiers & Qt.KeyboardModifier.ControlModifier:
                self._accept_patch(opcode)
                return
            if opcode and modifiers & Qt.KeyboardModifier.AltModifier:
                self._push_patch(opcode)
                return
        super().mousePressEvent(event)

    def _accept_patch(self, opcode):
        _tag, i1, i2, j1, j2 = opcode
        if self.side == "left":
            self.apply_patch_signal.emit((i1, i2), self.other_text_content[j1:j2])
        else:
            self.apply_patch_signal.emit((j1, j2), self.other_text_content[i1:i2])

    def _push_patch(self, opcode):
        _tag, i1, i2, j1, j2 = opcode
        current = self.toPlainText()
        if self.side == "left":
            self.push_patch_signal.emit((j1, j2), current[i1:i2])
        else:
            self.push_patch_signal.emit((i1, i2), current[j1:j2])


class DiffEditorPair(QWidget):
    """A compact reusable pair with diff highlight, patching, scroll and zoom."""

    changed = pyqtSignal()

    def __init__(self, parent=None):
        from PyQt6.QtWidgets import QHBoxLayout, QSplitter
        super().__init__(parent)
        self.left = SharedDiffTextEdit("left")
        self.right = SharedDiffTextEdit("right")
        self.left_highlighter = SharedDiffHighlighter(self.left.document())
        self.right_highlighter = SharedDiffHighlighter(self.right.document())
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.left)
        splitter.addWidget(self.right)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)
        self._syncing = False
        self._ignore_markup = False
        self._modes = ("plain", "plain")
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self.refresh_diff)
        self.left.textChanged.connect(self._text_changed)
        self.right.textChanged.connect(self._text_changed)
        self.left.apply_patch_signal.connect(lambda span, text: self.apply_patch(self.left, span, text))
        self.right.apply_patch_signal.connect(lambda span, text: self.apply_patch(self.right, span, text))
        self.left.push_patch_signal.connect(lambda span, text: self.apply_patch(self.right, span, text))
        self.right.push_patch_signal.connect(lambda span, text: self.apply_patch(self.left, span, text))
        self.left.verticalScrollBar().valueChanged.connect(lambda value: self._sync_scroll(self.left, self.right, value))
        self.right.verticalScrollBar().valueChanged.connect(lambda value: self._sync_scroll(self.right, self.left, value))
        self.left.zoom_signal.connect(self._zoom)
        self.right.zoom_signal.connect(self._zoom)

    def _text_changed(self):
        self._timer.start()
        self.changed.emit()

    def set_options(self, ignore_markup=False, mode_left="plain", mode_right="plain"):
        self._ignore_markup = bool(ignore_markup)
        self._modes = (mode_left or "plain", mode_right or "plain")
        self.refresh_diff()

    def set_texts(self, left, right):
        for editor, value in ((self.left, left), (self.right, right)):
            editor.blockSignals(True)
            editor.setPlainText(value or "")
            editor.blockSignals(False)
        self.refresh_diff()

    def refresh_diff(self):
        result = DEFAULT_DIFF_ENGINE.compare(
            self.left.toPlainText(), self.right.toPlainText(), self._ignore_markup, *self._modes
        )
        self.left.set_diff_data(result.opcodes, self.right.toPlainText())
        self.right.set_diff_data(result.opcodes, self.left.toPlainText())
        self.left_highlighter.set_diff_data(result.opcodes, True)
        self.right_highlighter.set_diff_data(result.opcodes, False)
        self.diff_result = result
        return result

    @staticmethod
    def apply_patch(editor, span, replacement):
        if editor.isReadOnly():
            return
        text = editor.toPlainText()
        cursor = editor.textCursor()
        cursor.setPosition(to_qt_pos(text, span[0]))
        cursor.setPosition(to_qt_pos(text, span[1]), QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(replacement)

    def _sync_scroll(self, source, target, value):
        if self._syncing:
            return
        self._syncing = True
        try:
            source_bar = source.verticalScrollBar()
            target_bar = target.verticalScrollBar()
            ratio = value / max(1, source_bar.maximum())
            target_bar.setValue(round(ratio * target_bar.maximum()))
        finally:
            self._syncing = False

    def _zoom(self, delta):
        for editor in (self.left, self.right):
            editor.zoomIn(1) if delta > 0 else editor.zoomOut(1)