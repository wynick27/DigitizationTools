from __future__ import annotations

import copy
import os
import re
import uuid

from PyQt6.QtCore import QSize, Qt, QTimer
from PyQt6.QtGui import QIcon, QImage, QKeySequence, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListView, QListWidget,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QSplitter,
    QTextBrowser, QToolButton, QVBoxLayout, QWidget,
)

from .matching import (
    LoadedOcrMapper, OcrMatchWorker, match_entries_global, match_entry,
)
from .models import CropSegment, NamingPolicy, ReviewItem, ReviewMode
from ocr.ocr_engines import (
    canonical_engine_id,
    discover_ocr_results,
    sort_ocr_results_by_priority,
)
from .preview import ImagePreviewView
from .service import ImageReviewService, OverrideStore
from .sources import (
    DictionarySliceSource, ImageAuditSource,
    default_override_path,
)


class ImageReviewWorkspace(QWidget):
    def __init__(self, main_window, mode, parent=None):
        super().__init__(parent)
        self.main_window = main_window
        self.mode = ReviewMode(mode)
        self.items = []
        self.items_by_id = {}
        self.image_items = []
        self.image_items_by_id = {}
        self.entries = []
        self.entry_source = None
        self.entry_side = ""
        self.entry_drafts = {}
        self.baselines = {}
        self.image_baselines = {}
        self.source_text = ""
        self.source_path = ""
        self.store = None
        self.image_store = None
        self.current_item_id = ""
        self._segment_owners = {}
        self._selection_extension = None
        self._updating_controls = False
        self._active = False
        self._drawing_new_record = False
        self._match_request = 0
        self.match_worker = None
        self._selected_ocr_identity = None
        self.service = ImageReviewService(self._load_page_image, main_window.project_config)
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(90)
        self.preview_timer.timeout.connect(self.refresh_preview)
        self._build_ui()
        self._build_shortcuts()
        self.reload_sources()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(3)
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(4, 2, 4, 2)
        self.tool_group = QButtonGroup(self)
        self.tool_group.setExclusive(True)
        self.btn_pan = self._mode_button("平移", "pan")
        self.btn_select = self._mode_button("选择", "select")
        self.btn_draw = self._mode_button("新增框", "draw")
        for button in (self.btn_pan, self.btn_select, self.btn_draw):
            self.tool_group.addButton(button)
            toolbar.addWidget(button)
        self.btn_select.setChecked(True)
        self.tool_group.buttonClicked.connect(self._edit_mode_changed)
        self.btn_undo = QToolButton()
        self.btn_undo.setText("撤销")
        self.btn_undo.clicked.connect(self.undo)
        self.btn_redo = QToolButton()
        self.btn_redo.setText("重做")
        self.btn_redo.clicked.connect(self.redo)
        toolbar.addWidget(self.btn_undo)
        toolbar.addWidget(self.btn_redo)
        toolbar.addWidget(QLabel("数据源:"))
        self.source_combo = QComboBox()
        self.source_combo.setMinimumWidth(220)
        self.source_combo.currentIndexChanged.connect(self.load_source)
        toolbar.addWidget(self.source_combo, 1)
        toolbar.addWidget(QLabel("命名:"))
        self.naming_combo = QComboBox()
        self.naming_combo.addItem("保持原名", NamingPolicy.KEEP.value)
        self.naming_combo.addItem("页码 + 序号", NamingPolicy.PAGE_SEQUENCE.value)
        self.naming_combo.addItem("页码 + 新 bbox", NamingPolicy.PAGE_BBOX.value)
        self.naming_combo.currentIndexChanged.connect(self.naming_changed)
        toolbar.addWidget(self.naming_combo)
        self.btn_discard = QPushButton("放弃当前")
        self.btn_discard.clicked.connect(self.discard_current)
        self.btn_apply_current = QPushButton("应用当前")
        self.btn_apply_current.clicked.connect(self.apply_current)
        self.btn_apply_all = QPushButton("全部应用")
        self.btn_apply_all.clicked.connect(self.apply_all)
        for button in (self.btn_discard, self.btn_apply_current, self.btn_apply_all):
            toolbar.addWidget(button)
        root.addLayout(toolbar)

        self.ocr_source_combo = None
        self.btn_replace_from_ocr = None
        self.btn_replace_page_from_ocr = None
        self.bbox_display_group = None
        if self.mode == ReviewMode.DICTIONARY_SLICES:
            ocr_toolbar = QHBoxLayout()
            ocr_toolbar.setContentsMargins(4, 0, 4, 2)
            ocr_toolbar.addWidget(QLabel("OCR 数据源:"))
            self.ocr_source_combo = QComboBox()
            self.ocr_source_combo.setMinimumWidth(220)
            self.ocr_source_combo.currentIndexChanged.connect(
                self.ocr_source_changed
            )
            ocr_toolbar.addWidget(self.ocr_source_combo, 1)
            self.btn_replace_from_ocr = QPushButton("覆盖当前词条")
            self.btn_replace_from_ocr.clicked.connect(
                self.replace_current_from_selected_ocr
            )
            ocr_toolbar.addWidget(self.btn_replace_from_ocr)
            self.btn_replace_page_from_ocr = QPushButton("覆盖整页")
            self.btn_replace_page_from_ocr.clicked.connect(
                self.replace_page_from_selected_ocr
            )
            ocr_toolbar.addWidget(self.btn_replace_page_from_ocr)
            ocr_toolbar.addSpacing(10)
            ocr_toolbar.addWidget(QLabel("矩形显示:"))
            self.bbox_display_group = QButtonGroup(self)
            self.bbox_display_group.setExclusive(True)
            saved_display = str(
                self.main_window.project_config.get(
                    "slice_bbox_display_mode", "selected"
                )
            )
            for label, mode in (("选中词条", "selected"), ("整页词条", "page")):
                button = QToolButton()
                button.setText(label)
                button.setCheckable(True)
                button.setProperty("bbox_display_mode", mode)
                button.setChecked(saved_display == mode)
                self.bbox_display_group.addButton(button)
                ocr_toolbar.addWidget(button)
            if self.bbox_display_group.checkedButton() is None:
                self.bbox_display_group.buttons()[0].setChecked(True)
            self.bbox_display_group.buttonClicked.connect(
                self.bbox_display_mode_changed
            )
            root.addLayout(ocr_toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter, 1)
        middle = QWidget()
        middle_layout = QVBoxLayout(middle)
        middle_layout.setContentsMargins(4, 2, 4, 2)
        middle_layout.setSpacing(4)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(
            "搜索图片、文件名或词条"
            if self.mode == ReviewMode.MARKDOWN_IMAGES else "搜索词条或匹配状态"
        )
        self.search_edit.textChanged.connect(self.apply_filter)
        middle_layout.addWidget(self.search_edit)
        middle_layout.addWidget(QLabel(
            "本页图片" if self.mode == ReviewMode.MARKDOWN_IMAGES else "词条与 OCR 匹配"
        ))
        self.item_list = QListWidget()
        self.item_list.currentItemChanged.connect(self.current_row_changed)
        self.item_list.itemDoubleClicked.connect(self.locate_item_on_canvas)
        if self.mode == ReviewMode.MARKDOWN_IMAGES:
            self._configure_gallery(self.item_list, 132)
        middle_layout.addWidget(self.item_list, 2)

        self.entry_search = self.entry_combo = self.caption_edit = self.ignore_check = None
        if self.mode == ReviewMode.MARKDOWN_IMAGES:
            self.entry_search = QLineEdit()
            self.entry_search.setPlaceholderText("输入词头并回车定位归属词条")
            self.entry_search.returnPressed.connect(self.find_entry)
            middle_layout.addWidget(self.entry_search)
            self.entry_combo = QComboBox()
            self.entry_combo.setEditable(True)
            self.entry_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            self.entry_combo.setMaxVisibleItems(24)
            self.entry_combo.currentIndexChanged.connect(self.assignment_changed)
            middle_layout.addWidget(self.entry_combo)
            middle_layout.addWidget(QLabel("图片说明文字"))
            self.caption_edit = QPlainTextEdit()
            self.caption_edit.setMaximumHeight(78)
            self.caption_edit.textChanged.connect(self.caption_changed)
            middle_layout.addWidget(self.caption_edit)
            image_actions = QHBoxLayout()
            self.ignore_check = QCheckBox("不进入词典")
            self.ignore_check.toggled.connect(self.ignore_changed)
            self.btn_replace = QPushButton("替换当前图片")
            self.btn_replace.clicked.connect(self.replace_current)
            self.btn_add_crop = QPushButton("新增裁图")
            self.btn_add_crop.clicked.connect(self.start_add_crop)
            image_actions.addWidget(self.ignore_check)
            image_actions.addStretch()
            image_actions.addWidget(self.btn_replace)
            image_actions.addWidget(self.btn_add_crop)
            middle_layout.addLayout(image_actions)

        middle_layout.addWidget(QLabel(
            "本词条图片" if self.mode == ReviewMode.MARKDOWN_IMAGES else "词条附属图片"
        ))
        self.entry_gallery = QListWidget()
        self._configure_gallery(self.entry_gallery, 118)
        self.entry_gallery.setMovement(QListView.Movement.Snap)
        self.entry_gallery.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.entry_gallery.currentItemChanged.connect(self.entry_image_selected)
        self.entry_gallery.model().rowsMoved.connect(self.entry_image_order_changed)
        middle_layout.addWidget(self.entry_gallery, 1)
        middle_layout.addWidget(QLabel(
            "当前图片裁框" if self.mode == ReviewMode.MARKDOWN_IMAGES else "OCR 文字片段"
        ))
        self.segment_list = QListWidget()
        self.segment_list.setMaximumHeight(120)
        self.segment_list.currentRowChanged.connect(self.segment_row_changed)
        self.segment_list.itemDoubleClicked.connect(
            self.locate_segment_on_canvas
        )
        middle_layout.addWidget(self.segment_list)
        actions = QHBoxLayout()
        self.btn_segment_up = QPushButton("上移")
        self.btn_segment_down = QPushButton("下移")
        self.btn_segment_delete = QPushButton("删除框")
        self.btn_segment_up.clicked.connect(lambda: self.move_segment(-1))
        self.btn_segment_down.clicked.connect(lambda: self.move_segment(1))
        self.btn_segment_delete.clicked.connect(self.delete_selected_segment)
        for button in (self.btn_segment_up, self.btn_segment_down, self.btn_segment_delete):
            actions.addWidget(button)
        self.btn_assign_previous = self.btn_assign_next = None
        if self.mode == ReviewMode.DICTIONARY_SLICES:
            self.btn_assign_previous = QPushButton("分给上一词条")
            self.btn_assign_next = QPushButton("分给下一词条")
            self.btn_assign_previous.clicked.connect(
                lambda: self.transfer_selected_segment(-1)
            )
            self.btn_assign_next.clicked.connect(
                lambda: self.transfer_selected_segment(1)
            )
            actions.addWidget(self.btn_assign_previous)
            actions.addWidget(self.btn_assign_next)
        middle_layout.addLayout(actions)
        bbox_form = QFormLayout()
        bbox_row = QHBoxLayout()
        self.bbox_spins = []
        for label in ("x1", "y1", "x2", "y2"):
            spin = QDoubleSpinBox()
            spin.setRange(0, 100000)
            spin.setDecimals(1)
            spin.setPrefix(label + " ")
            spin.editingFinished.connect(self.bbox_value_changed)
            self.bbox_spins.append(spin)
            bbox_row.addWidget(spin)
        bbox_form.addRow("原图坐标:", bbox_row)
        middle_layout.addLayout(bbox_form)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        middle_layout.addWidget(self.status_label)
        splitter.addWidget(middle)

        preview_container = QWidget()
        preview_layout = QVBoxLayout(preview_container)
        preview_layout.setContentsMargins(4, 2, 4, 2)
        preview_header = QHBoxLayout()
        preview_header.addWidget(QLabel(
            "当前图片完整预览"
            if self.mode == ReviewMode.MARKDOWN_IMAGES else "图文综合版词条预览"
        ))
        preview_header.addStretch()
        if self.mode == ReviewMode.MARKDOWN_IMAGES:
            minus = QToolButton()
            minus.setText("-")
            minus.clicked.connect(lambda: self.preview.zoom_by(1 / 1.2))
            plus = QToolButton()
            plus.setText("+")
            plus.clicked.connect(lambda: self.preview.zoom_by(1.2))
            actual = QPushButton("实际大小")
            actual.clicked.connect(lambda: self.preview.actual_size())
            fit = QPushButton("适合窗口")
            fit.clicked.connect(lambda: self.preview.fit_image())
            for widget in (minus, plus, actual, fit):
                preview_header.addWidget(widget)
        preview_layout.addLayout(preview_header)
        self.preview = (
            ImagePreviewView()
            if self.mode == ReviewMode.MARKDOWN_IMAGES else QTextBrowser()
        )
        if isinstance(self.preview, QTextBrowser):
            self.preview.setOpenExternalLinks(False)
        preview_layout.addWidget(self.preview, 1)
        splitter.addWidget(preview_container)
        splitter.setSizes([420, 720])

    @staticmethod
    def _configure_gallery(widget, height):
        widget.setViewMode(QListView.ViewMode.IconMode)
        widget.setIconSize(QSize(118, 78))
        widget.setGridSize(QSize(138, height - 4))
        widget.setResizeMode(QListView.ResizeMode.Adjust)
        widget.setMovement(QListView.Movement.Static)
        widget.setWordWrap(True)
        widget.setMaximumHeight(height)

    @staticmethod
    def _mode_button(text, mode):
        button = QToolButton()
        button.setText(text)
        button.setCheckable(True)
        button.setProperty("edit_mode", mode)
        return button

    def _build_shortcuts(self):
        self.undo_shortcut = QShortcut(QKeySequence.StandardKey.Undo, self)
        self.redo_shortcut = QShortcut(QKeySequence.StandardKey.Redo, self)
        self.undo_shortcut.activated.connect(self.undo)
        self.redo_shortcut.activated.connect(self.redo)
        self.delete_shortcut = QShortcut(QKeySequence("Delete"), self)
        self.delete_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.delete_shortcut.activated.connect(self.delete_selected_segment)


    def activate(self):
        self._active = True
        self.show()
        if not self.items:
            self.load_source()
        if self.mode == ReviewMode.MARKDOWN_IMAGES:
            self.refresh_page_gallery(self.main_window.current_loaded_page)
        self.bind_extension()
        return True

    def request_deactivate(self):
        self.cancel_matching()
        dirty = any(item.dirty for item in self.items + self.image_items)
        if dirty:
            answer = QMessageBox.question(
                self, "未应用的图片调整", "当前视图还有未应用的调整。是否全部应用？",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if answer == QMessageBox.StandardButton.Cancel:
                return False
            if answer == QMessageBox.StandardButton.Save and not self.apply_all():
                return False
            if answer == QMessageBox.StandardButton.Discard:
                self.load_source()
        self._active = False
        self.hide()
        return True

    def shutdown(self):
        return self.request_deactivate()

    def project_reloaded(self):
        self.service.project_config = self.main_window.project_config
        self.reload_sources()

    def reload_sources(self):
        selected = self.source_combo.currentData() if self.source_combo.count() else None
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        project = self.main_window.project_config
        for label, path, side in (
            ("左侧", project.get("text_path_left", ""), "left"),
            ("右侧", self.main_window.get_current_right_text_path(), "right"),
        ):
            if path and os.path.isfile(path):
                self.source_combo.addItem(
                    f"{label}: {os.path.basename(path)}", {"path": path, "side": side}
                )
        if selected:
            for index in range(self.source_combo.count()):
                if self.source_combo.itemData(index) == selected:
                    self.source_combo.setCurrentIndex(index)
                    break
        self.source_combo.blockSignals(False)
        self.load_source()

    def load_source(self, _index=None):
        self.cancel_matching()
        source = self.source_combo.currentData()
        self.items = []
        self.items_by_id = {}
        self.image_items = []
        self.image_items_by_id = {}
        self.entries = []
        self.entry_source = None
        self.entry_side = ""
        self.entry_drafts = {}
        self.baselines = {}
        self.image_baselines = {}
        self.source_text = ""
        self.source_path = ""
        if not isinstance(source, dict):
            self.status_label.setText("当前项目没有可用的数据源")
            return
        self.source_path = str(source.get("path") or "")
        side = str(source.get("side") or "left")
        self.entry_side = side
        self.entries = self._load_entries(side, self.main_window.current_loaded_page)
        audit = ImageAuditSource(
            self.source_path, self.main_window.project_config, side
        )
        self.image_items = audit.scan()
        self.image_items_by_id = {item.item_id: item for item in self.image_items}
        markup_mode = str(
            self.main_window.project_config.get(f"markup_mode_{side}") or "plain"
        ).lower()
        if (
            markup_mode in {"markdown", "html"}
            or self.source_path.lower().endswith((".md", ".markdown"))
        ):
            self.source_text = audit.text
            if not self.source_text:
                with open(self.source_path, "r", encoding="utf-8-sig") as stream:
                    self.source_text = stream.read()
        image_override = audit.override_path or default_override_path(
            self.source_path, ReviewMode.MARKDOWN_IMAGES,
            self.main_window.project_config, side,
        )
        self.image_store = OverrideStore(image_override)
        self.image_store.load()
        if audit.map_path and not os.path.isfile(image_override):
            self.image_store.legacy_mode = True
        self.image_store.apply_to(self.image_items)
        if self.mode == ReviewMode.MARKDOWN_IMAGES:
            self.items, self.store = self.image_items, self.image_store
        else:
            self.items = self.entries
            self.store = OverrideStore(default_override_path(
                self.source_path, self.mode, self.main_window.project_config, side
            ))
            self.store.load()
            self.store.apply_to(self.items)
        self.items_by_id = {item.item_id: item for item in self.items}
        self.baselines = {item.item_id: copy.deepcopy(item) for item in self.items}
        self.image_baselines = {
            item.item_id: copy.deepcopy(item) for item in self.image_items
        }
        self._populate_entry_combo()
        self.populate_items()
        if self.mode == ReviewMode.MARKDOWN_IMAGES:
            self.refresh_page_gallery(self.main_window.current_loaded_page)
        else:
            self.refresh_ocr_sources(self.main_window.current_loaded_page)
            if self.items:
                self.item_list.setCurrentRow(self._nearest_entry_row())
                self.start_matching()
        self.status_label.setText(
            f"已加载 {len(self.items)} 项，附属图片 {len(self.image_items)} 张"
        )

    def _load_entries(self, side, page=None):
        page = int(page or self.main_window.current_loaded_page or 0)
        project = self.main_window.project_config
        pages = (
            self.main_window.pages_left
            if side == "left" else self.main_window.pages_right_text
        )
        registry = getattr(self.main_window, "comparison_sources", None)
        adapter = (
            registry.left if registry is not None and side == "left"
            else registry.right_source() if registry is not None else None
        )
        if self.entry_source is None:
            self.entry_source = DictionarySliceSource(
                pages, project.get(f"regex_{side}", ""),
                project.get(f"regex_group_{side}", 0),
                self.source_path, side, project, adapter,
            )

        anchors = ()
        from tools.comparison_sources import detect_source_kind, KIND_PAGED_TEXT
        if detect_source_kind(self.source_path) != KIND_PAGED_TEXT:
            other = "right" if side == "left" else "left"
            other_path = (
                self.main_window.get_current_right_text_path()
                if other == "right" else str(project.get("text_path_left") or "")
            )
            other_pages = (
                self.main_window.pages_right_text
                if other == "right" else self.main_window.pages_left
            )
            if other_path and detect_source_kind(other_path) == KIND_PAGED_TEXT:
                other_adapter = (
                    registry.left if registry is not None and other == "left"
                    else registry.right_source() if registry is not None else None
                )
                anchor_source = DictionarySliceSource(
                    other_pages, project.get(f"regex_{other}", ""),
                    project.get(f"regex_group_{other}", 0),
                    other_path, other, project, other_adapter,
                )
                anchors = tuple(item.label for item in anchor_source.scan_page(page))
            elif getattr(self.main_window, "current_ocr_data", None):
                from tools.headword_extraction import extract_ocr_items
                from tools.headword_rules import HeadwordProfile

                profile = HeadwordProfile.from_dict(
                    project.get(f"headword_profile_{side}") or {},
                    project.get(f"regex_{side}", ""),
                    project.get(f"regex_group_{side}", 0),
                )
                result = extract_ocr_items(
                    self.main_window.current_ocr_data, profile, page, "image-review"
                )
                anchors = tuple(item.raw for item in result.items)

        return self.entry_source.scan_page(page, anchors)

    def _reload_dictionary_page(self, page):
        self.cancel_matching()
        self.refresh_ocr_sources(page)
        for item in self.items:
            self.entry_drafts[item.item_id] = item

        fresh_items = self._load_entries(self.entry_side, page)
        if self.store is not None:
            self.store.apply_to(fresh_items)

        merged = []
        for item in fresh_items:
            self.baselines.setdefault(item.item_id, copy.deepcopy(item))
            merged.append(self.entry_drafts.get(item.item_id, item))

        previous_id = self.current_item_id
        self.entries = merged
        self.items = merged
        self.items_by_id = {item.item_id: item for item in merged}
        self.current_item_id = ""
        self.populate_items()

        selected_id = previous_id if previous_id in self.items_by_id else ""
        if not selected_id and self.items:
            selected_id = self.items[0].item_id
        if selected_id:
            self._select_list_item(selected_id)
        else:
            self.segment_list.clear()
            self.entry_gallery.clear()
            self.preview.clear()
            self.bind_extension()

        if self.items:
            self.start_matching()
        self.status_label.setText(f"本页词条 {len(self.items)} 项")

    def _select_list_item(self, item_id):
        for row in range(self.item_list.count()):
            widget_item = self.item_list.item(row)
            if widget_item.data(Qt.ItemDataRole.UserRole) == item_id:
                self.item_list.setCurrentItem(widget_item)
                return
        self.select_item(item_id)

    def _nearest_entry_row(self):
        page = int(self.main_window.current_loaded_page or 0)
        return next((
            index for index, item in enumerate(self.items)
            if page in (item.metadata.get("pages") or ())
        ), 0)

    def _populate_entry_combo(self):
        if self.entry_combo is None:
            return
        self.entry_combo.blockSignals(True)
        self.entry_combo.clear()
        self.entry_combo.addItem("（未匹配）", "")
        for entry in self.entries:
            self.entry_combo.addItem(entry.label, entry.item_id)
        self.entry_combo.blockSignals(False)

    def populate_items(self):
        if self.mode == ReviewMode.MARKDOWN_IMAGES:
            return
        selected_id = self.current_item_id
        self.item_list.blockSignals(True)
        self.item_list.clear()
        for item in self.items:
            row = QListWidgetItem(self._row_text(item))
            row.setData(Qt.ItemDataRole.UserRole, item.item_id)
            row.setToolTip(item.error or str(item.metadata.get("text") or "")[:240])
            self.item_list.addItem(row)
            if item.item_id == selected_id:
                self.item_list.setCurrentItem(row)
        self.item_list.blockSignals(False)
        self.apply_filter(self.search_edit.text())

    def refresh_page_gallery(self, page):
        if self.mode != ReviewMode.MARKDOWN_IMAGES:
            return
        selected_id = self.current_item_id
        self.item_list.blockSignals(True)
        self.item_list.clear()
        page_items = sorted(
            (item for item in self.image_items if item.page == int(page or 0)),
            key=lambda item: (item.sequence, item.original_name),
        )
        for item in page_items:
            row = self._gallery_row(item, True)
            self.item_list.addItem(row)
            if item.item_id == selected_id:
                self.item_list.setCurrentItem(row)
        self.item_list.blockSignals(False)
        self.apply_filter(self.search_edit.text())
        if self.item_list.currentItem() is None and self.item_list.count():
            self.item_list.setCurrentRow(0)
        if not self.item_list.count():
            self.current_item_id = ""
            self.segment_list.clear()
            self.entry_gallery.clear()
            self.refresh_preview()

    def _gallery_row(self, item, include_headword=False):
        title = item.original_name or item.label
        if include_headword:
            title = f"{item.metadata.get('headword') or '（未匹配）'}\n{title}"
        row = QListWidgetItem(self._thumbnail_icon(item), title)
        row.setData(Qt.ItemDataRole.UserRole, item.item_id)
        row.setToolTip(
            f"P{item.page}  {item.original_name}\n{item.metadata.get('reason') or ''}"
        )
        if item.ignored:
            row.setForeground(Qt.GlobalColor.gray)
        return row

    def _thumbnail_icon(self, item):
        local_path = str(item.metadata.get("local_path") or "")
        image = QImage(local_path) if local_path and os.path.isfile(local_path) else QImage()
        if image.isNull() and item.segments:
            image = self.service.compose(item)
        if image.isNull():
            return QIcon()
        pixmap = QPixmap.fromImage(image).scaled(
            118, 78, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        return QIcon(pixmap)

    @staticmethod
    def _row_text(item):
        dirty = " *" if item.dirty else ""
        pages = item.metadata.get("pages") or ()
        page = f"P{pages[0]}" if pages else (f"P{item.page}" if item.page else "P?")
        return f"{page}  {item.label}  [{item.status}]{dirty}"

    def apply_filter(self, text):
        needle = (text or "").strip().casefold()
        for row in range(self.item_list.count()):
            item = self.item_list.item(row)
            item.setHidden(bool(needle and needle not in item.text().casefold()))

    def current_item(self):
        return self.items_by_id.get(self.current_item_id)

    def current_row_changed(self, current, _previous=None):
        if current is not None:
            self.select_item(str(current.data(Qt.ItemDataRole.UserRole) or ""))

    def select_item(self, item_id):
        self.current_item_id = str(item_id or "")
        item = self.current_item()
        if item is None:
            return
        self._apply_legacy_box(item)
        current_page = int(self.main_window.current_loaded_page or 0)
        pages = tuple(item.metadata.get("pages") or ())
        target_page = (
            current_page
            if self.mode == ReviewMode.DICTIONARY_SLICES and current_page in pages
            else (item.ordered_segments()[0].page if item.segments else item.page)
        )
        if self._active and target_page > 0 and target_page != self.main_window.current_loaded_page:
            self.main_window.goto_page(target_page)
        self._update_item_controls(item)
        self.refresh_entry_gallery(item)
        self.bind_extension()
        self.refresh_preview()

    def _update_item_controls(self, item):
        self._updating_controls = True
        try:
            self.naming_combo.setCurrentIndex(max(
                0, self.naming_combo.findData(item.naming_policy.value)
            ))
            self.segment_list.clear()
            for segment in item.ordered_segments():
                x1, y1, x2, y2 = segment.bbox
                row = QListWidgetItem(
                    f"P{segment.page}  {round(x1)},{round(y1)} - {round(x2)},{round(y2)}"
                )
                row.setData(Qt.ItemDataRole.UserRole, segment.segment_id)
                self.segment_list.addItem(row)
            if item.segments:
                page = self.main_window.current_loaded_page
                index = next((
                    index for index, segment in enumerate(item.ordered_segments())
                    if segment.page == page
                ), 0)
                self.segment_list.setCurrentRow(index)
                self._set_bbox_spins(item.ordered_segments()[index].bbox)
            if self.mode == ReviewMode.MARKDOWN_IMAGES:
                self._set_assignment_controls(item)
            self.status_label.setText(
                item.error or str(item.metadata.get("reason") or "")
                or f"{len(item.segments)} 个裁切段"
            )
        finally:
            self._updating_controls = False

    def _set_assignment_controls(self, item):
        index = self.entry_combo.findData(item.entry_id)
        if index < 0:
            index = self.entry_combo.findText(
                str(item.metadata.get("headword") or ""), Qt.MatchFlag.MatchExactly
            )
        self.entry_combo.setCurrentIndex(max(0, index))
        self.entry_search.setText(str(item.metadata.get("headword") or ""))
        self.caption_edit.setPlainText(str(item.metadata.get("caption") or ""))
        self.ignore_check.setChecked(item.ignored)

    def find_entry(self):
        needle = self.entry_search.text().strip().casefold()
        for index in range(1, self.entry_combo.count()):
            if needle and needle in self.entry_combo.itemText(index).casefold():
                self.entry_combo.setCurrentIndex(index)
                return
        self.status_label.setText("没有找到对应词条")

    def assignment_changed(self, _index=None):
        if self._updating_controls:
            return
        item = self.current_item()
        if item is None:
            return
        item.metadata["entry_id"] = str(self.entry_combo.currentData() or "")
        item.metadata["headword"] = self.entry_combo.currentText()
        item.label = item.metadata["headword"] or item.original_name
        self.mark_dirty(item, metadata=True)
        self.refresh_entry_gallery(item)
        self.refresh_page_gallery(item.page)
        self.refresh_preview()

    def caption_changed(self):
        if self._updating_controls:
            return
        item = self.current_item()
        if item:
            item.metadata["caption"] = self.caption_edit.toPlainText()
            self.mark_dirty(item, metadata=True)
            self.refresh_preview()

    def ignore_changed(self, ignored):
        if self._updating_controls:
            return
        item = self.current_item()
        if item:
            item.metadata["action"] = "ignore" if ignored else "attach"
            self.mark_dirty(item, metadata=True)
            self.refresh_entry_gallery(item)
            self.refresh_page_gallery(item.page)
            self.refresh_preview()

    def attached_images(self, entry_item):
        if entry_item is None:
            return []
        entry_id = (
            entry_item.item_id
            if entry_item.mode == ReviewMode.DICTIONARY_SLICES else entry_item.entry_id
        )
        headword = str(entry_item.metadata.get("headword") or entry_item.label)
        exact = [
            item for item in self.image_items
            if item.entry_id and item.entry_id == entry_id
        ]
        candidates = exact or [
            item for item in self.image_items
            if headword and str(item.metadata.get("headword") or "") == headword
        ]
        return sorted(candidates, key=lambda item: (
            int(item.metadata.get("image_order") or 0), item.page, item.sequence
        ))

    def refresh_entry_gallery(self, item):
        self.entry_gallery.blockSignals(True)
        self.entry_gallery.clear()
        for image_item in self.attached_images(item):
            self.entry_gallery.addItem(self._gallery_row(image_item))
        self.entry_gallery.blockSignals(False)

    def entry_image_selected(self, current, _previous=None):
        if current is None or self.mode != ReviewMode.MARKDOWN_IMAGES:
            return
        item_id = str(current.data(Qt.ItemDataRole.UserRole) or "")
        target = self.image_items_by_id.get(item_id)
        if target and target.page != self.main_window.current_loaded_page:
            self.main_window.goto_page(target.page)
            self.refresh_page_gallery(target.page)
        self._select_gallery_id(item_id)

    def _select_gallery_id(self, item_id):
        for row in range(self.item_list.count()):
            item = self.item_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == item_id:
                self.item_list.setCurrentItem(item)
                return
        self.select_item(item_id)

    def entry_image_order_changed(self, *_args):
        if self._updating_controls:
            return
        for index in range(self.entry_gallery.count()):
            item_id = str(self.entry_gallery.item(index).data(Qt.ItemDataRole.UserRole) or "")
            item = self.image_items_by_id.get(item_id)
            if item and item.metadata.get("image_order") != index + 1:
                item.metadata["image_order"] = index + 1
                self.mark_dirty(item, metadata=True, refresh_row=False)
        self.refresh_preview()

    def segment_row_changed(self, row):
        if self._updating_controls or row < 0:
            return
        item = self.current_item()
        if item is None or row >= len(item.ordered_segments()):
            return
        segment = item.ordered_segments()[row]
        self._set_bbox_spins(segment.bbox)
        if segment.page == self.main_window.current_loaded_page:
            self.bind_extension(segment.segment_id)
        else:
            self.status_label.setText(
                f"片段位于第 {segment.page} 页，双击可定位"
            )

    def locate_segment_on_canvas(self, list_item):
        segment_id = str(
            list_item.data(Qt.ItemDataRole.UserRole) or ""
        )
        item = self.current_item()
        segment = next((
            value for value in item.segments
            if value.segment_id == segment_id
        ), None) if item is not None else None
        if segment is None:
            self.status_label.setText("文字片段已经不存在")
            return
        if segment.page != int(self.main_window.current_loaded_page or 0):
            self.main_window.goto_page(segment.page)
            QTimer.singleShot(
                0, lambda value=segment_id: self._ensure_segment_visible(value)
            )
            return
        self._ensure_segment_visible(segment_id)

    def _ensure_segment_visible(self, segment_id):
        item = self.current_item()
        segment = next((
            value for value in item.segments
            if value.segment_id == str(segment_id)
        ), None) if item is not None else None
        if segment is None:
            return
        self.bind_extension(segment.segment_id)
        extension = self._extension()
        canvas = getattr(extension, "canvas", None)
        if extension is not None:
            extension.select_segment(segment.segment_id)
        if canvas is not None:
            x1, y1, x2, y2 = segment.bbox
            if hasattr(canvas, "ensure_visible_bbox"):
                canvas.ensure_visible_bbox(x1, y1, x2 - x1, y2 - y1)
            else:
                canvas.ensureVisible(
                    x1, y1, x2 - x1, y2 - y1, 60, 60
                )
    def _set_bbox_spins(self, bbox):
        old = self._updating_controls
        self._updating_controls = True
        for spin, value in zip(self.bbox_spins, bbox):
            spin.setValue(value)
        self._updating_controls = old

    def bbox_value_changed(self):
        if self._updating_controls:
            return
        item = self.current_item()
        row = self.segment_list.currentRow()
        if item is None or row < 0 or row >= len(item.ordered_segments()):
            return
        values = tuple(spin.value() for spin in self.bbox_spins)
        if values[2] <= values[0] or values[3] <= values[1]:
            self.status_label.setText("bbox 的右下角必须大于左上角")
            return
        item.ordered_segments()[row].bbox = values
        self.mark_dirty(item, geometry=True)
        self.bind_extension(item.ordered_segments()[row].segment_id)
        self.refresh_preview()

    def naming_changed(self, _index=None):
        if self._updating_controls:
            return
        item = self.current_item()
        if item:
            item.naming_policy = NamingPolicy(self.naming_combo.currentData())
            self.mark_dirty(item, geometry=True)

    def bbox_display_mode(self):
        if self.bbox_display_group is None:
            return "page"
        button = self.bbox_display_group.checkedButton()
        return str(button.property("bbox_display_mode")) if button else "selected"

    def bbox_display_mode_changed(self, _button=None):
        self.main_window.project_config[
            "slice_bbox_display_mode"
        ] = self.bbox_display_mode()
        self.main_window.config_manager.save()
        self.bind_extension()

    def bind_extension(self, selected_segment_id=""):
        extension = self._extension()
        if not self._active or extension is None or not hasattr(extension, "set_segments"):
            return
        if extension is not self._selection_extension:
            extension.selection_changed.connect(self.canvas_segment_selected)
            self._selection_extension = extension

        page = int(self.main_window.current_loaded_page or 0)
        catalog = self.image_items if self.mode == ReviewMode.MARKDOWN_IMAGES else self.items
        if (
            self.mode == ReviewMode.DICTIONARY_SLICES
            and self.bbox_display_mode() == "selected"
        ):
            current = self.current_item()
            catalog = [current] if current is not None else []
        segments = []
        self._segment_owners = {}
        for owner in catalog:
            for segment in owner.segment_for_page(page):
                segments.append(segment)
                self._segment_owners[segment.segment_id] = owner.item_id

        previous_selected = selected_segment_id or extension.selected_segment_id()
        extension.set_segments(
            segments, self.geometry_changed, self.new_segment, self.segment_deleted
        )
        extension.set_mode(self._current_edit_mode())
        for segment_id, graphics_item in extension.items.items():
            graphics_item.set_mute_state(
                self._segment_owners.get(segment_id) != self.current_item_id
            )
        if previous_selected in extension.items:
            extension.select_segment(previous_selected)

    def canvas_segment_selected(self, segment_id):
        segment_id = str(segment_id)
        QTimer.singleShot(0, lambda: self._select_canvas_segment(segment_id))

    def _select_canvas_segment(self, segment_id):
        owner_id = self._segment_owners.get(str(segment_id), "")
        if not owner_id:
            return
        if owner_id != self.current_item_id:
            self._select_list_item(owner_id)
        item = self.items_by_id.get(owner_id) or self.image_items_by_id.get(owner_id)
        if item is not None:
            ordered = item.ordered_segments()
            row = next(
                (index for index, segment in enumerate(ordered)
                 if segment.segment_id == str(segment_id)),
                -1,
            )
            if row >= 0:
                self.segment_list.setCurrentRow(row)
        extension = self._extension()
        if extension is not None:
            extension.select_segment(str(segment_id))

    def locate_item_on_canvas(self, list_item):
        item_id = str(list_item.data(Qt.ItemDataRole.UserRole) or "")
        item = self.items_by_id.get(item_id) or self.image_items_by_id.get(item_id)
        if item is None:
            return
        page = int(self.main_window.current_loaded_page or 0)
        segment = next(
            (value for value in item.ordered_segments() if value.page == page),
            item.ordered_segments()[0] if item.segments else None,
        )
        if segment is None:
            self.status_label.setText("当前词条还没有可定位的图片框")
            return
        if segment.page != page:
            self.main_window.goto_page(segment.page)
        self._select_list_item(item.item_id)
        self.bind_extension(segment.segment_id)
        extension = self._extension()
        canvas = getattr(extension, "canvas", None)
        if canvas is not None:
            x1, y1, x2, y2 = segment.bbox
            if hasattr(canvas, "ensure_visible_bbox"):
                canvas.ensure_visible_bbox(x1, y1, x2 - x1, y2 - y1)
            else:
                canvas.ensureVisible(x1, y1, x2 - x1, y2 - y1, 60, 60)

    def _current_edit_mode(self):
        button = self.tool_group.checkedButton()
        return str(button.property("edit_mode")) if button else "select"

    def _edit_mode_changed(self, _button):
        extension = self._extension()
        if extension is not None:
            extension.set_mode(self._current_edit_mode())

    def geometry_changed(self, segment_id, bbox, final):
        owner_id = self._segment_owners.get(str(segment_id), self.current_item_id)
        item = self.items_by_id.get(owner_id) or self.image_items_by_id.get(owner_id)
        segment = next((
            value for value in item.segments if value.segment_id == segment_id
        ), None) if item else None
        if segment is None:
            return
        segment.bbox = tuple(float(value) for value in bbox)
        self.mark_dirty(item, geometry=True)
        self._set_bbox_spins(segment.bbox)
        if final:
            self._update_item_controls(item)
        self.preview_timer.start()

    def start_add_crop(self):
        self._drawing_new_record = True
        self.btn_draw.setChecked(True)
        self._edit_mode_changed(self.btn_draw)
        self.status_label.setText("请在左侧原页拖动框选新图片")

    def replace_current(self):
        item = self.current_item()
        if item and item.segments:
            self.mark_dirty(item, geometry=True)
            self.status_label.setText("当前裁框将在点击应用后替换图片")
        else:
            self.btn_draw.setChecked(True)
            self._edit_mode_changed(self.btn_draw)

    def new_segment(self, bbox):
        page = int(self.main_window.current_loaded_page or 0)
        if not page:
            return
        if self.mode == ReviewMode.MARKDOWN_IMAGES and self._drawing_new_record:
            current = self.current_item()
            item_id = uuid.uuid4().hex
            item = ReviewItem(
                item_id, ReviewMode.MARKDOWN_IMAGES,
                str(current.metadata.get("headword") or "新增图片") if current else "新增图片",
                page,
                [CropSegment(f"{item_id}:0", page, tuple(bbox), order=0, label="image")],
                source_path=self.source_path,
                sequence=1 + max(
                    (value.sequence for value in self.image_items if value.page == page),
                    default=0,
                ),
                status="manual",
                naming_policy=NamingPolicy.PAGE_BBOX,
                metadata={
                    "entry_id": current.entry_id if current else "",
                    "headword": str(current.metadata.get("headword") or "") if current else "",
                    "caption": "", "action": "attach",
                    "image_order": len(self.attached_images(current)) + 1 if current else 1,
                },
            )
            item.mark_geometry_dirty()
            self.image_items.append(item)
            self.items.append(item)
            self.image_items_by_id[item_id] = item
            self.items_by_id[item_id] = item
            self._drawing_new_record = False
            self.refresh_page_gallery(page)
            self._select_gallery_id(item_id)
        else:
            item = self.current_item()
            if item is None:
                return
            segment = CropSegment(
                f"{item.item_id}:{len(item.segments)}:{page}", page, tuple(bbox),
                order=len(item.segments),
                label="image" if item.mode == ReviewMode.MARKDOWN_IMAGES else "text",
            )
            item.segments.append(segment)
            item.page = item.page or page
            item.status, item.error = "manual", ""
            self.mark_dirty(item, geometry=True)
            self._update_item_controls(item)
            self.bind_extension(segment.segment_id)
            self.refresh_preview()
        self.btn_select.setChecked(True)
        self._edit_mode_changed(self.btn_select)

    def segment_deleted(self, segment_id):
        owner_id = self._segment_owners.get(str(segment_id), self.current_item_id)
        item = self.items_by_id.get(owner_id) or self.image_items_by_id.get(owner_id)
        if item is None:
            return
        ordered_before = item.ordered_segments()
        removed_index = next((
            index for index, segment in enumerate(ordered_before)
            if segment.segment_id == str(segment_id)
        ), -1)
        item.segments = [
            segment for segment in item.segments if segment.segment_id != segment_id
        ]
        ordered_after = item.ordered_segments()
        for index, segment in enumerate(ordered_after):
            segment.order = index
        next_segment_id = ""
        if ordered_after:
            next_segment_id = ordered_after[
                min(max(removed_index, 0), len(ordered_after) - 1)
            ].segment_id
        self.mark_dirty(item, geometry=True)
        self._update_item_controls(item)
        self.bind_extension(next_segment_id)
        self.refresh_preview()

    def delete_selected_segment(self):
        extension = self._extension()
        if extension is None:
            return
        if not extension.selected_segment_id():
            current = self.segment_list.currentItem()
            segment_id = str(
                current.data(Qt.ItemDataRole.UserRole) or ""
            ) if current is not None else ""
            if segment_id:
                extension.select_segment(segment_id)
        extension.delete_selected()

    def transfer_selected_segment(self, direction):
        if self.mode != ReviewMode.DICTIONARY_SLICES:
            return
        extension = self._extension()
        segment_id = extension.selected_segment_id() if extension is not None else ""
        owner_id = self._segment_owners.get(segment_id, self.current_item_id)
        source = self.items_by_id.get(owner_id)
        if source is None or not segment_id:
            self.status_label.setText("请先选择要移交的矩形框")
            return
        page = int(self.main_window.current_loaded_page or 0)
        ordered_items = sorted(
            (item for item in self.items if page in (item.metadata.get("pages") or ())),
            key=lambda item: (item.sequence, item.label),
        )
        try:
            source_index = ordered_items.index(source)
        except ValueError:
            return
        target_index = source_index + int(direction)
        if target_index < 0 or target_index >= len(ordered_items):
            self.status_label.setText("没有可移交的相邻词条")
            return
        segment = next((
            value for value in source.segments if value.segment_id == segment_id
        ), None)
        if segment is None:
            return
        target = ordered_items[target_index]
        source.segments.remove(segment)
        transferred = copy.deepcopy(segment)
        transferred.segment_id = f"{target.item_id}:manual:{uuid.uuid4().hex}"
        transferred.order = len(target.segments)
        transferred.origin = "manual"
        target.segments.append(transferred)
        for index, value in enumerate(source.ordered_segments()):
            value.order = index
        self.mark_dirty(source, geometry=True)
        self.mark_dirty(target, geometry=True)
        self._refresh_dictionary_row(source)
        self._refresh_dictionary_row(target)
        self._select_list_item(target.item_id)
        self._update_item_controls(target)
        self.bind_extension(transferred.segment_id)
        self.refresh_preview()
        self.status_label.setText(f"矩形框已移交给 {target.label}")

    def move_segment(self, delta):
        item = self.current_item()
        row = self.segment_list.currentRow()
        if item is None or row < 0:
            return
        ordered = item.ordered_segments()
        target = row + delta
        if target < 0 or target >= len(ordered):
            return
        ordered[row].order, ordered[target].order = ordered[target].order, ordered[row].order
        self.mark_dirty(item, geometry=True)
        self._update_item_controls(item)
        self.segment_list.setCurrentRow(target)
        self.refresh_preview()

    def mark_dirty(self, item, geometry=False, metadata=False, refresh_row=True):
        if geometry:
            item.mark_geometry_dirty()
        if metadata:
            item.mark_metadata_dirty()
        if not geometry and not metadata:
            item.dirty = True
        item.status = "modified"
        if refresh_row and self.mode != ReviewMode.MARKDOWN_IMAGES:
            self._refresh_dictionary_row(item)

    def discard_current(self):
        item = self.current_item()
        if item is None:
            return
        baseline = self.baselines.get(item.item_id) or self.image_baselines.get(item.item_id)
        if baseline is None:
            if item in self.items:
                self.items.remove(item)
            if item in self.image_items:
                self.image_items.remove(item)
            self.items_by_id.pop(item.item_id, None)
            self.image_items_by_id.pop(item.item_id, None)
            self.refresh_page_gallery(self.main_window.current_loaded_page)
            return
        restored = copy.deepcopy(baseline)
        if item in self.items:
            self.items[self.items.index(item)] = restored
        if item in self.image_items:
            self.image_items[self.image_items.index(item)] = restored
        self.items_by_id[restored.item_id] = restored
        self.image_items_by_id[restored.item_id] = restored
        if self.mode == ReviewMode.MARKDOWN_IMAGES:
            self.refresh_page_gallery(restored.page)
            self._select_gallery_id(restored.item_id)
        else:
            self.populate_items()
            self.select_item(restored.item_id)

    def apply_current(self):
        item = self.current_item()
        return self._apply([item] if item else [])

    def apply_all(self):
        return self._apply(self.items)

    def _apply(self, items):
        if not items or self.store is None:
            return True
        report, self.source_text = self.service.apply(
            items, self.store, self.source_text, catalog_items=self.items
        )
        ok = report.ok
        for item in items:
            if item and item.item_id in report.applied:
                self.baselines[item.item_id] = copy.deepcopy(item)
        if self.mode == ReviewMode.DICTIONARY_SLICES and self.image_store:
            ids = {item.item_id for item in items if item}
            words = {item.label for item in items if item}
            changed_images = [
                image for image in self.image_items
                if image.dirty and (
                    image.entry_id in ids
                    or str(image.metadata.get("headword") or "") in words
                )
            ]
            if changed_images:
                image_report, self.source_text = self.service.apply(
                    changed_images, self.image_store, self.source_text,
                    catalog_items=self.image_items,
                )
                ok = ok and image_report.ok
                report.applied.extend(image_report.applied)
                report.errors.update(image_report.errors)
                for image in changed_images:
                    if image.item_id in image_report.applied:
                        self.image_baselines[image.item_id] = copy.deepcopy(image)
        self.status_label.setText(report.message)
        self.main_window.statusBar().showMessage(report.message, 8000)
        if report.errors:
            self.status_label.setText(
                f"{report.message}：{next(iter(report.errors.values()))}"
            )
        if self.mode == ReviewMode.MARKDOWN_IMAGES:
            self.refresh_page_gallery(self.main_window.current_loaded_page)
        else:
            self.populate_items()
        self.refresh_preview()
        return ok

    @staticmethod
    def _ocr_identity(_main_window, info):
        if not isinstance(info, dict):
            return None
        filename = os.path.splitext(os.path.basename(info.get("path", "")))[0]
        suffix = re.sub(
            r"^(?:page_)?\d+(?:_|$)", "", filename, count=1, flags=re.I
        )
        return (
            canonical_engine_id(str(info.get("engine_id") or "")),
            suffix.lower(),
            bool(info.get("legacy")),
        )

    def refresh_ocr_sources(self, page):
        if self.ocr_source_combo is None:
            return
        page = int(page or 0)
        real_page = page + int(
            self.main_window.project_config.get("page_offset", 0) or 0
        )
        results = sort_ocr_results_by_priority(
            discover_ocr_results(
                self.main_window.project_config.get("ocr_json_path", ""),
                real_page,
            ),
            getattr(self.main_window, "global_config", {}),
        )
        selected = self._selected_ocr_identity
        self.ocr_source_combo.blockSignals(True)
        self.ocr_source_combo.clear()
        labels = set()
        selected_index = -1
        for info in results:
            data = dict(info)
            data["type"] = "ocr"
            label = str(data.get("label") or data.get("engine_id") or "OCR")
            if label in labels:
                label = f"{label} ({os.path.basename(data.get('path', ''))})"
            labels.add(label)
            self.ocr_source_combo.addItem(label, data)
            index = self.ocr_source_combo.count() - 1
            self.ocr_source_combo.setItemData(
                index, data.get("path", ""), Qt.ItemDataRole.ToolTipRole
            )
            if selected and self._ocr_identity(self.main_window, data) == selected:
                selected_index = index
        if not results:
            self.ocr_source_combo.addItem("当前页没有 OCR 结果", None)
        self.ocr_source_combo.setCurrentIndex(
            selected_index if selected_index >= 0 else 0
        )
        self.ocr_source_combo.blockSignals(False)
        self.ocr_source_changed(self.ocr_source_combo.currentIndex())
        if self.btn_replace_from_ocr is not None:
            self.btn_replace_from_ocr.setEnabled(bool(results))
        if self.btn_replace_page_from_ocr is not None:
            self.btn_replace_page_from_ocr.setEnabled(bool(results))

    def ocr_source_changed(self, _index=None):
        if self.ocr_source_combo is None:
            return
        info = self.ocr_source_combo.currentData()
        identity = self._ocr_identity(self.main_window, info)
        if identity is not None:
            self._selected_ocr_identity = identity

    def _selected_ocr_result_for_page(self, page):
        identity = self._selected_ocr_identity
        if identity is None:
            return None
        real_page = int(page) + int(
            self.main_window.project_config.get("page_offset", 0) or 0
        )
        results = discover_ocr_results(
            self.main_window.project_config.get("ocr_json_path", ""),
            real_page,
        )
        for info in results:
            data = dict(info)
            data["type"] = "ocr"
            if self._ocr_identity(self.main_window, data) == identity:
                return data
        return None

    def replace_current_from_selected_ocr(self):
        item = self.current_item()
        if item is None or self.mode != ReviewMode.DICTIONARY_SLICES:
            return
        current_info = (
            self.ocr_source_combo.currentData()
            if self.ocr_source_combo is not None else None
        )
        if not isinstance(current_info, dict):
            self.status_label.setText("当前页没有可用的 OCR 数据源")
            return

        def load_page(page):
            info = self._selected_ocr_result_for_page(page)
            return self.main_window.load_ocr_json(page, info) if info else []

        def page_size(page):
            image = self._load_page_image(page)
            return (
                (image.width(), image.height())
                if not image.isNull() else (1.0, 1.0)
            )

        result = match_entry(item, LoadedOcrMapper(load_page, page_size))
        if not result.segments:
            self.status_label.setText(
                result.reason or "指定 OCR 数据源没有匹配到当前词条"
            )
            return

        item.segments = list(result.segments)
        item.error = ""
        item.metadata.update({
            "orientation": result.orientation,
            "confidence": result.confidence,
            "reason": result.reason,
            "ocr_source": self.ocr_source_combo.currentText(),
            "ocr_source_identity": list(self._selected_ocr_identity or ()),
        })
        self.mark_dirty(item, geometry=True)
        self._update_item_controls(item)
        current_page = int(self.main_window.current_loaded_page or 0)
        selected = next(
            (segment.segment_id for segment in item.segments
             if segment.page == current_page),
            item.segments[0].segment_id,
        )
        self.bind_extension(selected)
        self.refresh_preview()
        self.status_label.setText(
            f"已用 {self.ocr_source_combo.currentText()} 替换当前框，点击应用后保存"
        )
    def replace_page_from_selected_ocr(self):
        if self.mode != ReviewMode.DICTIONARY_SLICES:
            return
        page = int(self.main_window.current_loaded_page or 0)
        info = (
            self.ocr_source_combo.currentData()
            if self.ocr_source_combo is not None else None
        )
        if page <= 0 or not isinstance(info, dict):
            self.status_label.setText("当前页没有可用的 OCR 数据源")
            return
        page_items = [
            item for item in self.items
            if page in tuple(item.metadata.get("pages") or ())
        ]
        if not page_items:
            self.status_label.setText("当前页没有可覆盖的词条")
            return

        dialog = QMessageBox(self)
        dialog.setWindowTitle("覆盖整页 OCR 框")
        dialog.setText("是否保留当前页已经人工调整的矩形框？")
        preserve_button = dialog.addButton(
            "保留人工框并覆盖", QMessageBox.ButtonRole.AcceptRole
        )
        replace_button = dialog.addButton(
            "全部覆盖", QMessageBox.ButtonRole.DestructiveRole
        )
        dialog.addButton(QMessageBox.StandardButton.Cancel)
        dialog.setDefaultButton(preserve_button)
        dialog.exec()
        clicked = dialog.clickedButton()
        if clicked not in (preserve_button, replace_button):
            return
        preserve_manual = clicked is preserve_button
        self.cancel_matching()

        def load_page(target_page):
            selected = self._selected_ocr_result_for_page(target_page)
            return self.main_window.load_ocr_json(
                target_page, selected
            ) if selected else []

        def page_size(target_page):
            image = self._load_page_image(target_page)
            return (
                (image.width(), image.height())
                if not image.isNull() else (1.0, 1.0)
            )

        reserved = {}
        if preserve_manual:
            reserved[page] = [
                segment.bbox
                for item in page_items
                for segment in item.segment_for_page(page)
                if segment.origin != "ocr_auto"
            ]
        results = match_entries_global(
            page_items,
            LoadedOcrMapper(load_page, page_size),
            reserved_by_page=reserved,
            page_filter=page,
        )
        changed = 0
        for item in page_items:
            result = results[item.item_id]
            retained = [
                segment for segment in item.segments
                if segment.page != page
                or (preserve_manual and segment.origin != "ocr_auto")
            ]
            replacement = list(result.segments)
            new_segments = retained + replacement
            if [segment.to_dict() for segment in new_segments] == [
                segment.to_dict() for segment in item.segments
            ]:
                continue
            item.segments = new_segments
            for index, segment in enumerate(item.ordered_segments()):
                segment.order = index
            item.error = "" if replacement else result.reason
            item.metadata.update({
                "orientation": result.orientation,
                "confidence": result.confidence,
                "reason": result.reason,
                "ocr_source": self.ocr_source_combo.currentText(),
                "ocr_source_identity": list(self._selected_ocr_identity or ()),
            })
            self.mark_dirty(item, geometry=True)
            self._refresh_dictionary_row(item)
            changed += 1
        current = self.current_item()
        if current is not None:
            self._update_item_controls(current)
        self.bind_extension()
        self.refresh_preview()
        self.status_label.setText(
            f"已重算本页 {changed} 个词条；"
            + ("人工框已保留" if preserve_manual else "原框已全部覆盖")
            + "。点击应用后保存"
        )
    def refresh_preview(self):
        item = self.current_item()
        if self.mode == ReviewMode.MARKDOWN_IMAGES:
            if item is None:
                self.preview.clear_image()
                return
            image = self.service.compose(item) if item.segments else QImage()
            if image.isNull():
                path = str(item.metadata.get("local_path") or "")
                image = QImage(path) if path else QImage()
            self.preview.set_image(image)
        elif item is None:
            self.preview.clear()
        else:
            self.preview.setHtml(
                self.service.entry_preview_html(item, self.attached_images(item))
            )

    def start_matching(self):
        if self.mode != ReviewMode.DICTIONARY_SLICES or not self.items:
            return
        self.cancel_matching()
        self._match_request += 1
        page = int(self.main_window.current_loaded_page or 0)
        page_blocks = {}
        source_fingerprint = ""
        info = (
            self.ocr_source_combo.currentData()
            if self.ocr_source_combo is not None else None
        )
        if page > 0 and isinstance(info, dict):
            rows = self.main_window.load_ocr_json(page, info)
            image = self._load_page_image(page)
            width = image.width() if not image.isNull() else 1
            height = image.height() if not image.isNull() else 1
            normalizer = LoadedOcrMapper(
                lambda target: rows if int(target) == page else (),
                lambda _target: (width, height),
            )
            page_blocks[page] = tuple(normalizer.load_page_data(page))
            path = str(info.get("path") or "")
            try:
                stat = os.stat(path)
                source_fingerprint = (
                    f"{os.path.abspath(path)}:{stat.st_size}:{stat.st_mtime_ns}"
                )
            except OSError:
                source_fingerprint = repr(
                    self._ocr_identity(self.main_window, info)
                )
        self.match_worker = OcrMatchWorker(
            self._match_request,
            self.items,
            self.main_window.project_config.get("ocr_json_path", ""),
            self.main_window.project_config.get("page_offset", 0),
            page,
            self,
            page_blocks=page_blocks,
            source_fingerprint=source_fingerprint,
        )
        self.match_worker.matched.connect(self.ocr_match_ready)
        self.match_worker.progress.connect(self.ocr_match_progress)
        self.match_worker.completed.connect(self.ocr_match_completed)
        self.match_worker.finished.connect(self.match_worker.deleteLater)
        self.status_label.setText("正在匹配当前页 OCR…")
        self.match_worker.start()
    def cancel_matching(self):
        self._match_request += 1
        worker = self.match_worker
        self.match_worker = None
        if worker is not None and worker.isRunning():
            worker.cancel()
            worker.requestInterruption()
    def ocr_match_ready(self, request_id, item_id, result):
        if request_id != self._match_request:
            return
        item = self.items_by_id.get(item_id)
        if item is None or item.dirty:
            return
        page = int(self.main_window.current_loaded_page or 0)
        retained = [
            segment for segment in item.segments if segment.page != page
        ]
        item.segments = retained + list(result.segments)
        item.status = "matched" if item.segments else result.status
        item.error = "" if result.segments else result.reason
        item.metadata.update({
            "orientation": result.orientation,
            "confidence": result.confidence,
            "reason": result.reason,
        })
        if item.item_id == self.current_item_id:
            self._update_item_controls(item)
            self.bind_extension()
            self.refresh_preview()
        self._refresh_dictionary_row(item)
    def _refresh_dictionary_row(self, item):
        if self.mode == ReviewMode.MARKDOWN_IMAGES:
            return
        for row in range(self.item_list.count()):
            widget_item = self.item_list.item(row)
            if widget_item.data(Qt.ItemDataRole.UserRole) == item.item_id:
                widget_item.setText(self._row_text(item))
                widget_item.setToolTip(
                    item.error or str(item.metadata.get("reason") or "")
                )
                break

    def ocr_match_progress(self, current, total):
        self.status_label.setText(f"OCR 匹配 {current}/{total}")

    def ocr_match_completed(self, request_id, cancelled):
        if request_id == self._match_request:
            self.match_worker = None
            self.status_label.setText("OCR 匹配已取消" if cancelled else "OCR 匹配完成")

    def _apply_legacy_box(self, item):
        box = item.metadata.pop("legacy_normalized_box", None)
        if not box or item.segments:
            return
        image = self._load_page_image(item.page)
        if image.isNull():
            item.metadata["legacy_normalized_box"] = box
            return
        x, y, width, height = (float(value) for value in box)
        item.segments = [CropSegment(
            f"{item.item_id}:0", item.page,
            (
                x * image.width(), y * image.height(),
                (x + width) * image.width(), (y + height) * image.height(),
            ),
            order=0, label="image",
        )]

    def on_page_loaded(self, page_num):
        if not self._active:
            return
        if self.mode == ReviewMode.DICTIONARY_SLICES:
            self._reload_dictionary_page(page_num)
            return
        self.entries = self._load_entries(self.entry_side, page_num)
        self._populate_entry_combo()
        self.refresh_page_gallery(page_num)
        self.bind_extension()
        item = self.current_item()
        if item and item.segment_for_page(page_num):
            self._set_bbox_spins(item.segment_for_page(page_num)[0].bbox)

    def undo(self):
        extension = self._extension()
        if extension is not None:
            extension.undo_stack.undo()

    def redo(self):
        extension = self._extension()
        if extension is not None:
            extension.undo_stack.redo()

    def _extension(self):
        manager = getattr(self.main_window, "workspace_view_manager", None)
        return getattr(manager, "image_edit_extension", None)

    def _load_page_image(self, page):
        pixmap = self.main_window.get_page_pixmap(int(page))
        return pixmap.toImage() if pixmap is not None and not pixmap.isNull() else QImage()
