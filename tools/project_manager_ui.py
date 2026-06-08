import os
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QWidget,
                             QDialogButtonBox, QFormLayout, QLineEdit, QKeySequenceEdit,
                             QListWidget, QPushButton, QSpinBox, QLabel, QFileDialog,
                             QInputDialog, QMessageBox, QComboBox, QTextEdit, QStackedWidget,
                             QCheckBox, QDoubleSpinBox)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QKeySequence
from ocr.ocr_worker import refresh_remote_engine_label

class ProjectManagerDialog(QDialog):
    settings_changed = pyqtSignal()

    def __init__(self, parent, config_manager):
        super().__init__(parent)
        self.setWindowTitle("Settings & Project Manager")
        self.resize(800, 600)
        self.config_manager = config_manager
        
        # UI Layout
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        
        # Tab 1: Global Settings
        self.tab_global = QWidget()
        self.init_global_tab()
        self.tabs.addTab(self.tab_global, "Global Settings")

        self.tab_ocr = QWidget()
        self.init_ocr_tab()
        self.tabs.addTab(self.tab_ocr, "OCR Engines")
        
        # Tab 2: Projects
        self.tab_projects = QWidget()
        self.init_projects_tab()
        self.tabs.addTab(self.tab_projects, "Projects")
        
        # Buttons
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
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
        layout.addRow("Furigana Shortcut:", self.input_furigana)
        
        alt_texts = g.get("shortcuts_alt", [""] * 10)
        for i in range(10):
            le = QLineEdit()
            le.setText(alt_texts[i] if i < len(alt_texts) else "")
            le.textChanged.connect(self.save_global)
            self.inputs_alt.append(le)
            layout.addRow(f"Alt+{i} Text:", le)

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
        common_page, common_layout = self._add_ocr_page("通用设置")
        self.spin_retry = QSpinBox()
        self.spin_retry.setRange(1, 10)
        self.spin_retry.setValue(int(g.get("ocr_retry_count", 3)))
        self.spin_retry.valueChanged.connect(self.save_global)

        self.spin_concurrent = QSpinBox()
        self.spin_concurrent.setRange(1, 8)
        self.spin_concurrent.setValue(int(g.get("ocr_concurrent_tasks", 2)))
        self.spin_concurrent.valueChanged.connect(self.save_global)

        self.input_excluded_labels = QLineEdit()
        self.input_excluded_labels.setText(g.get("ocr_excluded_labels", "image,table,formula,Illustration,PrintedFormula,WrittenFormula"))
        self.input_excluded_labels.textChanged.connect(self.save_global)

        common_layout.addRow("Retry Count:", self.spin_retry)
        common_layout.addRow("Concurrent Tasks:", self.spin_concurrent)
        common_layout.addRow("Excluded Labels:", self.input_excluded_labels)

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
        paddle_layout.addRow("API Token:", self.input_api_token)
        paddle_layout.addRow("Use Orientation Classify:", self.chk_paddle_orientation)
        paddle_layout.addRow("Use Doc Unwarping:", self.chk_paddle_unwarp)
        paddle_layout.addRow("Use Chart Recognition:", self.chk_paddle_chart)

        textin_page, textin_layout = self._add_ocr_page("Textin")
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
        for value in ["html", "markdown"]:
            self.combo_textin_table_view.addItem(value, value)
        idx = self.combo_textin_table_view.findData(textin.get("table_view", "html"))
        self.combo_textin_table_view.setCurrentIndex(idx if idx >= 0 else 0)
        self.combo_textin_force_engine = QComboBox()
        self.combo_textin_force_engine.addItem("默认", "")
        for value in ["textin", "mineru", "paddle_ocr", "textin_gui"]:
            self.combo_textin_force_engine.addItem(value, value)
        idx = self.combo_textin_force_engine.findData(textin.get("force_engine", ""))
        self.combo_textin_force_engine.setCurrentIndex(idx if idx >= 0 else 0)
        self.combo_textin_parse_mode = QComboBox()
        for value in ["auto", "scan", "parse", "lite", "vlm"]:
            self.combo_textin_parse_mode.addItem(value, value)
        idx = self.combo_textin_parse_mode.findData(textin.get("parse_mode", "auto"))
        self.combo_textin_parse_mode.setCurrentIndex(idx if idx >= 0 else 0)
        self.spin_textin_formula_level = QSpinBox()
        self.spin_textin_formula_level.setRange(0, 1)
        self.spin_textin_formula_level.setValue(int(textin.get("formula_level", 0)))
        self.chk_textin_recognize_chemical = QCheckBox()
        self.chk_textin_recognize_chemical.setChecked(bool(textin.get("recognize_chemical", False)))
        self.combo_textin_image_output_type = QComboBox()
        for value in ["url", "base64"]:
            self.combo_textin_image_output_type.addItem(value, value)
        idx = self.combo_textin_image_output_type.findData(textin.get("image_output_type", "url"))
        self.combo_textin_image_output_type.setCurrentIndex(idx if idx >= 0 else 0)
        textin_layout.addRow("App ID:", self.input_textin_app_id)
        textin_layout.addRow("Secret Code:", self.input_textin_secret)
        textin_layout.addRow("Endpoint:", self.input_textin_endpoint)
        textin_layout.addRow("PDF Password:", self.input_textin_password)
        textin_layout.addRow("Page Range:", self.input_textin_page_range)
        textin_layout.addRow("Include Hierarchy:", self.chk_textin_hierarchy)
        textin_layout.addRow("Include Inline Objects:", self.chk_textin_inline_objects)
        textin_layout.addRow("Include Table Structure:", self.chk_textin_table)
        textin_layout.addRow("Include Char Details:", self.chk_textin_chars)
        textin_layout.addRow("Include Image Data:", self.chk_textin_images)
        textin_layout.addRow("Return Pages:", self.chk_textin_pages)
        textin_layout.addRow("Title Tree:", self.chk_textin_title_tree)
        textin_layout.addRow("Table View:", self.combo_textin_table_view)
        textin_layout.addRow("Remove Watermark:", self.chk_textin_remove_watermark)
        textin_layout.addRow("Crop Dewarp:", self.chk_textin_crop_dewarp)
        textin_layout.addRow("Force Engine:", self.combo_textin_force_engine)
        textin_layout.addRow("Parse Mode:", self.combo_textin_parse_mode)
        textin_layout.addRow("Formula Level:", self.spin_textin_formula_level)
        textin_layout.addRow("Recognize Chemical:", self.chk_textin_recognize_chemical)
        textin_layout.addRow("Image Output Type:", self.combo_textin_image_output_type)

        mineru_page, mineru_layout = self._add_ocr_page("MinerU")
        mineru = engines.setdefault("mineru", {})
        self.input_mineru_token = QLineEdit(mineru.get("token", ""))
        self.input_mineru_endpoint = QLineEdit(mineru.get("endpoint", "https://mineru.net/api/v4/file-urls/batch"))
        self.input_mineru_poll = QLineEdit(mineru.get("poll_endpoint", "https://mineru.net/api/v4/extract-results/batch/{batch_id}"))
        self.combo_mineru_language = QComboBox()
        mineru_languages = [
            ("ch", "ch - 中英文（默认）"),
            ("ch_server", "ch_server - 繁体、手写体"),
            ("en", "en - 纯英文"),
            ("japan", "japan - 日文为主"),
            ("korean", "korean - 韩文"),
            ("chinese_cht", "chinese_cht - 繁体中文为主"),
            ("ta", "ta - 泰米尔文"),
            ("te", "te - 泰卢固文"),
            ("ka", "ka - 卡纳达文"),
            ("el", "el - 希腊文"),
            ("th", "th - 泰文"),
            ("latin", "latin - 拉丁语系"),
            ("arabic", "arabic - 阿拉伯语系"),
            ("cyrillic", "cyrillic - 西里尔语系"),
            ("east_slavic", "east_slavic - 东斯拉夫语系"),
            ("devanagari", "devanagari - 天城文语系"),
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
        mineru_layout.addRow("Token:", self.input_mineru_token)
        mineru_layout.addRow("Create Endpoint:", self.input_mineru_endpoint)
        mineru_layout.addRow("Poll Endpoint:", self.input_mineru_poll)
        mineru_layout.addRow("Language:", self.combo_mineru_language)
        mineru_layout.addRow("Model Version:", self.input_mineru_model)
        mineru_layout.addRow("Extra Formats:", self.input_mineru_extra_formats)
        mineru_layout.addRow("Enable Table:", self.chk_mineru_table)
        mineru_layout.addRow("Enable Formula:", self.chk_mineru_formula)
        mineru_layout.addRow("Force OCR:", self.chk_mineru_ocr)
        mineru_layout.addRow("No Cache:", self.chk_mineru_no_cache)
        mineru_layout.addRow("Poll Interval:", self.spin_mineru_poll_interval)

        quark_page, quark_layout = self._add_ocr_page("Quark")
        quark = engines.setdefault("quark", {})
        self.input_quark_client_id = QLineEdit(quark.get("client_id", ""))
        self.input_quark_client_secret = QLineEdit(quark.get("client_secret", ""))
        self.input_quark_endpoint = QLineEdit(quark.get("endpoint", "https://scan-business.quark.cn/vision"))
        self.combo_quark_function = QComboBox()
        for value in ["RecognizeGeneralDocument"]:
            self.combo_quark_function.addItem(value, value)
        idx = self.combo_quark_function.findData(quark.get("function_option", "RecognizeGeneralDocument"))
        self.combo_quark_function.setCurrentIndex(idx if idx >= 0 else 0)
        self.chk_quark_return_image = QCheckBox()
        self.chk_quark_return_image.setChecked(bool(quark.get("need_return_image", True)))
        self.input_quark_sign_method = QLineEdit(quark.get("sign_method", "SHA3-256"))
        quark_layout.addRow("Client ID:", self.input_quark_client_id)
        quark_layout.addRow("Client Secret:", self.input_quark_client_secret)
        quark_layout.addRow("Endpoint:", self.input_quark_endpoint)
        quark_layout.addRow("Sign Method:", self.input_quark_sign_method)
        quark_layout.addRow("Function:", self.combo_quark_function)
        quark_layout.addRow("Return Image:", self.chk_quark_return_image)

        chrome_page, chrome_layout = self._add_ocr_page("Chrome Lens")
        chrome_lens = engines.setdefault("chrome_lens", {})
        self.input_chrome_lens_note = QLineEdit(chrome_lens.get("note", "Requires chrome-lens-py package; no token"))
        chrome_layout.addRow("Note:", self.input_chrome_lens_note)

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
        
    def save_global(self):
        g = self.config_manager.get_global()
        if hasattr(self, "input_api_token"):
            g["ocr_api_token"] = self.input_api_token.text()
        if hasattr(self, "spin_retry"):
            g["ocr_retry_count"] = self.spin_retry.value()
        if hasattr(self, "spin_concurrent"):
            g["ocr_concurrent_tasks"] = self.spin_concurrent.value()
        if hasattr(self, "input_excluded_labels"):
            g["ocr_excluded_labels"] = self.input_excluded_labels.text()
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
        
        btn_add = QPushButton("New Project")
        btn_add.clicked.connect(self.add_project)
        btn_del = QPushButton("Delete Project")
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
        self.form_layout.addRow("Name:", self.inp_name)
        
        # 2. Paths with Browse Buttons
        self.inp_pdf = self.add_browse_row("PDF Path:", "file", "PDF Files (*.pdf)")
        self.inp_left_txt = self.add_browse_row("Left Text:", "file", "Text (*.txt)")
        self.inp_right_txt = self.add_browse_row("Right Text:", "file", "Text (*.txt)")
        self.inp_img_dir = self.add_browse_row("Image Dir:", "dir")
        self.inp_ocr_json = self.add_browse_row("OCR JSON Dir:", "dir")
        self.inp_export_dir = self.add_browse_row("Export Dir:", "dir")
        
        # 3. Numeric Fields
        self.spin_start = QSpinBox(); self.spin_start.setRange(1, 9999)
        self.spin_end = QSpinBox(); self.spin_end.setRange(1, 9999)
        self.spin_offset = QSpinBox(); self.spin_offset.setRange(-999, 999)
        
        self.spin_start.valueChanged.connect(self.save_current_project)
        self.spin_end.valueChanged.connect(self.save_current_project)
        self.spin_offset.valueChanged.connect(self.save_current_project)
        
        self.form_layout.addRow("Start Page:", self.spin_start)
        self.form_layout.addRow("End Page:", self.spin_end)
        self.form_layout.addRow("Page Offset:", self.spin_offset)
        
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

        h_l = QHBoxLayout(); h_l.addWidget(self.inp_reg_l); h_l.addWidget(QLabel("Grp:")); h_l.addWidget(self.spin_reg_grp_l)
        h_r = QHBoxLayout(); h_r.addWidget(self.inp_reg_r); h_r.addWidget(QLabel("Grp:")); h_r.addWidget(self.spin_reg_grp_r)
        
        self.form_layout.addRow("Regex Left:", h_l)
        self.form_layout.addRow("Regex Right:", h_r)
        
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
             path, _ = QFileDialog.getOpenFileName(self, "Select File", current, filter_str)
        else:
             path = QFileDialog.getExistingDirectory(self, "Select Directory", current)
             
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
                QMessageBox.warning(self, "Error", "Project name already exists!")
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

    def add_project(self):
        name, ok = QInputDialog.getText(self, "New Project", "Project Name:")
        if ok and name:
            if self.config_manager.create_project(name):
                self.refresh_project_list()
                items = self.list_projects.findItems(name, Qt.MatchFlag.MatchExactly)
                if items:
                    self.list_projects.setCurrentItem(items[0])
            else:
                QMessageBox.warning(self, "Error", "Project name exists or invalid")

    def delete_project(self):
        row = self.list_projects.currentRow()
        if row < 0: return
        name = self.list_projects.item(row).text()
        
        ret = QMessageBox.question(self, "Delete", f"Delete project '{name}'?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ret == QMessageBox.StandardButton.Yes:
            if self.config_manager.delete_project(name):
                self.refresh_project_list()
            else:
                QMessageBox.warning(self, "Error", "Cannot delete the last project")
