from __future__ import annotations

import difflib
import os

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from tools.entry_sources import JsonEntrySource, MdxEntrySource, load_structured_source
from tools.comparison_sources import KIND_PAGED_TEXT, detect_source_kind
from tools.diff_widgets import DiffEditorPair
from tools.headword_extraction import extract_ocr_items
from tools.headword_rules import language_profile, resolve_ocr_profile, resolve_side_profile
from tools.headword_scope import HeadwordScopeController, SCOPE_CURRENT_PAGE, SCOPE_GLOBAL
from tools.headword_matching import (
    ExtractionResult,
    HeadwordExtractionCache,
    HeadwordFilterRule,
    HeadwordProfile,
    HeadwordProfileError,
    KIND_AMBIGUOUS,
    KIND_DIFF,
    KIND_EXACT,
    KIND_FILTERED,
    KIND_FUZZY,
    KIND_LEFT_ONLY,
    KIND_RIGHT_ONLY,
    compare_headword_items,
    extract_page_headwords,
    read_entry_body,
    replace_entry_body,
)
from tools.headword_compare_tools import parse_page_ranges
from tools.markup_support import apply_visible_text_edit, build_markup_projection


KIND_ALL = "全部"
KIND_PAIRED = "非孤立"
VISIBLE_KINDS = (
    KIND_ALL,
    KIND_EXACT,
    KIND_FILTERED,
    KIND_DIFF,
    KIND_FUZZY,
    KIND_AMBIGUOUS,
    KIND_LEFT_ONLY,
    KIND_RIGHT_ONLY,
    KIND_PAIRED,
)


def _hit_text(hits):
    parts = []
    for hit in hits or ():
        matched = "、".join(hit.matched[:4])
        if len(hit.matched) > 4:
            matched += "…"
        parts.append(f"{hit.rule_name}: {matched}")
    return "; ".join(parts)


class HeadwordTableModel(QAbstractTableModel):
    headers = ("页码", "状态", "左侧原词头", "右侧原词头", "左侧匹配键", "右侧匹配键", "过滤内容")
    colors = {
        KIND_FILTERED: QColor("#fff2b8"),
        KIND_DIFF: QColor("#ffd9d5"),
        KIND_FUZZY: QColor("#ffe2bd"),
        KIND_AMBIGUOUS: QColor("#eadfff"),
        KIND_LEFT_ONLY: QColor("#ffdede"),
        KIND_RIGHT_ONLY: QColor("#dceeff"),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = list(rows)
        self.endResetModel()

    def row_at(self, row):
        return self.rows[row] if 0 <= row < len(self.rows) else None

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.headers[section]
        return super().headerData(section, orientation, role)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return row
        if role == Qt.ItemDataRole.BackgroundRole:
            return self.colors.get(row["kind"])
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        page_text = "/".join(
            str(value)
            for value in dict.fromkeys(
                value
                for value in (row.get("left_page"), row.get("right_page"))
                if value is not None
            )
        )
        values = (
            page_text,
            row["kind"],
            row.get("left", ""),
            row.get("right", ""),
            row.get("left_key", ""),
            row.get("right_key", ""),
            " | ".join(filter(None, (
                _hit_text(row.get("left_hits")),
                _hit_text(row.get("right_hits")),
            ))),
        )
        return values[index.column()]

class HeadwordDiffDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        if index.column() not in (2, 3):
            super().paint(painter, option, index)
            return
        row = index.data(Qt.ItemDataRole.UserRole) or {}
        left, right = row.get("left", ""), row.get("right", "")
        source = left if index.column() == 2 else right
        base = QStyleOptionViewItem(option)
        self.initStyleOption(base, index)
        base.text = ""
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, base, painter, option.widget)
        metrics = option.fontMetrics
        baseline = option.rect.top() + (option.rect.height() + metrics.ascent() - metrics.descent()) // 2
        x = option.rect.left() + 4
        painter.save()
        painter.setClipRect(option.rect)
        selected = bool(option.state & option.state.State_Selected)
        normal_color = option.palette.highlightedText().color() if selected else option.palette.text().color()
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, left, right, autojunk=False).get_opcodes():
            start, end = (i1, i2) if index.column() == 2 else (j1, j2)
            value = source[start:end]
            if not value:
                continue
            width = metrics.horizontalAdvance(value)
            if tag != "equal":
                painter.fillRect(x, option.rect.top() + 2, width, option.rect.height() - 4, QColor("#ffebe9"))
                painter.setPen(QColor("#b42318"))
            else:
                painter.setPen(normal_color)
            painter.drawText(x, baseline, value)
            x += width
            if x > option.rect.right():
                break
        painter.restore()

