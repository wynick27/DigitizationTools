import os
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QWidget,
                             QDialogButtonBox, QFormLayout, QLineEdit, QKeySequenceEdit,
                             QListWidget, QListWidgetItem, QPushButton, QSpinBox, QLabel, QFileDialog,
                             QInputDialog, QMessageBox, QComboBox, QTextEdit, QStackedWidget,
                             QCheckBox, QDoubleSpinBox)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QKeySequence
from ocr.ocr_engines import (
    ENGINE_DEFS, OCR_CATEGORY_OPTIONS, PADDLE_ENGINE_ID,
    canonical_engine_id, excluded_categories,
)
from ocr.ocr_worker import refresh_remote_engine_label
from lang.i18n import text_from_config

class ProjectManagerDialog(QDialog):
    settings_changed = pyqtSignal()

    def _text(self, key, **values):
        template = text_from_config(
            self.config_manager.get_global(),
            f"pm_{key}",
        )
        return template.format(**values)
    def __init__(self, parent, config_manager):
        super().__init__(parent)
        self.config_manager = config_manager
        self.setWindowTitle(self._text("window_title"))
        self.resize(800, 600)
        # UI Layout
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        
        # Tab 1: Global Settings
        self.tab_global = QWidget()
        self.init_global_tab()
        self.tabs.addTab(self.tab_global, self._text("tab_global"))

        self.tab_ocr = QWidget()
        self.init_ocr_tab()
        self.tabs.addTab(self.tab_ocr, self._text("tab_ocr"))
        
        # Tab 2: Projects
        self.tab_projects = QWidget()
        self.init_projects_tab()
        self.tabs.addTab(self.tab_projects, self._text("tab_projects"))
        
        # Buttons
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.button(QDialogButtonBox.StandardButton.Close).setText(self._text("close"))
        btns.rejected.connect(self.accept) # Close acts as confirm/exit
        layout.addWidget(btns)
        
    def init_global_tab(self):
        layout = QFormLayout(self.tab_global)
        self.input_furigana = QKeySequenceEdit()
        self.inputs_alt = []
        
        # Load values
        g = self.config_manager.get_global()
        
        # Shortcuts logic
        self.input_furigana.setKeySequence(QKeySequence(g.get("shortcut_furigana", "Ctrl+Shift+F")))
        self.input_furigana.keySequenceChanged.connect(self.save_global)
        layout.addRow(self._text("furigana_shortcut"), self.input_furigana)

        self.input_furigana_left = QLineEdit()
        self.input_furigana_left.setText(g.get("furigana_left_marker", "["))
        self.input_furigana_left.textChanged.connect(self.save_global)
        layout.addRow(self._text("ruby_left"), self.input_furigana_left)

        self.input_furigana_right = QLineEdit()
        self.input_furigana_right.setText(g.get("furigana_right_marker", "]"))
        self.input_furigana_right.textChanged.connect(self.save_global)
        layout.addRow(self._text("ruby_right"), self.input_furigana_right)

        self.combo_furigana_kana = QComboBox()
        self.combo_furigana_kana.addItem(self._text("hiragana"), "hiragana")
        self.combo_furigana_kana.addItem(self._text("katakana"), "katakana")
        kana_idx = self.combo_furigana_kana.findData(g.get("furigana_kana_type", "hiragana"))
        self.combo_furigana_kana.setCurrentIndex(kana_idx if kana_idx >= 0 else 0)
        self.combo_furigana_kana.currentIndexChanged.connect(self.save_global)
        layout.addRow(self._text("ruby_kana"), self.combo_furigana_kana)

        self.chk_furigana_split = QCheckBox()
        self.chk_furigana_split.setChecked(bool(g.get("furigana_use_jmdict_split", True)))
        self.chk_furigana_split.toggled.connect(self.save_global)
        layout.addRow(self._text("jmdict_split"), self.chk_furigana_split)
        
        alt_texts = g.get("shortcuts_alt", [""] * 10)
        for i in range(10):
            le = QLineEdit()
            le.setText(alt_texts[i] if i < len(alt_texts) else "")
            le.textChanged.connect(self.save_global)
            self.inputs_alt.append(le)
            layout.addRow(self._text("alt_text", index=i), le)

    def init_ocr_tab(self):
        layout = QHBoxLayout(self.tab_ocr)
        g = self.config_manager.get_global()
        engines = g.setdefault("ocr_engines", {})

        self.ocr_nav = QListWidget()
        self.ocr_pages = QStackedWidget()
        layout.addWidget(self.ocr_nav, 1)
        layout.addWidget(self.ocr_pages, 3)

        self.ocr_nav.currentRowChanged.connect(self.ocr_pages.setCurrentIndex)

        # Common settings
        common_page, common_layout = self._add_ocr_page(self._text("common_settings"))
        self.spin_retry = QSpinBox()
        self.spin_retry.setRange(1, 10)
        self.spin_retry.setValue(int(g.get("ocr_retry_count", 3)))
        self.spin_retry.valueChanged.connect(self.save_global)

        self.spin_concurrent = QSpinBox()
        self.spin_concurrent.setRange(1, 8)
        self.spin_concurrent.setValue(int(g.get("ocr_concurrent_tasks", 2)))
        self.spin_concurrent.valueChanged.connect(self.save_global)

        self.list_excluded_categories = QListWidget()
        self.list_excluded_categories.setMinimumHeight(220)
        selected_categories = excluded_categories(g)
        for category_id, category_label in OCR_CATEGORY_OPTIONS:
            item = QListWidgetItem(self._text(f"category_{category_id}"))
            item.setData(Qt.ItemDataRole.UserRole, category_id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if category_id in selected_categories
                else Qt.CheckState.Unchecked
            )
            self.list_excluded_categories.addItem(item)
        self.list_excluded_categories.itemChanged.connect(self.save_global)
        self.list_ocr_priority = QListWidget()
        priority = g.get("ocr_result_priority") or [PADDLE_ENGINE_ID, "chrome_lens", "textin", "mineru", "quark", "local"]
        seen_priority = set()
        for engine_id in priority:
            engine_id = canonical_engine_id(engine_id)
            if engine_id in ENGINE_DEFS and engine_id not in seen_priority:
                self._add_ocr_priority_item(engine_id)
                seen_priority.add(engine_id)
        for engine_id in ENGINE_DEFS:
            if engine_id not in seen_priority:
                self._add_ocr_priority_item(engine_id)

        priority_buttons = QWidget()
        priority_buttons_layout = QHBoxLayout(priority_buttons)
        priority_buttons_layout.setContentsMargins(0, 0, 0, 0)
        self.btn_ocr_priority_up = QPushButton(self._text("move_up"))
        self.btn_ocr_priority_down = QPushButton(self._text("move_down"))
        self.btn_ocr_priority_up.clicked.connect(lambda: self.move_ocr_priority(-1))
        self.btn_ocr_priority_down.clicked.connect(lambda: self.move_ocr_priority(1))
        priority_buttons_layout.addWidget(self.btn_ocr_priority_up)
        priority_buttons_layout.addWidget(self.btn_ocr_priority_down)

        common_layout.addRow(self._text("retry_count"), self.spin_retry)
        common_layout.addRow(self._text("concurrent_tasks"), self.spin_concurrent)
        common_layout.addRow(self._text("hidden_categories"), self.list_excluded_categories)
        common_layout.addRow(self._text("ocr_priority"), self.list_ocr_priority)
        common_layout.addRow("", priority_buttons)

        paddle_page, paddle_layout = self._add_ocr_page("PaddleOCR")
        self.input_api_token = QLineEdit()
        self.input_api_token.setText(g.get("ocr_api_token", ""))
        self.input_api_token.textChanged.connect(self.save_global)

        paddle = engines.setdefault("paddleocr", {})
        self.chk_paddle_orientation = QCheckBox()
        self.chk_paddle_orientation.setChecked(bool(paddle.get("useDocOrientationClassify", False)))
        self.chk_paddle_unwarp = QCheckBox()
        self.chk_paddle_unwarp.setChecked(bool(paddle.get("useDocUnwarping", False)))
        self.chk_paddle_chart = QCheckBox()
        self.chk_paddle_chart.setChecked(bool(paddle.get("useChartRecognition", False)))
        for widget in [self.chk_paddle_orientation, self.chk_paddle_unwarp, self.chk_paddle_chart]:
            widget.toggled.connect(self.save_global)
        paddle_layout.addRow(self._text("api_token"), self.input_api_token)
        paddle_layout.addRow(self._text("orientation"), self.chk_paddle_orientation)
        paddle_layout.addRow(self._text("unwarp"), self.chk_paddle_unwarp)
        paddle_layout.addRow(self._text("chart_recognition"), self.chk_paddle_chart)

        textin_page, textin_layout = self._add_ocr_page("TextIn")
        textin = engines.setdefault("textin", {})
        self.input_textin_app_id = QLineEdit(textin.get("app_id", ""))
        self.input_textin_secret = QLineEdit(textin.get("secret_code", ""))
        self.input_textin_endpoint = QLineEdit(textin.get("endpoint", "https://api.textin.com/api/v1/xparse/parse/sync"))
        self.input_textin_password = QLineEdit(textin.get("password", ""))
        self.input_textin_page_range = QLineEdit(textin.get("page_range", ""))
        self.chk_textin_table = QCheckBox()
        self.chk_textin_table.setChecked(bool(textin.get("include_table_structure", True)))
        self.chk_textin_chars = QCheckBox()
        self.chk_textin_chars.setChecked(bool(textin.get("include_char_details", False)))
        self.chk_textin_images = QCheckBox()
        self.chk_textin_images.setChecked(bool(textin.get("include_image_data", False)))
        self.chk_textin_hierarchy = QCheckBox()
        self.chk_textin_hierarchy.setChecked(bool(textin.get("include_hierarchy", True)))
        self.chk_textin_inline_objects = QCheckBox()
        self.chk_textin_inline_objects.setChecked(bool(textin.get("include_inline_objects", False)))
        self.chk_textin_pages = QCheckBox()
        self.chk_textin_pages.setChecked(bool(textin.get("pages", True)))
        self.chk_textin_title_tree = QCheckBox()
        self.chk_textin_title_tree.setChecked(bool(textin.get("title_tree", False)))
        self.chk_textin_remove_watermark = QCheckBox()
        self.chk_textin_remove_watermark.setChecked(bool(textin.get("remove_watermark", False)))
        self.chk_textin_crop_dewarp = QCheckBox()
        self.chk_textin_crop_dewarp.setChecked(bool(textin.get("crop_dewarp", False)))
        self.combo_textin_table_view = QComboBox()
        self.combo_textin_table_view.addItem("HTML", "html")
        self.combo_textin_table_view.addItem("Markdown", "markdown")
        idx = self.combo_textin_table_view.findData(textin.get("table_view", "html"))
        self.combo_textin_table_view.setCurrentIndex(idx if idx >= 0 else 0)
        self.combo_textin_force_engine = QComboBox()
        self.combo_textin_force_engine.addItem(self._text("default"), "")
        for value in ["textin", "mineru", "paddle_ocr", "textin_gui"]:
            self.combo_textin_force_engine.addItem(value, value)
        idx = self.combo_textin_force_engine.findData(textin.get("force_engine", ""))
        self.combo_textin_force_engine.setCurrentIndex(idx if idx >= 0 else 0)
        self.combo_textin_parse_mode = QComboBox()
        for label, value in [
            (self._text("automatic"), "auto"),
            (self._text("scan_document"), "scan"),
            (self._text("parse_document"), "parse"),
            (self._text("lite_mode"), "lite"),
            (self._text("vlm_mode"), "vlm"),
        ]:
            self.combo_textin_parse_mode.addItem(label, value)
        idx = self.combo_textin_parse_mode.findData(textin.get("parse_mode", "auto"))
        self.combo_textin_parse_mode.setCurrentIndex(idx if idx >= 0 else 0)
        self.spin_textin_formula_level = QSpinBox()
        self.spin_textin_formula_level.setRange(0, 1)
        self.spin_textin_formula_level.setValue(int(textin.get("formula_level", 0)))
        self.chk_textin_recognize_chemical = QCheckBox()
        self.chk_textin_recognize_chemical.setChecked(bool(textin.get("recognize_chemical", False)))
        self.combo_textin_image_output_type = QComboBox()
        self.combo_textin_image_output_type.addItem(self._text("image_url"), "url")
        self.combo_textin_image_output_type.addItem(self._text("base64_data"), "base64")
        idx = self.combo_textin_image_output_type.findData(textin.get("image_output_type", "url"))
        self.combo_textin_image_output_type.setCurrentIndex(idx if idx >= 0 else 0)
        textin_layout.addRow(self._text("app_id"), self.input_textin_app_id)
        textin_layout.addRow(self._text("secret"), self.input_textin_secret)
        textin_layout.addRow(self._text("endpoint"), self.input_textin_endpoint)
        textin_layout.addRow(self._text("pdf_password"), self.input_textin_password)
        textin_layout.addRow(self._text("page_range"), self.input_textin_page_range)
        textin_layout.addRow(self._text("include_hierarchy"), self.chk_textin_hierarchy)
        textin_layout.addRow(self._text("include_inline"), self.chk_textin_inline_objects)
        textin_layout.addRow(self._text("include_table"), self.chk_textin_table)
        textin_layout.addRow(self._text("include_chars"), self.chk_textin_chars)
        textin_layout.addRow(self._text("include_images"), self.chk_textin_images)
        textin_layout.addRow(self._text("return_pages"), self.chk_textin_pages)
        textin_layout.addRow(self._text("title_tree"), self.chk_textin_title_tree)
        textin_layout.addRow(self._text("table_format"), self.combo_textin_table_view)
        textin_layout.addRow(self._text("remove_watermark"), self.chk_textin_remove_watermark)
        textin_layout.addRow(self._text("crop_dewarp"), self.chk_textin_crop_dewarp)
        textin_layout.addRow(self._text("force_engine"), self.combo_textin_force_engine)
        textin_layout.addRow(self._text("parse_mode"), self.combo_textin_parse_mode)
        textin_layout.addRow(self._text("formula_level"), self.spin_textin_formula_level)
        textin_layout.addRow(self._text("chemical"), self.chk_textin_recognize_chemical)
        textin_layout.addRow(self._text("image_output"), self.combo_textin_image_output_type)

        mineru_page, mineru_layout = self._add_ocr_page("MinerU")
        mineru = engines.setdefault("mineru", {})
        self.input_mineru_token = QLineEdit(mineru.get("token", ""))
        self.input_mineru_endpoint = QLineEdit(mineru.get("endpoint", "https://mineru.net/api/v4/file-urls/batch"))
        self.input_mineru_poll = QLineEdit(mineru.get("poll_endpoint", "https://mineru.net/api/v4/extract-results/batch/{batch_id}"))
        self.combo_mineru_language = QComboBox()
        mineru_languages = [
            ("ch", self._text("lang_ch")),
            ("ch_server", self._text("lang_ch_server")),
            ("en", self._text("lang_en")),
            ("japan", self._text("lang_japan")),
            ("korean", self._text("lang_korean")),
            ("chinese_cht", self._text("lang_cht")),
            ("ta", self._text("lang_ta")),
            ("te", self._text("lang_te")),
            ("ka", self._text("lang_ka")),
            ("el", self._text("lang_el")),
            ("th", self._text("lang_th")),
            ("latin", self._text("lang_latin")),
            ("arabic", self._text("lang_arabic")),
            ("cyrillic", self._text("lang_cyrillic")),
            ("east_slavic", self._text("lang_east_slavic")),
            ("devanagari", self._text("lang_devanagari")),
        ]
        for value, label in mineru_languages:
            self.combo_mineru_language.addItem(label, value)
        lang_idx = self.combo_mineru_language.findData(mineru.get("language", "ch"))
        self.combo_mineru_language.setCurrentIndex(lang_idx if lang_idx >= 0 else 0)
        self.input_mineru_model = QLineEdit(mineru.get("model_version", "vlm"))
        self.input_mineru_extra_formats = QLineEdit(mineru.get("extra_formats", ""))
        self.chk_mineru_table = QCheckBox()
        self.chk_mineru_table.setChecked(bool(mineru.get("enable_table", True)))
        self.chk_mineru_formula = QCheckBox()
        self.chk_mineru_formula.setChecked(bool(mineru.get("enable_formula", True)))
        self.chk_mineru_ocr = QCheckBox()
        self.chk_mineru_ocr.setChecked(bool(mineru.get("is_ocr", True)))
        self.chk_mineru_no_cache = QCheckBox()
        self.chk_mineru_no_cache.setChecked(bool(mineru.get("no_cache", False)))
        self.spin_mineru_poll_interval = QDoubleSpinBox()
        self.spin_mineru_poll_interval.setRange(0.5, 30)
        self.spin_mineru_poll_interval.setValue(float(mineru.get("poll_interval", 2)))
        mineru_layout.addRow(self._text("token"), self.input_mineru_token)
        mineru_layout.addRow(self._text("create_endpoint"), self.input_mineru_endpoint)
        mineru_layout.addRow(self._text("poll_endpoint"), self.input_mineru_poll)
        mineru_layout.addRow(self._text("language"), self.combo_mineru_language)
        mineru_layout.addRow(self._text("model_version"), self.input_mineru_model)
        mineru_layout.addRow(self._text("extra_formats"), self.input_mineru_extra_formats)
        mineru_layout.addRow(self._text("enable_table"), self.chk_mineru_table)
        mineru_layout.addRow(self._text("enable_formula"), self.chk_mineru_formula)
        mineru_layout.addRow(self._text("force_ocr"), self.chk_mineru_ocr)
        mineru_layout.addRow(self._text("no_cache"), self.chk_mineru_no_cache)
        mineru_layout.addRow(self._text("poll_interval"), self.spin_mineru_poll_interval)

        quark_page, quark_layout = self._add_ocr_page(self._text("quark_page"))
        quark = engines.setdefault("quark", {})
        self.input_quark_client_id = QLineEdit(quark.get("client_id", ""))
        self.input_quark_client_secret = QLineEdit(quark.get("client_secret", ""))
        self.input_quark_endpoint = QLineEdit(quark.get("endpoint", "https://scan-business.quark.cn/vision"))
        self.combo_quark_function = QComboBox()
        self.combo_quark_function.addItem(self._text("general_document"), "RecognizeGeneralDocument")
        idx = self.combo_quark_function.findData(quark.get("function_option", "RecognizeGeneralDocument"))
        self.combo_quark_function.setCurrentIndex(idx if idx >= 0 else 0)
        self.chk_quark_return_image = QCheckBox()
        self.chk_quark_return_image.setChecked(bool(quark.get("need_return_image", True)))
        self.input_quark_sign_method = QLineEdit(quark.get("sign_method", "SHA3-256"))
        quark_layout.addRow(self._text("client_id"), self.input_quark_client_id)
        quark_layout.addRow(self._text("client_secret"), self.input_quark_client_secret)
        quark_layout.addRow(self._text("endpoint"), self.input_quark_endpoint)
        quark_layout.addRow(self._text("sign_method"), self.input_quark_sign_method)
        quark_layout.addRow(self._text("function"), self.combo_quark_function)
        quark_layout.addRow(self._text("return_image"), self.chk_quark_return_image)

        chrome_page, chrome_layout = self._add_ocr_page("ChromeLens")
        chrome_lens = engines.setdefault("chrome_lens", {})
        chrome_note = chrome_lens.get("note", self._text("chrome_note"))
        if chrome_note in {
            "Requires chrome-lens-py package; no token",
            text_from_config({"ui_lang": "zh"}, "pm_chrome_note"),
            text_from_config({"ui_lang": "en"}, "pm_chrome_note"),
        }:
            chrome_note = self._text("chrome_note")
        self.input_chrome_lens_note = QLineEdit(chrome_note)
        chrome_layout.addRow(self._text("note"), self.input_chrome_lens_note)

        for widget in [
            self.input_textin_app_id, self.input_textin_secret, self.input_textin_endpoint,
            self.input_textin_password, self.input_textin_page_range,
            self.input_mineru_token, self.input_mineru_endpoint, self.input_mineru_poll,
            self.input_mineru_model, self.input_mineru_extra_formats,
            self.input_quark_client_id, self.input_quark_client_secret, self.input_quark_endpoint,
            self.input_quark_sign_method,
            self.input_chrome_lens_note,
        ]:
            widget.textChanged.connect(self.save_global)

        for widget in [
            self.chk_textin_table, self.chk_textin_chars, self.chk_textin_images,
            self.chk_textin_hierarchy, self.chk_textin_inline_objects, self.chk_textin_pages,
            self.chk_textin_title_tree, self.chk_textin_remove_watermark, self.chk_textin_crop_dewarp,
            self.chk_textin_recognize_chemical,
            self.chk_quark_return_image,
            self.chk_mineru_table, self.chk_mineru_formula, self.chk_mineru_ocr, self.chk_mineru_no_cache,
        ]:
            widget.toggled.connect(self.save_global)
        self.combo_textin_table_view.currentIndexChanged.connect(self.save_global)
        self.combo_textin_force_engine.currentIndexChanged.connect(self.save_global)
        self.combo_textin_parse_mode.currentIndexChanged.connect(self.save_global)
        self.combo_textin_image_output_type.currentIndexChanged.connect(self.save_global)
        self.spin_textin_formula_level.valueChanged.connect(self.save_global)
        self.combo_quark_function.currentIndexChanged.connect(self.save_global)
        self.combo_mineru_language.currentIndexChanged.connect(self.save_global)
        self.spin_mineru_poll_interval.valueChanged.connect(self.save_global)
        self.ocr_nav.setCurrentRow(0)

    def _add_ocr_page(self, title):
        self.ocr_nav.addItem(title)
        page = QWidget()
        form = QFormLayout(page)
        self.ocr_pages.addWidget(page)
        return page, form

    def _add_ocr_priority_item(self, engine_id):
        item = QListWidgetItem(ENGINE_DEFS[engine_id].label)
        item.setData(Qt.ItemDataRole.UserRole, engine_id)
        self.list_ocr_priority.addItem(item)

    def move_ocr_priority(self, delta):
        row = self.list_ocr_priority.currentRow()
        new_row = row + delta
        if row < 0 or new_row < 0 or new_row >= self.list_ocr_priority.count():
            return
        item = self.list_ocr_priority.takeItem(row)
        self.list_ocr_priority.insertItem(new_row, item)
        self.list_ocr_priority.setCurrentRow(new_row)
        self.save_global()
        
    def save_global(self):
        g = self.config_manager.get_global()
        if hasattr(self, "input_api_token"):
            g["ocr_api_token"] = self.input_api_token.text()
        if hasattr(self, "spin_retry"):
            g["ocr_retry_count"] = self.spin_retry.value()
        if hasattr(self, "spin_concurrent"):
            g["ocr_concurrent_tasks"] = self.spin_concurrent.value()
        if hasattr(self, "list_excluded_categories"):
            g["ocr_excluded_categories"] = [
                self.list_excluded_categories.item(index).data(Qt.ItemDataRole.UserRole)
                for index in range(self.list_excluded_categories.count())
                if self.list_excluded_categories.item(index).checkState() == Qt.CheckState.Checked
            ]
        if hasattr(self, "list_ocr_priority"):
            g["ocr_result_priority"] = [
                self.list_ocr_priority.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(self.list_ocr_priority.count())
            ]
        if hasattr(self, "chk_paddle_orientation"):
            engines = g.setdefault("ocr_engines", {})
            engines.setdefault("paddleocr", {}).update({
                "useDocOrientationClassify": self.chk_paddle_orientation.isChecked(),
                "useDocUnwarping": self.chk_paddle_unwarp.isChecked(),
                "useChartRecognition": self.chk_paddle_chart.isChecked(),
            })
            engines.setdefault("textin", {}).update({
                "app_id": self.input_textin_app_id.text(),
                "secret_code": self.input_textin_secret.text(),
                "endpoint": self.input_textin_endpoint.text(),
                "password": self.input_textin_password.text(),
                "page_range": self.input_textin_page_range.text(),
                "include_table_structure": self.chk_textin_table.isChecked(),
                "include_char_details": self.chk_textin_chars.isChecked(),
                "include_image_data": self.chk_textin_images.isChecked(),
                "include_hierarchy": self.chk_textin_hierarchy.isChecked(),
                "include_inline_objects": self.chk_textin_inline_objects.isChecked(),
                "pages": self.chk_textin_pages.isChecked(),
                "title_tree": self.chk_textin_title_tree.isChecked(),
                "table_view": self.combo_textin_table_view.currentData(),
                "remove_watermark": self.chk_textin_remove_watermark.isChecked(),
                "crop_dewarp": self.chk_textin_crop_dewarp.isChecked(),
                "force_engine": self.combo_textin_force_engine.currentData(),
                "parse_mode": self.combo_textin_parse_mode.currentData(),
                "formula_level": self.spin_textin_formula_level.value(),
                "recognize_chemical": self.chk_textin_recognize_chemical.isChecked(),
                "image_output_type": self.combo_textin_image_output_type.currentData(),
            })
            engines.setdefault("mineru", {}).update({
                "endpoint": self.input_mineru_endpoint.text(),
                "poll_endpoint": self.input_mineru_poll.text(),
                "token": self.input_mineru_token.text(),
                "language": self.combo_mineru_language.currentData(),
                "model_version": self.input_mineru_model.text(),
                "extra_formats": self.input_mineru_extra_formats.text(),
                "enable_table": self.chk_mineru_table.isChecked(),
                "enable_formula": self.chk_mineru_formula.isChecked(),
                "is_ocr": self.chk_mineru_ocr.isChecked(),
                "no_cache": self.chk_mineru_no_cache.isChecked(),
                "poll_interval": self.spin_mineru_poll_interval.value(),
            })
            engines.setdefault("quark", {}).update({
                "client_id": self.input_quark_client_id.text(),
                "client_secret": self.input_quark_client_secret.text(),
                "endpoint": self.input_quark_endpoint.text(),
                "sign_method": self.input_quark_sign_method.text(),
                "function_option": self.combo_quark_function.currentData(),
                "need_return_image": self.chk_quark_return_image.isChecked(),
            })
            engines.setdefault("chrome_lens", {})["note"] = self.input_chrome_lens_note.text()
        
        if hasattr(self, 'input_furigana'):
            g["shortcut_furigana"] = self.input_furigana.keySequence().toString()
        if hasattr(self, 'input_furigana_left'):
            g["furigana_left_marker"] = self.input_furigana_left.text()
            g["furigana_right_marker"] = self.input_furigana_right.text()
            g["furigana_kana_type"] = self.combo_furigana_kana.currentData()
            g["furigana_use_jmdict_split"] = self.chk_furigana_split.isChecked()
        if hasattr(self, 'inputs_alt'):
            g["shortcuts_alt"] = [le.text() for le in self.inputs_alt]
            
        self.config_manager.save()
        self.settings_changed.emit()

    def init_projects_tab(self):
        layout = QHBoxLayout(self.tab_projects)
        
        # Left: List
        left_layout = QVBoxLayout()
        self.list_projects = QListWidget()
        self.list_projects.currentRowChanged.connect(self.load_selected_project)
        left_layout.addWidget(self.list_projects)
        
        btn_add = QPushButton(self._text("new_project"))
        btn_add.clicked.connect(self.add_project)
        btn_del = QPushButton(self._text("delete_project"))
        btn_del.clicked.connect(self.delete_project)
        
        left_layout.addWidget(btn_add)
        left_layout.addWidget(btn_del)
        
        layout.addLayout(left_layout, 1)
        
        # Right: Details Form
        self.form_widget = QWidget()
        self.form_layout = QFormLayout(self.form_widget)
        
        # 1. Project Name (Editable)
        self.inp_name = QLineEdit()
        self.inp_name.editingFinished.connect(self.save_current_project)
        self.form_layout.addRow(self._text("project_name"), self.inp_name)
        
        # 2. Paths with Browse Buttons
        self.inp_pdf = self.add_browse_row(self._text("pdf_file"), "file", self._text("pdf_filter"))
        self.inp_left_txt = self.add_browse_row(self._text("left_text"), "file", self._text("text_filter"))
        self.inp_right_txt = self.add_browse_row(self._text("right_text"), "file", self._text("text_filter"))
        self.list_right_candidates = QListWidget()
        candidate_buttons = QWidget()
        candidate_buttons_layout = QHBoxLayout(candidate_buttons)
        candidate_buttons_layout.setContentsMargins(0, 0, 0, 0)
        self.btn_add_right_candidate = QPushButton(self._text("add"))
        self.btn_remove_right_candidate = QPushButton(self._text("remove"))
        self.btn_add_right_candidate.clicked.connect(self.add_right_candidate)
        self.btn_remove_right_candidate.clicked.connect(self.remove_right_candidate)
        candidate_buttons_layout.addWidget(self.btn_add_right_candidate)
        candidate_buttons_layout.addWidget(self.btn_remove_right_candidate)
        self.form_layout.addRow(self._text("other_right"), self.list_right_candidates)
        self.form_layout.addRow("", candidate_buttons)
        self.inp_img_dir = self.add_browse_row(self._text("image_dir"), "dir")
        self.inp_ocr_json = self.add_browse_row(self._text("ocr_dir"), "dir")
        self.inp_export_dir = self.add_browse_row(self._text("export_dir"), "dir")
        
        # 3. Numeric Fields
        self.spin_start = QSpinBox(); self.spin_start.setRange(1, 99999)
        self.spin_end = QSpinBox(); self.spin_end.setRange(1, 99999)
        self.spin_offset = QSpinBox(); self.spin_offset.setRange(-99999, 99999)
        
        self.spin_start.valueChanged.connect(self.save_current_project)
        self.spin_end.valueChanged.connect(self.save_current_project)
        self.spin_offset.valueChanged.connect(self.save_current_project)
        
        self.form_layout.addRow(self._text("start_page"), self.spin_start)
        self.form_layout.addRow(self._text("end_page"), self.spin_end)
        self.form_layout.addRow(self._text("page_offset"), self.spin_offset)
        
        # 4. Regex
        self.inp_reg_l = QLineEdit()
        self.inp_reg_r = QLineEdit()
        self.inp_reg_l.editingFinished.connect(self.save_current_project)
        self.inp_reg_r.editingFinished.connect(self.save_current_project)
        
        # Group IDs
        self.spin_reg_grp_l = QSpinBox(); self.spin_reg_grp_l.setRange(0, 99);
        self.spin_reg_grp_r = QSpinBox(); self.spin_reg_grp_r.setRange(0, 99);
        self.spin_reg_grp_l.valueChanged.connect(self.save_current_project)
        self.spin_reg_grp_r.valueChanged.connect(self.save_current_project)

        h_l = QHBoxLayout(); h_l.addWidget(self.inp_reg_l); h_l.addWidget(QLabel(self._text("group"))); h_l.addWidget(self.spin_reg_grp_l)
        h_r = QHBoxLayout(); h_r.addWidget(self.inp_reg_r); h_r.addWidget(QLabel(self._text("group"))); h_r.addWidget(self.spin_reg_grp_r)
        
        self.form_layout.addRow(self._text("regex_left"), h_l)
        self.form_layout.addRow(self._text("regex_right"), h_r)
        
        layout.addWidget(self.form_widget, 2)
        
        self.current_project_original_name = None
        self.refresh_project_list()
        
    def add_browse_row(self, label, mode, filter_str=""):
        widget = QWidget()
        h = QHBoxLayout(widget)
        h.setContentsMargins(0,0,0,0)
        
        line_edit = QLineEdit()
        line_edit.editingFinished.connect(self.save_current_project)
        
        btn = QPushButton("...")
        btn.setFixedWidth(30)
        btn.clicked.connect(lambda: self.browse_path(line_edit, mode, filter_str))
        
        h.addWidget(line_edit)
        h.addWidget(btn)
        
        self.form_layout.addRow(label, widget)
        return line_edit
        
    def browse_path(self, line_edit, mode, filter_str):
        current = line_edit.text()
        path = ""
        if mode == "file":
             path, _ = QFileDialog.getOpenFileName(self, self._text("select_file"), current, filter_str)
        else:
             path = QFileDialog.getExistingDirectory(self, self._text("select_dir"), current)
             
        if path:
            line_edit.setText(path)
            self.save_current_project()

    def refresh_project_list(self):
        self.list_projects.blockSignals(True)
        self.list_projects.clear()
        projects = self.config_manager.get_projects()
        current = self.config_manager.get_active_project()
        
        sel_row = 0
        for i, p in enumerate(projects):
            self.list_projects.addItem(p["name"])
            if p["name"] == current["name"]:
                sel_row = i
                
        # If we just renamed, try to keep selection on renamed item
        if self.current_project_original_name:
             pass
             
        self.list_projects.setCurrentRow(sel_row)
        self.list_projects.blockSignals(False)
        self.load_selected_project() # Force reload fields
        
    def load_selected_project(self):
        row = self.list_projects.currentRow()
        if row < 0: 
            self.form_widget.setEnabled(False)
            return
        
        self.form_widget.setEnabled(True)
        name = self.list_projects.item(row).text()
        p = self.config_manager.get_project(name)
        if not p: return
        
        self.current_project_original_name = name
        
        self.block_signals_inputs(True)
        self.inp_name.setText(p.get("name"))
        self.inp_pdf.setText(p.get("pdf_path", ""))
        self.inp_img_dir.setText(p.get("image_dir", ""))
        self.inp_left_txt.setText(p.get("text_path_left", ""))
        self.inp_right_txt.setText(p.get("text_path_right", ""))
        self.list_right_candidates.clear()
        for candidate in p.get("right_text_candidates", []):
            if isinstance(candidate, dict):
                path = candidate.get("path", "")
                label = candidate.get("label") or os.path.basename(path) or "Candidate"
            else:
                path = str(candidate)
                label = os.path.basename(path) or "Candidate"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.list_right_candidates.addItem(item)
        self.inp_ocr_json.setText(p.get("ocr_json_path", ""))
        self.inp_export_dir.setText(p.get("export_dir", ""))
        
        self.spin_start.setValue(int(p.get("start_page", 1)))
        self.spin_end.setValue(int(p.get("end_page", 1)))
        self.spin_offset.setValue(int(p.get("page_offset", 0)))
        
        self.inp_reg_l.setText(p.get("regex_left", ""))
        self.inp_reg_r.setText(p.get("regex_right", ""))
        self.spin_reg_grp_l.setValue(int(p.get("regex_group_left", 0)))
        self.spin_reg_grp_r.setValue(int(p.get("regex_group_right", 0)))
        self.block_signals_inputs(False)

    def save_current_project(self):
        if not self.current_project_original_name: return
        
        p = self.config_manager.get_project(self.current_project_original_name)
        if not p: return
        
        # 1. Handle Rename
        new_name = self.inp_name.text().strip()
        if new_name and new_name != self.current_project_original_name:
            if self.config_manager.get_project(new_name):
                QMessageBox.warning(self, self._text("error"), self._text("name_exists"))
                self.inp_name.setText(self.current_project_original_name) # Revert
                return
            else:
                p["name"] = new_name
                if self.config_manager.data["active_project"] == self.current_project_original_name:
                    self.config_manager.data["active_project"] = new_name
                
                self.current_project_original_name = new_name
                
        # 2. Save Fields
        p["pdf_path"] = self.inp_pdf.text()
        p["image_dir"] = self.inp_img_dir.text()
        p["text_path_left"] = self.inp_left_txt.text()
        p["text_path_right"] = self.inp_right_txt.text()
        p["right_text_candidates"] = [
            {
                "label": self.list_right_candidates.item(i).text(),
                "path": self.list_right_candidates.item(i).data(Qt.ItemDataRole.UserRole),
            }
            for i in range(self.list_right_candidates.count())
        ]
        p["ocr_json_path"] = self.inp_ocr_json.text()
        p["export_dir"] = self.inp_export_dir.text()
        
        p["start_page"] = self.spin_start.value()
        p["end_page"] = self.spin_end.value()
        p["page_offset"] = self.spin_offset.value()
        
        p["regex_left"] = self.inp_reg_l.text()
        p["regex_right"] = self.inp_reg_r.text()
        p["regex_group_left"] = self.spin_reg_grp_l.value()
        p["regex_group_right"] = self.spin_reg_grp_r.value()
        
        self.config_manager.save()
        
        current_list_item = self.list_projects.currentItem()
        if current_list_item and current_list_item.text() != self.current_project_original_name:
             current_list_item.setText(self.current_project_original_name)

    def block_signals_inputs(self, block):
        inputs = [self.inp_pdf, self.inp_img_dir, self.inp_left_txt, self.inp_right_txt, 
                  self.inp_ocr_json, self.inp_export_dir, self.inp_reg_l, self.inp_reg_r, self.inp_name,
                  self.spin_start, self.spin_end, self.spin_offset,
                  self.spin_reg_grp_l, self.spin_reg_grp_r]
        for inp in inputs:
            if hasattr(inp, 'blockSignals'):
                inp.blockSignals(block)

    def add_right_candidate(self):
        path, _ = QFileDialog.getOpenFileName(self, self._text("select_right"), "", self._text("text_filter"))
        if not path:
            return
        item = QListWidgetItem(os.path.basename(path) or path)
        item.setData(Qt.ItemDataRole.UserRole, path)
        self.list_right_candidates.addItem(item)
        self.save_current_project()

    def remove_right_candidate(self):
        row = self.list_right_candidates.currentRow()
        if row < 0:
            return
        self.list_right_candidates.takeItem(row)
        self.save_current_project()

    def add_project(self):
        name, ok = QInputDialog.getText(self, self._text("new_project"), self._text("project_name"))
        if ok and name:
            if self.config_manager.create_project(name):
                self.refresh_project_list()
                items = self.list_projects.findItems(name, Qt.MatchFlag.MatchExactly)
                if items:
                    self.list_projects.setCurrentItem(items[0])
            else:
                QMessageBox.warning(self, self._text("error"), self._text("name_invalid"))

    def delete_project(self):
        row = self.list_projects.currentRow()
        if row < 0: return
        name = self.list_projects.item(row).text()
        
        ret = QMessageBox.question(self, self._text("delete_project"), self._text("confirm_delete", name=name), QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ret == QMessageBox.StandardButton.Yes:
            if self.config_manager.delete_project(name):
                self.refresh_project_list()
            else:
                QMessageBox.warning(self, self._text("error"), self._text("cannot_delete_last"))
