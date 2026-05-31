import os
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QWidget,
                             QDialogButtonBox, QFormLayout, QLineEdit, QKeySequenceEdit,
                             QListWidget, QPushButton, QSpinBox, QLabel, QFileDialog,
                             QInputDialog, QMessageBox, QComboBox)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeySequence
from ocr.ocr_worker import refresh_remote_engine_label

class ProjectManagerDialog(QDialog):
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
        
        self.input_api_token = QLineEdit()
        self.spin_retry = QSpinBox()
        self.spin_retry.setRange(1, 10)
        self.spin_concurrent = QSpinBox()
        self.spin_concurrent.setRange(1, 8)
        
        self.input_furigana = QKeySequenceEdit()
        self.inputs_alt = []
        
        # Load values
        g = self.config_manager.get_global()
        self.input_api_token.setText(g.get("ocr_api_token", ""))
        self.spin_retry.setValue(int(g.get("ocr_retry_count", 3)))
        self.spin_concurrent.setValue(int(g.get("ocr_concurrent_tasks", 2)))
        
        # Connect save
        self.input_api_token.textChanged.connect(self.save_global)
        self.spin_retry.valueChanged.connect(self.save_global)
        self.spin_concurrent.valueChanged.connect(self.save_global)
        
        layout.addRow("OCR API Token:", self.input_api_token)
        layout.addRow("Retry Count:", self.spin_retry)
        layout.addRow("Concurrent Tasks:", self.spin_concurrent)
        
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
        
    def save_global(self):
        g = self.config_manager.get_global()
        g["ocr_api_token"] = self.input_api_token.text()
        g["ocr_retry_count"] = self.spin_retry.value()
        g["ocr_concurrent_tasks"] = self.spin_concurrent.value()
        
        if hasattr(self, 'input_furigana'):
            g["shortcut_furigana"] = self.input_furigana.keySequence().toString()
        if hasattr(self, 'inputs_alt'):
            g["shortcuts_alt"] = [le.text() for le in self.inputs_alt]
            
        self.config_manager.save()

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