class HeadwordProfilePanel(QGroupBox):
    changed = pyqtSignal()

    def __init__(self, title, profile, parent=None):
        super().__init__(title, parent)
        self._setting = False
        self._init_ui()
        self.set_profile(profile)

    def _init_ui(self):
        layout = QVBoxLayout(self)
        extract_row = QHBoxLayout()
        extract_row.addWidget(QLabel("模板:"))
        self.language_combo = QComboBox()
        for label, value in (("自定义", "custom"), ("日语", "japanese"), ("英语", "english"), ("法语", "french")):
            self.language_combo.addItem(label, value)
        extract_row.addWidget(self.language_combo)
        extract_row.addWidget(QLabel("提取范围:"))
        self.scope_combo = QComboBox()
        for label, value in (("自动", "auto"), ("段落开头", "paragraph"), ("行首", "line")):
            self.scope_combo.addItem(label, value)
        extract_row.addWidget(self.scope_combo)
        self.line_fallback = QCheckBox("允许行首回退")
        extract_row.addWidget(self.line_fallback)
        extract_row.addWidget(QLabel("正则:"))
        self.regex_edit = QLineEdit()
        extract_row.addWidget(self.regex_edit, 1)
        extract_row.addWidget(QLabel("组:"))
        self.group_spin = QSpinBox(); self.group_spin.setRange(0, 99)
        extract_row.addWidget(self.group_spin)
        layout.addLayout(extract_row)

        ignore_row = QHBoxLayout()
        ignore_row.addWidget(QLabel("忽略行:"))
        self.ignore_edit = QLineEdit(); self.ignore_edit.setPlaceholderText("多个正则用换行分隔")
        ignore_row.addWidget(self.ignore_edit, 1)
        layout.addLayout(ignore_row)

        self.rules = QTableWidget(0, 5)
        self.rules.setHorizontalHeaderLabels(["启用", "类型", "名称", "匹配", "替换为"])
        for column in (0, 1, 2):
            self.rules.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.rules.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.rules.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.rules.setMaximumHeight(126)
        layout.addWidget(self.rules)
        buttons = QHBoxLayout()
        for label, callback in (("添加", self.add_rule), ("删除", self.remove_rule), ("上移", lambda: self.move_rule(-1)), ("下移", lambda: self.move_rule(1))):
            button = QPushButton(label); button.clicked.connect(callback); buttons.addWidget(button)
        buttons.addStretch(); layout.addLayout(buttons)

        fields = QHBoxLayout()
        self.headword_field = QLineEdit(); self.headword_field.setPlaceholderText("JSON词头字段(自动)")
        self.body_field = QLineEdit(); self.body_field.setPlaceholderText("JSON正文字段(自动)")
        self.alias_field = QLineEdit(); self.alias_field.setPlaceholderText("JSON别名字段(自动)")
        for widget in (self.headword_field, self.body_field, self.alias_field): fields.addWidget(widget)
        layout.addLayout(fields)
        self.preview_label = QLabel(); layout.addWidget(self.preview_label)

        self.language_combo.currentIndexChanged.connect(self._language_changed)
        self.scope_combo.currentIndexChanged.connect(self._changed)
        self.line_fallback.toggled.connect(self._changed)
        self.regex_edit.textChanged.connect(self._changed)
        self.group_spin.valueChanged.connect(self._changed)
        self.ignore_edit.textChanged.connect(self._changed)
        self.rules.itemChanged.connect(self._changed)
        self.headword_field.textChanged.connect(self._changed)
        self.body_field.textChanged.connect(self._changed)
        self.alias_field.textChanged.connect(self._changed)

    def _changed(self, *_args):
        if not self._setting:
            self.changed.emit()

    def _language_changed(self, _index):
        if self._setting:
            return
        language = self.language_combo.currentData()
        if language != "custom":
            preset = language_profile(language)
            current = self.profile()
            self.set_profile(HeadwordProfile(
                preset.extract_pattern, preset.group, preset.flags,
                current.ignore_patterns, preset.filters,
                current.json_headword_field, current.json_body_field, current.json_alias_field,
                language, preset.extraction_scope, preset.line_fallback,
            ))
        self.changed.emit()

    def add_rule(self, rule=None):
        rule = rule or HeadwordFilterRule("", "", f"规则 {self.rules.rowCount() + 1}")
        row = self.rules.rowCount(); self.rules.insertRow(row)
        enabled = QTableWidgetItem(); enabled.setFlags(enabled.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        enabled.setCheckState(Qt.CheckState.Checked if rule.enabled else Qt.CheckState.Unchecked)
        self.rules.setItem(row, 0, enabled)
        self.rules.setItem(row, 1, QTableWidgetItem("字面" if getattr(rule, "mode", "regex") == "literal" else "正则"))
        self.rules.setItem(row, 2, QTableWidgetItem(rule.name))
        self.rules.setItem(row, 3, QTableWidgetItem(rule.pattern))
        self.rules.setItem(row, 4, QTableWidgetItem(rule.replacement))
        self.rules.setCurrentCell(row, 3)
        self._changed()

    def remove_rule(self):
        row = self.rules.currentRow()
        if row >= 0:
            self.rules.removeRow(row); self.changed.emit()

    def move_rule(self, direction):
        row, target = self.rules.currentRow(), self.rules.currentRow() + direction
        if row < 0 or target < 0 or target >= self.rules.rowCount(): return
        values = []
        for column in range(5):
            item = self.rules.item(row, column)
            values.append((item.text(), item.checkState()) if item else ("", Qt.CheckState.Unchecked))
        self.rules.removeRow(row); self.rules.insertRow(target)
        for column, (value, state) in enumerate(values):
            item = QTableWidgetItem(value)
            if column == 0:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable); item.setCheckState(state)
            self.rules.setItem(target, column, item)
        self.rules.setCurrentCell(target, 3); self.changed.emit()

    def profile(self):
        rules = []
        for row in range(self.rules.rowCount()):
            value = lambda column: self.rules.item(row, column).text() if self.rules.item(row, column) else ""
            enabled = self.rules.item(row, 0)
            rules.append(HeadwordFilterRule(
                value(3), value(4), value(2),
                enabled=bool(enabled and enabled.checkState() == Qt.CheckState.Checked),
                mode="literal" if value(1) == "字面" else "regex",
            ))
        return HeadwordProfile(
            self.regex_edit.text(), self.group_spin.value(),
            ignore_patterns=tuple(line for line in self.ignore_edit.text().splitlines() if line),
            filters=tuple(rules), json_headword_field=self.headword_field.text().strip(),
            json_body_field=self.body_field.text().strip(), json_alias_field=self.alias_field.text().strip(),
            language=self.language_combo.currentData() or "custom",
            extraction_scope=self.scope_combo.currentData() or "auto",
            line_fallback=self.line_fallback.isChecked(),
        )

    def set_profile(self, profile):
        self._setting = True
        try:
            self.language_combo.setCurrentIndex(max(0, self.language_combo.findData(profile.language)))
            self.scope_combo.setCurrentIndex(max(0, self.scope_combo.findData(profile.extraction_scope)))
            self.line_fallback.setChecked(profile.line_fallback)
            self.regex_edit.setText(profile.extract_pattern)
            self.group_spin.setValue(int(profile.group) if isinstance(profile.group, int) else 0)
            self.ignore_edit.setText("\n".join(profile.ignore_patterns))
            self.rules.setRowCount(0)
            for rule in profile.filters: self.add_rule(rule)
            self.headword_field.setText(profile.json_headword_field)
            self.body_field.setText(profile.json_body_field)
            self.alias_field.setText(profile.json_alias_field)
        finally:
            self._setting = False

