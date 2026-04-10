import os
import re
import json
import html
import difflib

from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QPushButton, QLineEdit, QTextEdit, QPlainTextEdit,
    QCheckBox, QComboBox, QListWidget, QListWidgetItem,
    QTableView, QHeaderView, QAbstractItemView,
    QGroupBox, QRadioButton, QTabWidget,
    QMessageBox, QProgressDialog, QWidget, QStyle,
    QStyledItemDelegate, QSizePolicy, QSplitter
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QAbstractTableModel, QModelIndex, QSize, QRect, QPoint
from PyQt6.QtGui import (
    QColor, QFont, QTextDocument, QAbstractTextDocumentLayout,
    QTextCursor, QKeySequence, QShortcut
)

class TemplateManager:
    def __init__(self, filename="replace_templates.json"):
        self.filename = filename
        self.templates = {} # {name: [rules]}
        self.load()
        
    def load(self):
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r', encoding='utf-8') as f:
                    self.templates = json.load(f)
            except: self.templates = {}
        else:
            self.templates = {}
            
    def save(self):
        try:
            with open(self.filename, 'w', encoding='utf-8') as f:
                json.dump(self.templates, f, ensure_ascii=False, indent=2)
        except: pass
        
    def get_template_names(self):
        return sorted(list(self.templates.keys()))
        
    def get_rules(self, name):
        return self.templates.get(name, [])
        
    def set_template(self, name, rules):
        self.templates[name] = rules
        self.save()

    def delete_template(self, name):
        if name in self.templates:
            del self.templates[name]
            self.save()


class TemplateEditorDialog(QDialog):
    def __init__(self, parent=None, template_manager=None, template_name=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Template")
        self.resize(700, 500)
        self.manager = template_manager
        self.current_name = template_name
        self.rules = []
        if template_name:
            import copy
            self.rules = copy.deepcopy(self.manager.get_rules(template_name))
            
        self.init_ui()
        
    def init_ui(self):
        layout = QVBoxLayout(self)
        
        # Name
        h_name = QHBoxLayout()
        h_name.addWidget(QLabel("Template Name:"))
        self.txt_name = QLineEdit()
        if self.current_name: self.txt_name.setText(self.current_name)
        h_name.addWidget(self.txt_name)
        layout.addLayout(h_name)
        

        
        # Rules List
        self.list_rules = QListWidget()
        self.list_rules.itemDoubleClicked.connect(self.edit_rule)
        layout.addWidget(self.list_rules)
        self.refresh_list()
        
        # Buttons
        h_btns = QHBoxLayout()
        btn_add = QPushButton("Add Rule")
        btn_add.clicked.connect(self.add_rule)
        btn_del = QPushButton("Delete Rule")
        btn_del.clicked.connect(self.delete_rule)
        btn_up = QPushButton("Move Up")
        btn_up.clicked.connect(self.move_up)
        btn_down = QPushButton("Move Down")
        btn_down.clicked.connect(self.move_down)
        
        h_btns.addWidget(btn_add)
        h_btns.addWidget(btn_del)
        h_btns.addWidget(btn_up)
        h_btns.addWidget(btn_down)
        layout.addLayout(h_btns)
        
        # Dialog Buttons
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.save_and_close)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)
        
    def refresh_list(self):
        self.list_rules.clear()
        for i, rule in enumerate(self.rules):
            # Summary string
            pat = rule.get('find', '')
            repl = rule.get('replace', '')
            mode = rule.get('search_mode', 'Normal')
            item = QListWidgetItem(f"{i+1}. [{mode}] '{pat}' -> '{repl}'")
            item.setData(Qt.ItemDataRole.UserRole, i)
            self.list_rules.addItem(item)
            
    def add_rule(self):
        # Open simple dialog to get rule details
        dlg = RuleEditDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.rules.append(dlg.get_data())
            self.refresh_list()
            
    def edit_rule(self, item):
        idx = item.data(Qt.ItemDataRole.UserRole)
        rule = self.rules[idx]
        dlg = RuleEditDialog(self, rule)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.rules[idx] = dlg.get_data()
            self.refresh_list()
            
    def delete_rule(self):
        row = self.list_rules.currentRow()
        if row >= 0:
            self.rules.pop(row)
            self.refresh_list()
            
    def move_up(self):
        row = self.list_rules.currentRow()
        if row > 0:
            self.rules[row], self.rules[row-1] = self.rules[row-1], self.rules[row]
            self.refresh_list()
            self.list_rules.setCurrentRow(row-1)

    def move_down(self):
        row = self.list_rules.currentRow()
        if row < len(self.rules) - 1:
            self.rules[row], self.rules[row+1] = self.rules[row+1], self.rules[row]
            self.refresh_list()
            self.list_rules.setCurrentRow(row+1)

    def save_and_close(self):
        name = self.txt_name.text().strip()
        if not name:
            QMessageBox.warning(self, "Error", "Template Name required")
            return
            
        self.manager.set_template(name, self.rules)
        self.accept()


