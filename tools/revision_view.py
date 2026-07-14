import difflib

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QKeySequence, QTextCharFormat, QTextCursor, QTextFormat
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


REVISION_GROUP_PROPERTY = int(QTextFormat.Property.UserProperty) + 1
REVISION_TYPE_PROPERTY = int(QTextFormat.Property.UserProperty) + 2


class RevisionEditor(QTextEdit):
    """Editable rich-text surface whose generated revisions carry format metadata."""

    revision_action_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptRichText(False)
        self.setUndoRedoEnabled(True)

    @staticmethod
    def neutral_format():
        fmt = QTextCharFormat()
        fmt.clearProperty(REVISION_GROUP_PROPERTY)
        fmt.clearProperty(REVISION_TYPE_PROPERTY)
        return fmt

    def _prepare_plain_insertion(self):
        self.setCurrentCharFormat(self.neutral_format())

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Paste):
            self._prepare_plain_insertion()
        elif event.text() and not (
            event.modifiers()
            & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
        ):
            self._prepare_plain_insertion()
        super().keyPressEvent(event)

    def insertFromMimeData(self, source):
        self._prepare_plain_insertion()
        self.insertPlainText(source.text())

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            direction = 1 if event.angleDelta().y() > 0 else -1
            font = self.document().defaultFont()
            size = font.pointSizeF()
            if size <= 0:
                size = self.font().pointSizeF()
            font.setPointSizeF(max(6.0, min(72.0, size + direction)))
            self.document().setDefaultFont(font)
            event.accept()
            return
        super().wheelEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            modifiers = event.modifiers()
            action = None
            if modifiers & Qt.KeyboardModifier.ControlModifier:
                action = "accept"
            elif modifiers & Qt.KeyboardModifier.AltModifier:
                action = "reject"
            if action:
                cursor = self.cursorForPosition(event.position().toPoint())
                probe = QTextCursor(cursor)
                probe.movePosition(
                    QTextCursor.MoveOperation.NextCharacter,
                    QTextCursor.MoveMode.KeepAnchor,
                )
                fmt = probe.charFormat()
                if not fmt.property(REVISION_GROUP_PROPERTY) and cursor.position() > 0:
                    probe = QTextCursor(cursor)
                    probe.movePosition(
                        QTextCursor.MoveOperation.PreviousCharacter,
                        QTextCursor.MoveMode.KeepAnchor,
                    )
                    fmt = probe.charFormat()
                if fmt.property(REVISION_GROUP_PROPERTY):
                    self.setTextCursor(probe)
                    self.revision_action_requested.emit(action)
                    event.accept()
                    return
        super().mousePressEvent(event)