class HeadwordViewWorker(QThread):
    progress = pyqtSignal(int, int, str)
    result_ready = pyqtSignal(int, object)
    failed = pyqtSignal(int, str)

    def __init__(self, request_id, cache, left_pages, right_pages, left_profile, right_profile,
                 left_path="", right_path="", left_id="left", right_id="right",
                 scope=SCOPE_GLOBAL, page=None, ocr_items=(), ocr_source_id="", ocr_profiles=None):
        super().__init__()
        self.request_id = request_id
        self.cache = cache
        self.pages = {"left": dict(left_pages), "right": dict(right_pages)}
        self.profiles = {"left": left_profile, "right": right_profile}
        self.paths = {"left": left_path, "right": right_path}
        self.ids = {"left": left_id, "right": right_id}
        self.scope = scope
        self.page = page
        self.ocr_items = tuple(ocr_items or ())
        self.ocr_source_id = ocr_source_id
        self.ocr_profiles = dict(ocr_profiles or self.profiles)

    def _load_side(self, side):
        path = self.paths[side]
        profile = self.profiles[side]
        if path:
            source = load_structured_source(path, profile)
            return ExtractionResult(source.as_headword_items()), source
        return self.cache.extract_pages(self.ids[side], self.pages[side], profile), None

    @staticmethod
    def _keys(items):
        return {value for item in items for value in (item.key, *item.aliases) if value}

    def _page_items(self, side, result, source, other_result, other_source):
        if source is None:
            selection = HeadwordScopeController.select_items(result, self.pages[side], self.page)
            return list(selection.items), selection.reason
        if other_source is None:
            other_side = "right" if side == "left" else "left"
            anchor = HeadwordScopeController.select_items(other_result, self.pages[other_side], self.page)
            selection = HeadwordScopeController.select_items(result, {}, self.page, self._keys(anchor.items))
            return list(selection.items), selection.reason
        if not self.ocr_items:
            return [], "\u4e24\u4fa7\u90fd\u662f\u7ed3\u6784\u5316\u6570\u636e\uff0c\u5f53\u524d\u9875\u9700\u8981 OCR \u8bcd\u5934\u951a\u70b9"
        anchor = extract_ocr_items(
            self.ocr_items, self.ocr_profiles[side], self.page, self.ocr_source_id
        )
        selection = HeadwordScopeController.select_items(
            result, {}, self.page, self._keys(anchor.items)
        )
        return list(selection.items), selection.reason

    def run(self):
        try:
            self.progress.emit(0, 2, "\u6b63\u5728\u89e3\u6790\u5de6\u4fa7\u8bcd\u5934...")
            left_result, left_source = self._load_side("left")
            if self.isInterruptionRequested():
                return
            self.progress.emit(1, 2, "\u6b63\u5728\u89e3\u6790\u53f3\u4fa7\u8bcd\u5934...")
            right_result, right_source = self._load_side("right")
            if self.isInterruptionRequested():
                return
            left_items, right_items = left_result.items, right_result.items
            reasons = []
            if self.scope == SCOPE_CURRENT_PAGE:
                left_items, left_reason = self._page_items(
                    "left", left_result, left_source, right_result, right_source
                )
                right_items, right_reason = self._page_items(
                    "right", right_result, right_source, left_result, left_source
                )
                reasons.extend(filter(None, (left_reason, right_reason)))
            rows = compare_headword_items(left_items, right_items)
            self.result_ready.emit(self.request_id, {
                "rows": rows,
                "left_result": left_result,
                "right_result": right_result,
                "left_source": left_source,
                "right_source": right_source,
                "scope_reasons": tuple(dict.fromkeys(reasons)),
            })
            self.progress.emit(2, 2, "\u8bcd\u5934\u5339\u914d\u5b8c\u6210")
        except Exception as exc:
            self.failed.emit(self.request_id, str(exc))