class RuleEditDialog(QDialog):
    def __init__(self, parent=None, rule_data=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Rule")
        self.rule = rule_data or {}
        self.init_ui()
        
    def init_ui(self):
        layout = QFormLayout(self)
        
        self.txt_find = QLineEdit(self.rule.get('find', ''))
        self.txt_repl = QLineEdit(self.rule.get('replace', ''))
        
        self.cmb_search_mode = QComboBox()
        self.cmb_search_mode.addItems(["Normal", "Extended", "Regex"])
        self.cmb_search_mode.setCurrentText(self.rule.get('search_mode', 'Normal'))
        
        self.cmb_repl_mode = QComboBox()
        self.cmb_repl_mode.addItems(["Normal", "Regex Group", "Python Lambda", "Translate"])
        self.cmb_repl_mode.setCurrentText(self.rule.get('replace_mode', 'Normal'))
        
        self.chk_case = QCheckBox("Case Sensitive")
        self.chk_case.setChecked(self.rule.get('case_sensitive', False))
        
        self.chk_word = QCheckBox("Whole Word")
        self.chk_word.setChecked(self.rule.get('whole_word', False))
        
        self.txt_source = QLineEdit(self.rule.get('source', ''))
        
        layout.addRow("Find / Filter:", self.txt_find)
        self.lbl_source = QLabel("Source Chars:")
        self.lbl_find = layout.labelForField(self.txt_find)
        
        # We need custom insertion for Source Chars row to be able to hide it?
        # FormLayout allows hiding rows? Yes setVisible on widgets.
        
        layout.addRow(self.lbl_source, self.txt_source)
        layout.addRow("Replace / Target:", self.txt_repl)
        self.lbl_replace = layout.labelForField(self.txt_repl)
        
        layout.addRow("Search Mode:", self.cmb_search_mode)
        layout.addRow("Replace Mode:", self.cmb_repl_mode)
        layout.addRow("", self.chk_case)
        layout.addRow("", self.chk_word)
        
        self.cmb_repl_mode.currentTextChanged.connect(self.update_ui)
        self.update_ui(self.cmb_repl_mode.currentText())
        
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addRow(btns)

    def update_ui(self, mode):
        is_translate = "Translate" in mode
        self.lbl_source.setVisible(is_translate)
        self.txt_source.setVisible(is_translate)
        
        if is_translate:
            self.lbl_find.setText("Filter (Opt):")
            self.lbl_replace.setText("Target Chars:")
            self.txt_find.setPlaceholderText("Filter Pattern (Optional)")
        else:
            self.lbl_find.setText("Find:")
            self.lbl_replace.setText("Replace:")
            self.txt_find.setPlaceholderText("")
        
    def get_data(self):
        return {
            'find': self.txt_find.text(),
            'replace': self.txt_repl.text(),
            'source': self.txt_source.text(),
            'search_mode': self.cmb_search_mode.currentText(),
            'replace_mode': self.cmb_repl_mode.currentText(),
            'case_sensitive': self.chk_case.isChecked(),
            'whole_word': self.chk_word.isChecked()
        }