class RevisionViewWidget(QWidget):
    """Word-like, editable inline revision view for one page."""

    def __init__(
        self,
        parent,
        left_text,
        right_text,
        apply_callback,
        allow_right_target=True,
        page_num=None,
        navigate_page_callback=None,
        location_callback=None,
        close_callback=None,
    ):
        super().__init__(parent)
        self.apply_callback = apply_callback
        self.left_text = left_text
        self.right_text = right_text
        self.allow_right_target = allow_right_target
        self.page_num = page_num
        self.navigate_page_callback = navigate_page_callback
        self.location_callback = location_callback
        self.close_callback = close_callback
        self._building = False
        self._next_group_id = 1

        self._init_ui()
        self.rebuild_document()

    def _init_ui(self):
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("对比基准:"))
        self.target_combo = QComboBox()
        self.target_combo.addItem("以左侧为基准", "left")
        self.target_combo.addItem("以右侧为基准", "right")
        self.target_combo.currentIndexChanged.connect(self.on_baseline_changed)
        top.addWidget(self.target_combo)

        top.addWidget(QLabel("应用到:"))
        self.apply_combo = QComboBox()
        self.apply_combo.addItem("左侧文本", "left")
        self.apply_combo.addItem("右侧文本", "right")
        if not self.allow_right_target:
            right_item = self.apply_combo.model().item(1)
            if right_item is not None:
                right_item.setEnabled(False)
            self.apply_combo.setItemData(
                1, "OCR 结果只作为参考，不能作为修订保存目标", Qt.ItemDataRole.ToolTipRole
            )
        top.addWidget(self.apply_combo)
        self.summary_label = QLabel()
        top.addWidget(self.summary_label)
        top.addStretch()
        self.btn_apply = QPushButton("应用")
        self.btn_cancel = QPushButton("不应用")
        top.addWidget(self.btn_apply)
        top.addWidget(self.btn_cancel)
        layout.addLayout(top)

        actions = QHBoxLayout()
        self.btn_prev = QPushButton("上一处")
        self.btn_next = QPushButton("下一处")
        self.btn_accept = QPushButton("接受")
        self.btn_reject = QPushButton("拒绝")
        self.btn_accept_all = QPushButton("全部接受")
        self.btn_reject_all = QPushButton("全部拒绝")
        self.btn_undo = QPushButton("撤销")
        self.btn_redo = QPushButton("重做")
        for button in (
            self.btn_prev,
            self.btn_next,
            self.btn_accept,
            self.btn_reject,
            self.btn_accept_all,
            self.btn_reject_all,
            self.btn_undo,
            self.btn_redo,
        ):
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)

        self.editor = RevisionEditor(self)
        self.editor.setPlaceholderText("当前页没有文本。")
        layout.addWidget(self.editor, 1)

        footer = QHBoxLayout()
        self.help_label = QLabel(
            "绿色下划线为新增，红色删除线为删除；Ctrl+点击接受，Alt+点击拒绝。"
        )
        footer.addWidget(self.help_label)
        footer.addStretch()
        layout.addLayout(footer)

        self.btn_prev.clicked.connect(lambda: self.navigate_revision(-1))
        self.btn_next.clicked.connect(lambda: self.navigate_revision(1))
        self.btn_accept.clicked.connect(lambda: self.resolve_current("accept"))
        self.btn_reject.clicked.connect(lambda: self.resolve_current("reject"))
        self.btn_accept_all.clicked.connect(lambda: self.resolve_all("accept"))
        self.btn_reject_all.clicked.connect(lambda: self.resolve_all("reject"))
        self.btn_undo.clicked.connect(self.editor.undo)
        self.btn_redo.clicked.connect(self.editor.redo)
        self.btn_apply.clicked.connect(self.apply_and_close)
        self.btn_cancel.clicked.connect(self.request_close)
        self.editor.document().contentsChanged.connect(self.update_summary)
        self.editor.cursorPositionChanged.connect(self.sync_image_location)
        self.editor.revision_action_requested.connect(self.resolve_current)
        self.editor.undoAvailable.connect(self.btn_undo.setEnabled)
        self.editor.redoAvailable.connect(self.btn_redo.setEnabled)
        self.btn_undo.setEnabled(False)
        self.btn_redo.setEnabled(False)

    def target_side(self):
        return self.target_combo.currentData()

    def apply_side(self):
        return self.apply_combo.currentData()

    def on_baseline_changed(self, _index=None):
        baseline = self.target_side()
        apply_index = self.apply_combo.findData(baseline)
        apply_item = self.apply_combo.model().item(apply_index)
        if apply_index >= 0 and (apply_item is None or apply_item.isEnabled()):
            self.apply_combo.setCurrentIndex(apply_index)
        self.rebuild_document()

    def request_close(self):
        box = QMessageBox(self)
        box.setWindowTitle("关闭修订")
        box.setText("是否保存并应用修订窗口中的更改？")
        box.setInformativeText("未处理的修订将保留对比基准中的内容。")
        save_button = box.addButton("保存并应用", QMessageBox.ButtonRole.AcceptRole)
        discard_button = box.addButton("不保存", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_button:
            return self.apply_current(close=True)
        if clicked is discard_button:
            self.close_without_prompt()
            return True
        return False

    def close_without_prompt(self):
        if self.close_callback:
            self.close_callback()
        else:
            self.close()

    def set_page_content(
        self, page_num, left_text, right_text, allow_right_target=True
    ):
        self.page_num = page_num
        self.left_text = left_text
        self.right_text = right_text
        self.allow_right_target = allow_right_target
        right_item = self.apply_combo.model().item(1)
        if right_item is not None:
            right_item.setEnabled(allow_right_target)
        if not allow_right_target and self.apply_side() == "right":
            self.apply_combo.setCurrentIndex(0)
        self.rebuild_document()

    def navigate_page(self, delta):
        if not self.navigate_page_callback:
            return
        if self.editor.document().isUndoAvailable():
            box = QMessageBox(self)
            box.setWindowTitle("切换页面")
            box.setText("当前页的修订或手工编辑尚未应用。")
            apply_button = box.addButton("应用后翻页", QMessageBox.ButtonRole.AcceptRole)
            discard_button = box.addButton("放弃并翻页", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked is apply_button:
                if not self.apply_current(close=False):
                    return
            elif clicked is not discard_button:
                return

        page_data = self.navigate_page_callback(delta)
        if not page_data:
            return
        self.set_page_content(*page_data)

    def sync_image_location(self):
        if self._building or not self.location_callback:
            return
        display_position = self.editor.textCursor().position()
        target_position = 0
        cursor = QTextCursor(self.editor.document())
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        while not cursor.atEnd() and cursor.position() < display_position:
            cursor.movePosition(
                QTextCursor.MoveOperation.NextCharacter,
                QTextCursor.MoveMode.KeepAnchor,
            )
            if cursor.charFormat().property(REVISION_TYPE_PROPERTY) != "insert":
                target_position += len(cursor.selectedText().replace("\u2029", "\n"))
            cursor.clearSelection()
        self.location_callback(self.page_num, self.target_side(), target_position)

    def rebuild_document(self):
        if not hasattr(self, "editor"):
            return
        if self.editor.document().isUndoAvailable() and not self._building:
            answer = QMessageBox.question(
                self,
                "切换修订目标",
                "切换目标会放弃当前修订窗口中的编辑，是否继续？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.target_combo.blockSignals(True)
                self.target_combo.setCurrentIndex(1 - self.target_combo.currentIndex())
                self.target_combo.blockSignals(False)
                return

        target = self.left_text if self.target_side() == "left" else self.right_text
        source = self.right_text if self.target_side() == "left" else self.left_text
        opcodes = difflib.SequenceMatcher(None, target, source, autojunk=False).get_opcodes()

        self._building = True
        self.editor.blockSignals(True)
        self.editor.clear()
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        self._next_group_id = 1
        for tag, i1, i2, j1, j2 in opcodes:
            if tag == "equal":
                cursor.insertText(target[i1:i2], RevisionEditor.neutral_format())
                continue

            group_id = self._next_group_id
            self._next_group_id += 1
            if tag in ("delete", "replace"):
                cursor.insertText(
                    target[i1:i2], self._revision_format(group_id, "delete")
                )
            if tag in ("insert", "replace"):
                cursor.insertText(
                    source[j1:j2], self._revision_format(group_id, "insert")
                )
        cursor.endEditBlock()
        self.editor.document().clearUndoRedoStacks()
        self.editor.blockSignals(False)
        self._building = False
        self.update_summary()

    @staticmethod
    def _revision_format(group_id, revision_type):
        fmt = QTextCharFormat()
        fmt.setProperty(REVISION_GROUP_PROPERTY, group_id)
        fmt.setProperty(REVISION_TYPE_PROPERTY, revision_type)
        if revision_type == "insert":
            fmt.setForeground(QColor("#08783e"))
            fmt.setBackground(QColor("#e4f6ea"))
            fmt.setFontUnderline(True)
        else:
            fmt.setForeground(QColor("#b42318"))
            fmt.setBackground(QColor("#fde8e7"))
            fmt.setFontStrikeOut(True)
        return fmt

    def _revision_segments(self):
        segments = []
        cursor = QTextCursor(self.editor.document())
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        active = None
        while not cursor.atEnd():
            start = cursor.position()
            cursor.movePosition(
                QTextCursor.MoveOperation.NextCharacter,
                QTextCursor.MoveMode.KeepAnchor,
            )
            fmt = cursor.charFormat()
            group_id = fmt.property(REVISION_GROUP_PROPERTY)
            revision_type = fmt.property(REVISION_TYPE_PROPERTY)
            cursor.clearSelection()
            end = cursor.position()
            key = (group_id, revision_type) if group_id and revision_type else None
            if key and active and active[2:] == key and active[1] == start:
                active = (active[0], end, group_id, revision_type)
                segments[-1] = active
            elif key:
                active = (start, end, group_id, revision_type)
                segments.append(active)
            else:
                active = None
        return segments

    def _current_group_id(self):
        position = self.editor.textCursor().selectionStart()
        for start, end, group_id, _revision_type in self._revision_segments():
            if start <= position < end or (position == end and start < end):
                return group_id
        return None

    def _resolve_group(self, group_id, action, edit_block=True):
        segments = [s for s in self._revision_segments() if s[2] == group_id]
        if not segments:
            return False
        edit_cursor = QTextCursor(self.editor.document())
        if edit_block:
            edit_cursor.beginEditBlock()
        for start, end, _group_id, revision_type in reversed(segments):
            cursor = QTextCursor(self.editor.document())
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            remove = (
                (action == "accept" and revision_type == "delete")
                or (action == "reject" and revision_type == "insert")
            )
            if remove:
                cursor.removeSelectedText()
            else:
                cursor.setCharFormat(RevisionEditor.neutral_format())
        if edit_block:
            edit_cursor.endEditBlock()
        return True

    def resolve_current(self, action):
        group_id = self._current_group_id()
        if group_id is None:
            self.status_message("请先将光标放在一处修订中。")
            return
        group_order = []
        for segment in self._revision_segments():
            if segment[2] not in group_order:
                group_order.append(segment[2])
        group_index = group_order.index(group_id)
        next_groups = group_order[group_index + 1:] + group_order[:group_index]
        if not self._resolve_group(group_id, action):
            return
        self.update_summary()
        for next_group in next_groups:
            if self._select_revision_group(next_group):
                break

    def resolve_all(self, action):
        segments = self._revision_segments()
        if not segments:
            return
        edit_cursor = QTextCursor(self.editor.document())
        self._building = True
        try:
            edit_cursor.beginEditBlock()
            # Work from one immutable snapshot. Reverse order keeps every saved
            # range valid while earlier document positions are removed.
            for start, end, _group_id, revision_type in reversed(segments):
                cursor = QTextCursor(self.editor.document())
                cursor.setPosition(start)
                cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
                remove = (
                    (action == "accept" and revision_type == "delete")
                    or (action == "reject" and revision_type == "insert")
                )
                if remove:
                    cursor.removeSelectedText()
                else:
                    cursor.setCharFormat(RevisionEditor.neutral_format())
            edit_cursor.endEditBlock()
        finally:
            self._building = False
        self.update_summary()

    def navigate_revision(self, direction):
        segments = self._revision_segments()
        if not segments:
            self.status_message("没有待处理的修订。")
            return
        groups = []
        seen = set()
        for segment in segments:
            if segment[2] not in seen:
                groups.append(segment)
                seen.add(segment[2])
        position = self.editor.textCursor().position()
        if direction > 0:
            target = next((item for item in groups if item[0] > position), groups[0])
        else:
            target = next((item for item in reversed(groups) if item[0] < position), groups[-1])
        self._show_revision_segment(target)

    def _select_revision_group(self, group_id):
        target = next(
            (segment for segment in self._revision_segments() if segment[2] == group_id),
            None,
        )
        if target is None:
            return False
        self._show_revision_segment(target)
        return True

    def _show_revision_segment(self, target):
        cursor = self.editor.textCursor()
        cursor.setPosition(target[0])
        cursor.setPosition(target[1], QTextCursor.MoveMode.KeepAnchor)
        self.editor.setTextCursor(cursor)
        self.editor.ensureCursorVisible()
        cursor_rect = self.editor.cursorRect()
        viewport_center = self.editor.viewport().rect().center().y()
        scrollbar = self.editor.verticalScrollBar()
        scrollbar.setValue(
            scrollbar.value() + cursor_rect.center().y() - viewport_center
        )

    def update_summary(self):
        if self._building:
            return
        segments = self._revision_segments()
        groups = {segment[2] for segment in segments}
        insertions = {segment[2] for segment in segments if segment[3] == "insert"}
        deletions = {segment[2] for segment in segments if segment[3] == "delete"}
        self.summary_label.setText(
            f"待处理 {len(groups)} 处（新增 {len(insertions)} / 删除 {len(deletions)}）"
        )
        has_revisions = bool(groups)
        for button in (
            self.btn_prev,
            self.btn_next,
            self.btn_accept,
            self.btn_reject,
            self.btn_accept_all,
            self.btn_reject_all,
        ):
            button.setEnabled(has_revisions)

    def status_message(self, message):
        self.help_label.setText(message)

    def resolved_text(self):
        """Return the result with every still-pending revision accepted."""
        return self._document_text(accept_pending=True)

    def partially_applied_text(self):
        """Return only explicit decisions; unresolved revisions keep the baseline."""
        return self._document_text(accept_pending=False)

    def _document_text(self, accept_pending):
        parts = []
        cursor = QTextCursor(self.editor.document())
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        while not cursor.atEnd():
            cursor.movePosition(
                QTextCursor.MoveOperation.NextCharacter,
                QTextCursor.MoveMode.KeepAnchor,
            )
            fmt = cursor.charFormat()
            revision_type = fmt.property(REVISION_TYPE_PROPERTY)
            include = not revision_type
            if revision_type == "insert":
                include = accept_pending
            elif revision_type == "delete":
                include = not accept_pending
            if include:
                parts.append(cursor.selectedText().replace("\u2029", "\n"))
            cursor.clearSelection()
        return "".join(parts)

    def apply_current(self, close=False):
        result_text = self.partially_applied_text()
        self.apply_callback(self.apply_side(), result_text, self.page_num)
        if self.apply_side() == "left":
            self.left_text = result_text
        else:
            self.right_text = result_text
        self.editor.document().clearUndoRedoStacks()
        if close:
            self.close_without_prompt()
        else:
            self.rebuild_document()
        return True

    def apply_and_close(self):
        self.apply_current(close=True)