class HeadwordCompareView(QWidget):
    def __init__(self, mainwindow):
        super().__init__(mainwindow)
        self.mainwindow = mainwindow
        self.cache = HeadwordExtractionCache()
        self.scope_controller = HeadwordScopeController(mainwindow.project_config)
        self.worker = None
        self._retired_workers = []
        self.rows = []
        self.bundle = {}
        self.external_paths = {"left": "", "right": ""}
        self._loading_body = False
        self._active_row = None
        self._init_ui()
        self.load_project_profiles()
        self.scope_controller.set_page(getattr(mainwindow, "current_loaded_page", None))
        self._sync_scope_buttons()
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(350)
        self.preview_timer.timeout.connect(self.update_profile_preview)
        self.left_panel.changed.connect(lambda: self.preview_timer.start())
        self.right_panel.changed.connect(lambda: self.preview_timer.start())
        self.update_profile_preview()
        self._update_source_labels()

    def _sync_scope_buttons(self):
        self.current_scope_button.setChecked(
            self.scope_controller.scope == SCOPE_CURRENT_PAGE
        )
        self.global_scope_button.setChecked(
            self.scope_controller.scope == SCOPE_GLOBAL
        )
        self.page_filter.setVisible(self.scope_controller.scope == SCOPE_GLOBAL)

    def set_scope(self, scope):
        self._capture_active_drafts()
        if self.scope_controller.set_scope(scope):
            self._sync_scope_buttons()
            self.mainwindow.config_manager.save()
            self.refresh()

    def page_changed(self, page):
        changed = self.scope_controller.set_page(page)
        if changed and self.scope_controller.scope == SCOPE_CURRENT_PAGE and self.isVisible():
            self.refresh()

    def _project_path(self, side):
        key = "text_path_left" if side == "left" else "text_path_right"
        return self.mainwindow.project_config.get(key, "")

    def _effective_path(self, side):
        path = self.external_paths[side] or self._project_path(side)
        if path and detect_source_kind(path) != KIND_PAGED_TEXT:
            return path
        return ""

    def _update_source_labels(self):
        for side, label in (("left", self.left_path_label), ("right", self.right_path_label)):
            path = self._effective_path(side) or self._project_path(side)
            prefix = "" if self._effective_path(side) else "分页文本: "
            label.setText(prefix + (path or "未配置"))
    def _fallback_profile(self, side):
        return HeadwordProfile(
            self.mainwindow.project_config.get(f"regex_{side}", ""),
            self.mainwindow.project_config.get(f"regex_group_{side}", 0),
        )

    def load_project_profiles(self):
        self.left_panel.set_profile(resolve_side_profile(self.mainwindow.project_config, "left"))
        self.right_panel.set_profile(resolve_side_profile(self.mainwindow.project_config, "right"))
    def _init_ui(self):
        layout = QVBoxLayout(self)
        left_source_row = QHBoxLayout()
        left_source_row.addWidget(QLabel("左侧数据源:"))
        self.left_path_label = QLabel("当前左侧分页文本")
        self.left_path_label.setMinimumWidth(0)
        left_source_row.addWidget(self.left_path_label, 1)
        self.left_format_combo = QComboBox()
        self._fill_format_combo(self.left_format_combo, "left")
        left_source_row.addWidget(self.left_format_combo)
        left_load = QPushButton("加载数据源")
        left_load.clicked.connect(lambda: self.choose_source("left"))
        left_source_row.addWidget(left_load)
        left_reset = QPushButton("使用当前文本")
        left_reset.clicked.connect(lambda: self.reset_source("left"))
        left_source_row.addWidget(left_reset)
        layout.addLayout(left_source_row)

        right_source_row = QHBoxLayout()
        right_source_row.addWidget(QLabel("右侧数据源:"))
        self.right_path_label = QLabel("当前右侧分页文本")
        self.right_path_label.setMinimumWidth(0)
        right_source_row.addWidget(self.right_path_label, 1)
        self.right_format_combo = QComboBox()
        self._fill_format_combo(self.right_format_combo, "right")
        right_source_row.addWidget(self.right_format_combo)
        right_load = QPushButton("加载数据源")
        right_load.clicked.connect(lambda: self.choose_source("right"))
        right_source_row.addWidget(right_load)
        right_reset = QPushButton("使用当前文本")
        right_reset.clicked.connect(lambda: self.reset_source("right"))
        right_source_row.addWidget(right_reset)
        layout.addLayout(right_source_row)

        self.profiles_widget = QSplitter(Qt.Orientation.Horizontal)
        self.left_panel = HeadwordProfilePanel("左侧词头配置", HeadwordProfile(""))
        self.right_panel = HeadwordProfilePanel("右侧词头配置", HeadwordProfile(""))
        self.profiles_widget.addWidget(self.left_panel)
        self.profiles_widget.addWidget(self.right_panel)
        self.profiles_widget.setSizes([1, 1])
        self.profiles_widget.setMaximumHeight(300)
        self.profiles_widget.setVisible(False)
        layout.addWidget(self.profiles_widget)

        commands = QHBoxLayout()
        self.scope_group = QButtonGroup(self)
        self.scope_group.setExclusive(True)
        self.current_scope_button = QPushButton("当前页")
        self.global_scope_button = QPushButton("全局")
        for button, scope in (
            (self.current_scope_button, SCOPE_CURRENT_PAGE),
            (self.global_scope_button, SCOPE_GLOBAL),
        ):
            button.setCheckable(True)
            self.scope_group.addButton(button)
            button.clicked.connect(lambda _checked=False, value=scope: self.set_scope(value))
            commands.addWidget(button)
        commands.addSpacing(8)
        self.profile_toggle = QPushButton("词头配置")
        self.profile_toggle.setCheckable(True)
        self.profile_toggle.setToolTip("显示或隐藏左右词头正则与过滤规则")
        self.profile_toggle.toggled.connect(self.profiles_widget.setVisible)
        commands.addWidget(self.profile_toggle)
        copy_profile = QPushButton("复制左侧规则到右侧")
        copy_profile.clicked.connect(lambda: self.right_panel.set_profile(self.left_panel.profile()))
        commands.addWidget(copy_profile)
        self.calculate_button = QPushButton("计算匹配")
        self.calculate_button.clicked.connect(self.refresh)
        commands.addWidget(self.calculate_button)
        save_profile = QPushButton("保存配置")
        save_profile.clicked.connect(self.save_profiles)
        commands.addWidget(save_profile)
        layout.addLayout(commands)

        filters = QHBoxLayout()
        filters.addWidget(QLabel("页码:"))
        self.page_filter = QLineEdit()
        self.page_filter.setPlaceholderText("全部，或 1-4,8")
        self.page_filter.setMaximumWidth(150)
        self.page_filter.textChanged.connect(self.apply_filter)
        filters.addWidget(self.page_filter)
        self.kind_filter = QComboBox()
        self.kind_filter.addItems(VISIBLE_KINDS)
        self.kind_filter.currentIndexChanged.connect(self.apply_filter)
        filters.addWidget(self.kind_filter)
        self.text_filter = QLineEdit()
        self.text_filter.setPlaceholderText("搜索原词头、匹配键或过滤内容")
        self.text_filter.textChanged.connect(self.apply_filter)
        filters.addWidget(self.text_filter, 1)
        layout.addLayout(filters)

        content_splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableView()
        self.table_model = HeadwordTableModel(self.table)
        self.table.setModel(self.table_model)
        self.table.setItemDelegate(HeadwordDiffDelegate(self.table))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.selectionModel().selectionChanged.connect(
            lambda _selected, _deselected: self.load_selected_bodies()
        )
        self.table.doubleClicked.connect(self.on_double_click)
        content_splitter.addWidget(self.table)

        body_widget = QWidget()
        body_layout = QVBoxLayout(body_widget)
        body_commands = QHBoxLayout()
        copy_lr = QPushButton("正文左 → 右")
        copy_lr.clicked.connect(lambda: self.copy_selected("left", "right"))
        body_commands.addWidget(copy_lr)
        copy_rl = QPushButton("正文右 → 左")
        copy_rl.clicked.connect(lambda: self.copy_selected("right", "left"))
        body_commands.addWidget(copy_rl)
        self.save_left_button = QPushButton("保存左侧结构化数据")
        self.save_left_button.clicked.connect(lambda: self.save_structured("left"))
        body_commands.addWidget(self.save_left_button)
        self.save_right_button = QPushButton("保存右侧结构化数据")
        self.save_right_button.clicked.connect(lambda: self.save_structured("right"))
        body_commands.addWidget(self.save_right_button)
        body_commands.addStretch()
        body_layout.addLayout(body_commands)

        body_options = QHBoxLayout()
        self.ignore_body_markup = QCheckBox("忽略标记比较")
        self.ignore_body_markup.setChecked(bool(self.mainwindow.project_config.get("headword_ignore_markup", False)))
        self.ignore_body_markup.toggled.connect(self._update_body_diff_options)
        body_options.addWidget(self.ignore_body_markup)
        self.render_body_check = QPushButton("源码")
        self.render_body_check.setCheckable(True)
        self.render_body_check.toggled.connect(self.on_render_body_toggled)
        body_options.addWidget(self.render_body_check)
        body_options.addStretch()
        body_layout.addLayout(body_options)

        self.body_display_stack = QStackedWidget()
        self.body_pair = DiffEditorPair(self)
        self.left_body = self.body_pair.left
        self.right_body = self.body_pair.right
        self.body_pair.changed.connect(self._capture_active_drafts)
        self.body_display_stack.addWidget(self.body_pair)

        previews = QSplitter(Qt.Orientation.Horizontal)
        self.left_body_preview = QTextEdit()
        self.left_body_preview.setReadOnly(True)
        self.right_body_preview = QTextEdit()
        self.right_body_preview.setReadOnly(True)
        previews.addWidget(self.left_body_preview)
        previews.addWidget(self.right_body_preview)
        self.body_display_stack.addWidget(previews)
        body_layout.addWidget(self.body_display_stack)

        apply_row = QHBoxLayout()
        apply_left = QPushButton("应用正文到左侧")
        apply_left.clicked.connect(lambda: self.apply_body_editor("left"))
        apply_right = QPushButton("应用正文到右侧")
        apply_right.clicked.connect(lambda: self.apply_body_editor("right"))
        apply_row.addWidget(apply_left)
        apply_row.addWidget(apply_right)
        body_layout.addLayout(apply_row)
        content_splitter.addWidget(body_widget)
        content_splitter.setSizes([520, 260])
        layout.addWidget(content_splitter, 1)

        self.status_label = QLabel()
        layout.addWidget(self.status_label)

    def _fill_format_combo(self, combo, side):
        combo.addItem("纯文本", "plain")
        combo.addItem("Markdown", "markdown")
        combo.addItem("HTML", "html")
        try:
            mode = self.mainwindow.markup_mode(side)
        except Exception:
            mode = "plain"
        index = combo.findData(mode)
        combo.setCurrentIndex(max(0, index))
        combo.currentIndexChanged.connect(self.load_selected_bodies)

    def _body_mode(self, side):
        combo = self.left_format_combo if side == "left" else self.right_format_combo
        return combo.currentData() or "plain"

    @staticmethod
    def _markup_error_text(projection):
        if not projection.errors:
            return ""
        error = projection.errors[0]
        return getattr(error, "message", str(error))

    def _render_body(self, side, source):
        projection = build_markup_projection(source, self._body_mode(side))
        preview = self.left_body_preview if side == "left" else self.right_body_preview
        preview.setHtml(projection.rendered_html)
        return projection

    def _update_body_diff_options(self, *_args):
        self.mainwindow.project_config["headword_ignore_markup"] = self.ignore_body_markup.isChecked()
        if not hasattr(self, "body_pair"):
            return
        self.body_pair.set_options(
            self.ignore_body_markup.isChecked(),
            self._body_mode("left"),
            self._body_mode("right"),
        )

    def on_render_body_toggled(self, checked):
        self._capture_active_drafts()
        self.render_body_check.setText("渲染" if checked else "源码")
        self.body_display_stack.setCurrentIndex(1 if checked else 0)
        if checked:
            self._render_body("left", self.left_body.toPlainText())
            self._render_body("right", self.right_body.toPlainText())

    def _synchronized_body(self, source_side, target_side, source_body, target_body):
        source_projection = build_markup_projection(source_body, self._body_mode(source_side))
        target_projection = build_markup_projection(target_body, self._body_mode(target_side))
        source_error = self._markup_error_text(source_projection)
        target_error = self._markup_error_text(target_projection)
        if source_error or target_error:
            raise ValueError(source_error or target_error)
        if target_projection.visible_text:
            score = difflib.SequenceMatcher(
                None,
                source_projection.visible_text,
                target_projection.visible_text,
                autojunk=False,
            ).ratio()
            if score < 0.25:
                raise ValueError(f"正文可见文本相似度过低 ({score:.1%})")
        if self._body_mode(target_side) == "plain":
            candidate = source_projection.visible_text
        else:
            candidate = apply_visible_text_edit(
                target_body, target_projection, source_projection.visible_text
            )
        checked = build_markup_projection(candidate, self._body_mode(target_side))
        error = self._markup_error_text(checked)
        if error:
            raise ValueError(error)
        return candidate
    def choose_source(self, side):
        path, _selected = QFileDialog.getOpenFileName(
            self,
            "Load entry source",
            "",
            "Supported Sources (*.txt *.md *.markdown *.html *.htm *.mdx *.json);;"
            "Text and Markup (*.txt *.md *.markdown *.html *.htm);;"
            "Structured Sources (*.mdx *.json);;All Files (*)",
        )
        if not path:
            return
        self.external_paths[side] = path
        if path.lower().endswith((".mdx", ".mdx.txt")):
            combo = self.left_format_combo if side == "left" else self.right_format_combo
            combo.setCurrentIndex(combo.findData("html"))
        self._update_source_labels()
        self.update_profile_preview()
        self.refresh()

    def reset_source(self, side):
        self.external_paths[side] = ""
        combo = self.left_format_combo if side == "left" else self.right_format_combo
        combo.setCurrentIndex(max(0, combo.findData(self.mainwindow.markup_mode(side))))
        self._update_source_labels()
        self.update_profile_preview()
        self.refresh()
    def save_profiles(self):
        self.mainwindow.project_config["headword_profiles"] = {
            "left": self.left_panel.profile().to_dict(),
            "right": self.right_panel.profile().to_dict(),
        }
        self.mainwindow.project_config["headword_view_scope"] = self.scope_controller.scope
        self.mainwindow.config_manager.save()
        self.mainwindow.statusBar().showMessage("词头提取与过滤配置已保存。", 5000)

    def update_profile_preview(self):
        page = self.mainwindow.current_loaded_page
        for side, panel, pages in (
            ("left", self.left_panel, self.mainwindow.pages_left),
            ("right", self.right_panel, self.mainwindow.pages_right_text),
        ):
            path = self._effective_path(side)
            if path:
                panel.preview_label.setText(f"结构化数据: {os.path.basename(path)}；计算时检查字段和冲突")
                continue
            sample = pages.get(page, "") if page in pages else next(iter(pages.values()), "")
            try:
                result = extract_page_headwords(sample, panel.profile(), page)
                panel.preview_label.setText(
                    f"当前样本: {len(result.items)} 个词头；空键 {result.empty_keys}；冲突 {len(result.collisions)}"
                )
            except HeadwordProfileError as exc:
                panel.preview_label.setText(str(exc))

    def refresh(self):
        if self.worker and self.worker.isRunning():
            retired = self.worker
            retired.requestInterruption()
            self._retired_workers.append(retired)
            retired.finished.connect(lambda worker=retired: self._release_retired_worker(worker))
        self._capture_active_drafts()
        self.mainwindow.save_current_page_data()
        request_id = self.scope_controller.new_request()
        self.calculate_button.setEnabled(False)
        self.mainwindow.start_background_progress("词头匹配")
        source_data = self.mainwindow.combo_source.currentData() if hasattr(self.mainwindow, "combo_source") else None
        self.worker = HeadwordViewWorker(
            request_id,
            self.cache,
            self.mainwindow.pages_left,
            self.mainwindow.pages_right_text,
            self.left_panel.profile(),
            self.right_panel.profile(),
            self._effective_path("left"),
            self._effective_path("right"),
            self._project_path("left") or "left",
            self._project_path("right") or "right",
            self.scope_controller.scope,
            getattr(self.mainwindow, "current_loaded_page", None),
            getattr(self.mainwindow, "current_ocr_data", ()) or (),
            str(source_data or ""),
            {
                "left": resolve_ocr_profile(
                    self.mainwindow.project_config, source_data, self.left_panel.profile()
                ),
                "right": resolve_ocr_profile(
                    self.mainwindow.project_config, source_data, self.right_panel.profile()
                ),
            },
        )
        self.worker.progress.connect(self.mainwindow.update_background_progress)
        self.worker.result_ready.connect(self.on_result)
        self.worker.failed.connect(self.on_failed)
        self.worker.finished.connect(self.on_finished)
        self.worker.start()

    def on_result(self, request_id, bundle):
        if not self.scope_controller.accepts(request_id):
            return
        self.bundle = bundle
        self.rows = bundle["rows"]
        self.left_panel.preview_label.setText(self._result_summary(bundle["left_result"]))
        self.right_panel.preview_label.setText(self._result_summary(bundle["right_result"]))
        self.apply_filter()
        if bundle.get("scope_reasons"):
            self.status_label.setText("; ".join(bundle["scope_reasons"]))
    @staticmethod
    def _result_summary(result):
        return (
            f"词头 {len(result.items)}；未匹配行 {result.unmatched_lines}；"
            f"空键 {result.empty_keys}；冲突键 {len(result.collisions)}"
        )

    def on_failed(self, request_id, message):
        if not self.scope_controller.accepts(request_id):
            return
        self.status_label.setText(message)
        self.mainwindow.statusBar().showMessage(f"词头匹配失败: {message}", 8000)

    def _release_retired_worker(self, worker):
        if worker in self._retired_workers:
            self._retired_workers.remove(worker)
        worker.deleteLater()
    def on_finished(self):
        self.calculate_button.setEnabled(True)
        self.mainwindow.finish_background_progress("词头匹配完成")
        self.worker = None

    def apply_filter(self):
        try:
            pages = parse_page_ranges(self.page_filter.text())
            page_error = ""
        except ValueError as exc:
            pages = set()
            page_error = f"页码范围错误: {exc}"
        kind = self.kind_filter.currentText()
        needle = self.text_filter.text().strip().casefold()
        visible = []
        for row in self.rows:
            row_pages = {row.get("left_page"), row.get("right_page")} - {None}
            if pages is not None and not (row_pages & pages):
                continue
            if kind == KIND_PAIRED:
                if not row.get("left_item") or not row.get("right_item"):
                    continue
            elif kind != KIND_ALL and row["kind"] != kind:
                continue
            haystack = " ".join((
                row.get("left", ""), row.get("right", ""),
                row.get("left_key", ""), row.get("right_key", ""),
                _hit_text(row.get("left_hits")), _hit_text(row.get("right_hits")),
            )).casefold()
            if needle and needle not in haystack:
                continue
            visible.append(row)

        self.table_model.set_rows(visible)
        self.status_label.setText(page_error or f"显示 {len(visible)} / {len(self.rows)} 条")

    def selected_rows(self):
        return [
            self.table_model.row_at(index.row())
            for index in self.table.selectionModel().selectedRows()
            if self.table_model.row_at(index.row()) is not None
        ]

    def _side_state(self, side):
        result = self.bundle.get(f"{side}_result")
        source = self.bundle.get(f"{side}_source")
        pages = self.mainwindow.pages_left if side == "left" else self.mainwindow.pages_right_text
        return result, source, pages

    def _body_for(self, side, item):
        if item is None:
            return ""
        result, source, pages = self._side_state(side)
        body = source.get_body(item.order) if source is not None else read_entry_body(result.items, item, pages)
        return self.scope_controller.get_draft(side, item, body)

    def _capture_active_drafts(self):
        if self._loading_body or not self._active_row:
            return
        for side, editor in (("left", self.left_body), ("right", self.right_body)):
            item = self._active_row.get(f"{side}_item")
            if item is not None:
                self.scope_controller.put_draft(side, item, editor.toPlainText())

    def load_selected_bodies(self):
        self._capture_active_drafts()
        rows = self.selected_rows()
        row = rows[0] if rows else None
        self._active_row = row
        self._loading_body = True
        try:
            left_text = self._body_for("left", row.get("left_item") if row else None)
            right_text = self._body_for("right", row.get("right_item") if row else None)
            self.body_pair.set_options(
                self.ignore_body_markup.isChecked(), self._body_mode("left"), self._body_mode("right")
            )
            self.body_pair.set_texts(left_text, right_text)
            if self.render_body_check.isChecked():
                self._render_body("left", left_text)
                self._render_body("right", right_text)
        finally:
            self._loading_body = False
    def _write_body(self, side, item, body):
        result, source, pages = self._side_state(side)
        if source is not None:
            source.update_body(item.order, body)
            return set()
        changed = replace_entry_body(result.items, item, pages, body)
        for page in changed:
            self.mainwindow.mark_page_dirty(page, side == "left")
        return changed

    def apply_body_editor(self, side):
        rows = self.selected_rows()
        if len(rows) != 1:
            self.mainwindow.statusBar().showMessage("请选择一个词条后再应用正文。", 5000)
            return
        row = rows[0]
        item = row.get(f"{side}_item")
        if item is None:
            return
        text = self.left_body.toPlainText() if side == "left" else self.right_body.toPlainText()
        try:
            projection = build_markup_projection(text, self._body_mode(side))
            error = self._markup_error_text(projection)
            if error:
                raise ValueError(error)
            changed = self._write_body(side, item, text)
            self.scope_controller.discard_draft(side, item)
        except Exception as exc:
            self.mainwindow.statusBar().showMessage(f"正文应用失败: {exc}", 7000)
            return
        if changed and self.mainwindow.current_loaded_page in changed:
            self.mainwindow.load_current_page()
        self.mainwindow.statusBar().showMessage("词条正文已应用到内存，保存时写入数据源。", 5000)
        if changed:
            QTimer.singleShot(0, self.refresh)

    def copy_selected(self, source_side, target_side):
        rows = self.selected_rows()
        if not rows:
            return
        applied = 0
        skipped = 0
        last_error = ""
        changed_pages = set()
        self.mainwindow.save_current_page_data()
        rows.sort(
            key=lambda row: getattr(row.get(f"{target_side}_item"), "order", -1),
            reverse=True,
        )
        for row in rows:
            source_item = row.get(f"{source_side}_item")
            target_item = row.get(f"{target_side}_item")
            if not source_item or not target_item or not row.get("sync_allowed"):
                skipped += 1
                continue
            try:
                source_body = self._body_for(source_side, source_item)
                target_body = self._body_for(target_side, target_item)
                body = self._synchronized_body(
                    source_side, target_side, source_body, target_body
                )
                changed_pages.update(self._write_body(target_side, target_item, body))
                applied += 1
            except Exception as exc:
                last_error = str(exc)
                skipped += 1
        if changed_pages and self.mainwindow.current_loaded_page in changed_pages:
            self.mainwindow.load_current_page()
        detail = f"；最近原因：{last_error}" if last_error else ""
        self.mainwindow.statusBar().showMessage(
            f"已同步 {applied} 条；跳过 {skipped} 条歧义、格式错误或低相似度条目{detail}。", 7000
        )
        self.load_selected_bodies()
        if changed_pages:
            QTimer.singleShot(0, self.refresh)

    def save_structured(self, side):
        source = self.bundle.get(f"{side}_source")
        if source is None:
            self.mainwindow.statusBar().showMessage("该侧使用分页文本，请使用 Ctrl+S 保存当前侧。", 5000)
            return False
        try:
            if isinstance(source, JsonEntrySource):
                target = source.save()
                message = f"JSON 已保存: {target}"
            elif isinstance(source, MdxEntrySource):
                if source.path.lower().endswith(".mdx.txt"):
                    target = source.save_source(source.path)
                    message = f"MDX 文本源已保存: {target}"
                else:
                    stem = os.path.splitext(source.path)[0]
                    source_path = stem + "_edited.mdx.txt"
                    target = stem + "_edited.mdx"
                    source.save_source(source_path)
                    source.rebuild(source_path, target)
                    message = f"MDX 编辑源和新词典已保存: {target}"
            else:
                return False
            self.mainwindow.statusBar().showMessage(message, 7000)
            return True
        except Exception as exc:
            self.mainwindow.statusBar().showMessage(f"结构化数据保存失败: {exc}", 8000)
            return False
    def on_double_click(self, index):
        row = self.table_model.row_at(index.row())
        if not row:
            return
        preferred = "right" if index.column() in (3, 5) else "left"
        if row.get(f"{preferred}_page") is None:
            preferred = "left" if preferred == "right" else "right"
        self.mainwindow.goto_headword(row, preferred)

    def save_current_side(self):
        from PyQt6.QtWidgets import QApplication

        focus = QApplication.focusWidget()
        side = "right" if focus is self.right_body else "left"
        if self.selected_rows():
            self.apply_body_editor(side)
        if self.bundle.get(f"{side}_source") is not None:
            self.save_structured(side)
            return True
        return (
            self.mainwindow.save_right_data()
            if side == "right"
            else self.mainwindow.save_left_data()
        )

    def project_reloaded(self):
        if self.worker and self.worker.isRunning():
            self.worker.requestInterruption()
            self.worker.wait(1000)
        self.cache.clear()
        self.rows = []
        self.bundle = {}
        self.external_paths = {"left": "", "right": ""}
        self.scope_controller.clear_project_state(self.mainwindow.project_config)
        self._sync_scope_buttons()
        self.load_project_profiles()
        self._update_source_labels()
        self.update_profile_preview()
        self.apply_filter()
    def shutdown(self):
        workers = list(self._retired_workers)
        if self.worker is not None:
            workers.append(self.worker)
        for worker in workers:
            if worker.isRunning():
                worker.requestInterruption()
                worker.wait(1500)
        self._retired_workers.clear()
        return True
