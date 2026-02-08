import sys
import os
import json
import re
import difflib
import requests
import base64
import time
import html

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                             QLabel, QLineEdit, QPushButton, QListWidget, QListWidgetItem, 
                             QCheckBox, QDialogButtonBox, QMessageBox, QComboBox, QFormLayout, 
                             QTabWidget, QGroupBox, QRadioButton, QTableView, QAbstractItemView, 
                             QHeaderView, QSizePolicy, QStyledItemDelegate, QProgressDialog, 
                             QGridLayout, QDialog, QStyle, QStyleOptionViewItem)
from PyQt6.QtCore import (Qt, pyqtSignal, QAbstractTableModel, QThread, QPoint, QRect, QSize, 
                          QModelIndex)
from PyQt6.QtGui import (QTextDocument, QAbstractTextDocumentLayout, QColor, QTextCursor, 
                         QAction, QIcon)

# Import TemplateManager from core
from ocr_lib.core.templates import TemplateManager

# ==========================================
# Review Model / View
# ==========================================
class ReviewTableModel(QAbstractTableModel):
    def __init__(self, data):
        super().__init__()
        self._data = data # List of dicts
        self._headers = ["", "Page", "Line", "Col", "Change Context"]

    def rowCount(self, parent=None):
        return len(self._data)

    def columnCount(self, parent=None):
        return 5

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid(): return None
        row = index.row()
        col = index.column()
        item = self._data[row]
        
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 1: return str(item['page_num'])
            if col == 2: return str(item.get('line', ''))
            if col == 3: return str(item.get('col', ''))
            # Col 4 Handled by Delegate
            return None
        
        if role == Qt.ItemDataRole.CheckStateRole and col == 0:
            return Qt.CheckState.Checked if item['checked'] else Qt.CheckState.Unchecked
            
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == 0:
            self._data[index.row()]['checked'] = (value == Qt.CheckState.Checked.value)
            self.dataChanged.emit(index, index, [role])
            return True
        return False

    def headerData(self, section, orientation, role):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self._headers[section]
        return None

    def flags(self, index):
        f = super().flags(index)
        if index.column() == 0:
            f |= Qt.ItemFlag.ItemIsUserCheckable
        return f

class HtmlDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        if index.column() == 4:
            painter.save()
            
            doc = QTextDocument()
            html = index.model()._data[index.row()].get('context_html', '')
            
            # Subtract padding
            width = option.rect.width() - 10
            if width <= 0: width = 200
            
            doc.setHtml(html)
            doc.setTextWidth(width)
            doc.setDefaultFont(option.font)
            
            painter.translate(option.rect.topLeft() + QPoint(5, 5))
            
            # Custom Selection Highlight
            if option.state & QStyle.StateFlag.State_Selected:
                 painter.fillRect(QRect(-5, -5, width+10, int(doc.size().height())+10), QColor("#E0E0FF"))
            
            ctx = QAbstractTextDocumentLayout.PaintContext()
            doc.documentLayout().draw(painter, ctx)
            painter.restore()
        else:
            super().paint(painter, option, index)

    def sizeHint(self, option, index):
        if index.column() == 4:
            doc = QTextDocument()
            doc.setHtml(index.model()._data[index.row()].get('context_html', ''))
            
            # Use specific column width from the view if available
            width = option.rect.width()
            if self.parent(): # Assuming parent is the view
                 width = self.parent().columnWidth(4)
                 
            # Allow some padding in calculation
            text_width = width - 10
            if text_width <= 50: text_width = 400 # Default fallback
            
            doc.setTextWidth(text_width)
            doc.setDefaultFont(option.font)
            
            h = int(doc.size().height())
            return QSize(int(doc.idealWidth()), h + 15) # Add padding
        return super().sizeHint(option, index)

# ==========================================
# Review Diff Worker
# ==========================================
class ReviewDiffWorker(QThread):
    progress = pyqtSignal(int)
    finished = pyqtSignal(list, str) # items, msg
    
    def __init__(self, pages, pages_left, pages_right, target_is_left, 
                 regex_old, regex_new, 
                 check_insert, check_delete, check_replace):
        super().__init__()
        self.pages = pages
        self.pages_left = pages_left
        self.pages_right = pages_right
        self.target_is_left = target_is_left
        self.regex_old = regex_old
        self.regex_new = regex_new
        self.chk_insert = check_insert
        self.chk_delete = check_delete
        self.chk_replace = check_replace
        
        self.is_running = True
        self.items = []
        
    def run(self):
        try:
            total = len(self.pages)
            for i, p in enumerate(self.pages):
                if self.isInterruptionRequested(): break
                
                self.progress.emit(i + 1)
                
                # Get Texts
                t_l = self.pages_left.get(p, "")
                t_r = self.pages_right.get(p, "")
                
                text_a = ""
                text_b = ""
                if self.target_is_left:
                     # Target=Left. Turn Left into Right.
                     text_a = t_l
                     text_b = t_r
                else:
                     # Target=Right. Turn Right into Left.
                     text_a = t_r
                     text_b = t_l
                
                self._generate_diff_items(p, text_a, text_b)
                
            self.finished.emit(self.items, "Done")
            
        except Exception as e:
            self.finished.emit([], str(e))
            
    def _generate_diff_items(self, page_num, text_a, text_b):
        matcher = difflib.SequenceMatcher(None, text_a, text_b, autojunk=False)
        opcodes = matcher.get_opcodes()
        
        import html
        
        for tag, i1, i2, j1, j2 in opcodes:
             if self.isInterruptionRequested(): break
             if tag == 'equal': continue
            
             # Check Types
             if tag == 'replace' and not self.chk_replace: continue
             if tag == 'delete' and not self.chk_delete: continue
             if tag == 'insert' and not self.chk_insert: continue
            
             old_segment = text_a[i1:i2]
             new_segment = text_b[j1:j2]
            
             # Regex Filters
             if self.regex_old and old_segment:
                 if not self.regex_old.search(old_segment): continue
             if self.regex_old and not old_segment: continue
            
             if self.regex_new and new_segment:
                 if not self.regex_new.search(new_segment): continue
             if self.regex_new and not new_segment: continue
            
             # Context
             c_start = max(0, i1 - 10)
             c_end = min(len(text_a), i2 + 10)
             prefix = html.escape(text_a[c_start:i1])
             suffix = html.escape(text_a[i2:c_end])
             seg_old_esc = html.escape(old_segment)
             seg_new_esc = html.escape(new_segment)
             
             style_del = "background-color:#ffcccc; text-decoration:line-through;"
             style_ins = "background-color:#ccffcc;"
            
             diff_html = ""
             if tag == 'replace':
                  diff_html = f"{prefix}<span style='{style_del}'>{seg_old_esc}</span> <span style='{style_ins}'>{seg_new_esc}</span>{suffix}"
             elif tag == 'delete':
                  diff_html = f"{prefix}<span style='{style_del}'>{seg_old_esc}</span>{suffix}"
             elif tag == 'insert':
                  diff_html = f"{prefix}<span style='{style_ins}'>{seg_new_esc}</span>{suffix}"
            
             item_data = {
                'page_num': page_num,
                'span': (i1, i2), 
                'original': old_segment,
                'new': new_segment,
                'context_html': diff_html,
                'checked': True
             }
             self.items.append(item_data)

# ==========================================
# Template Editor Dialog
# ==========================================
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


class ReviewDialog(QDialog):
    def __init__(self, parent=None, replacements=None):
        super().__init__(parent)
        self.setWindowTitle("Review Replacements")
        self.resize(800, 600)
        self.replacements = replacements if replacements else []
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        
        # Summary
        layout.addWidget(QLabel(f"Found {len(self.replacements)} replacements. Uncheck to skip."))
        
        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget)
        
        # Controls
        btn_layout = QHBoxLayout()
        btn_all = QPushButton("Select All"); btn_all.clicked.connect(self.select_all)
        btn_none = QPushButton("Deselect All"); btn_none.clicked.connect(self.deselect_all)
        btn_layout.addWidget(btn_all)
        btn_layout.addWidget(btn_none)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)
        
        # Populate
        for item in self.replacements:
            self.add_item(item)
            
        # Dialog Buttons
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        
    def add_item(self, data):
        lw_item = QListWidgetItem(self.list_widget)
        widget = QWidget()
        hbox = QHBoxLayout(widget)
        hbox.setContentsMargins(2, 2, 2, 2)
        
        cb = QCheckBox()
        cb.setChecked(True)
        cb.toggled.connect(lambda c: self.update_check(data, c))
        
        # Context Label (Rich Text)
        lbl = QLabel(data.get('context_html', ''))
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setStyleSheet("border-bottom: 1px solid #eee;")
        
        # Page info
        lbl_info = QLabel(f"Page {data.get('page_num')}")
        lbl_info.setFixedWidth(60)
        lbl_info.setStyleSheet("color: #888;")
        
        hbox.addWidget(cb)
        hbox.addWidget(lbl_info)
        hbox.addWidget(lbl, 1) # Stretch label
        
        widget.setLayout(hbox)
        lw_item.setSizeHint(widget.sizeHint())
        self.list_widget.setItemWidget(lw_item, widget)
        
    def update_check(self, data, checked):
        data['checked'] = checked
        
    def select_all(self):
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            widget = self.list_widget.itemWidget(item)
            cb = widget.findChild(QCheckBox)
            if cb: cb.setChecked(True)
            
    def deselect_all(self):
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            widget = self.list_widget.itemWidget(item)
            cb = widget.findChild(QCheckBox)
            if cb: cb.setChecked(False)

# ==========================================
# Find Replace Dialog
# ==========================================
class FindReplaceDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Find and Replace")
        self.resize(500, 450)
        self.setModal(False)
        self.mainwindow = parent
        self.diff_worker_thread = None
        self.current_review_items = []
        self.review_is_global = False
        
        self.init_ui()

    def init_ui(self):
        main_layout = QVBoxLayout(self)
        
        # Tabs for Modes
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)
        
        # Tab 1: Standard
        self.tab_standard = QWidget()
        self.init_tab_standard()
        self.tabs.addTab(self.tab_standard, "Standard / Regex")
        
        self.tab_batch = QWidget()
        self.init_tab_batch()
        self.tabs.addTab(self.tab_batch, "Batch / Templates")
        
        # Tab: Diff Filter
        self.tab_diff = QWidget()
        self.init_tab_diff()
        self.tabs.addTab(self.tab_diff, "Diff Filter")
        
        # Scope Selection
        self.scope_group = QGroupBox("Target Text")
        scope_layout = QHBoxLayout(self.scope_group)
        self.rb_left = QRadioButton("Left Text")
        self.rb_right = QRadioButton("Right Text")
        self.rb_right.setChecked(True)
        scope_layout.addWidget(self.rb_left)
        scope_layout.addWidget(self.rb_right)
        main_layout.addWidget(self.scope_group)
        
        # Review Area (Integrated)
        self.review_group = QGroupBox("Review Replacements")
        self.review_group.setVisible(False)
        rev_layout = QVBoxLayout(self.review_group)
        
        # Selection Controls (Wrapper for hiding)
        self.review_top_widget = QWidget()
        h_sel = QHBoxLayout(self.review_top_widget)
        h_sel.setContentsMargins(0,0,0,0)
        
        self.btn_sel_all = QPushButton("Select All")
        self.btn_sel_all.clicked.connect(self.select_all_review)
        self.btn_sel_none = QPushButton("Select None")
        self.btn_sel_none.clicked.connect(self.deselect_all_review)
        self.btn_sel_toggle = QPushButton("Toggle Selection (Space)")
        self.btn_sel_toggle.clicked.connect(self.toggle_review_selection)
        self.btn_sel_toggle.setShortcut("Space")
        
        h_sel.addWidget(self.btn_sel_all)
        h_sel.addWidget(self.btn_sel_none)
        h_sel.addWidget(self.btn_sel_toggle)
        h_sel.addStretch()
        
        rev_layout.addWidget(self.review_top_widget)
        
        self.review_table = QTableView()
        self.review_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.review_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.review_table.verticalHeader().setVisible(False)
        self.review_table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.review_table.horizontalHeader().setStretchLastSection(True)
        # Custom Delegate
        self.review_table.setItemDelegate(HtmlDelegate(self.review_table))
        
        # Double click to jump
        self.review_table.doubleClicked.connect(self.on_review_table_dbl_click)
        
        rev_layout.addWidget(self.review_table)
        
        h_rev = QHBoxLayout()
        self.btn_rev_apply = QPushButton("Apply Selected")
        self.btn_rev_apply.clicked.connect(self.apply_review_selection)
        self.btn_rev_cancel = QPushButton("Cancel Review")
        self.btn_rev_cancel.clicked.connect(self.close_review)
        h_rev.addWidget(self.btn_rev_apply)
        h_rev.addWidget(self.btn_rev_cancel)
        rev_layout.addLayout(h_rev)
        
        main_layout.addWidget(self.review_group)

        # Status
        self.status_label = QLabel("")
        main_layout.addWidget(self.status_label)
        
        # Initialize Template Manager
        self.template_manager = TemplateManager()
        self.refresh_combo_templates()
        
        # Load History
        self.load_history()
        
    def load_history(self):
        """Load Find/Replace history from global config"""
        f_hist = self.mainwindow.global_config.get("find_history", [])
        r_hist = self.mainwindow.global_config.get("replace_history", [])
        
        self.cb_find.clear()
        self.cb_find.addItems(f_hist)
        self.cb_find.setCurrentIndex(-1)
        
        self.cb_replace.clear()
        self.cb_replace.addItems(r_hist)
        self.cb_replace.setCurrentIndex(-1)
        
    def save_search_history(self):
        """Save current find/replace terms to history"""
        f_txt = self.cb_find.currentText().strip()
        r_txt = self.cb_replace.currentText().strip()
        
        f_hist = self.mainwindow.global_config.get("find_history", [])
        r_hist = self.mainwindow.global_config.get("replace_history", [])
        
        # Helper to update list
        def update_list(lst, item):
            if not item: return False
            changed = False
            if item in lst:
                lst.remove(item)
                changed = True
            lst.insert(0, item)
            # Limit size
            while len(lst) > 20: 
                lst.pop()
                changed = True
            return True # Always true if item added
            
        f_changed = update_list(f_hist, f_txt)
        r_changed = update_list(r_hist, r_txt)
        
        if f_changed or r_changed:
            self.mainwindow.global_config["find_history"] = f_hist
            self.mainwindow.global_config["replace_history"] = r_hist
            self.mainwindow.config_manager.save()
            
            # Dynamic UI Refresh
            # Block signals to prevent recursion if we had currentIndexChanged connected
            self.cb_find.blockSignals(True)
            self.cb_replace.blockSignals(True)
            
            curr_f = self.cb_find.currentText()
            curr_r = self.cb_replace.currentText()
            
            self.cb_find.clear()
            self.cb_find.addItems(f_hist)
            self.cb_find.setEditText(curr_f) # Restore text
            
            self.cb_replace.clear()
            self.cb_replace.addItems(r_hist)
            self.cb_replace.setEditText(curr_r) # Restore text

            self.cb_find.blockSignals(False)
            self.cb_replace.blockSignals(False)
        
    def init_tab_standard(self):
        layout = QGridLayout(self.tab_standard)
        
        # Row 0: Find and Replace Inputs (Side by Side)
        h_inputs = QHBoxLayout()
        
        # Find
        self.lbl_find = QLabel("Find:")
        self.cb_find = QComboBox()
        self.cb_find.setEditable(True)
        self.cb_find.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        
        # Replace
        self.lbl_replace = QLabel("Replace:")
        self.cb_replace = QComboBox()
        self.cb_replace.setEditable(True)
        self.cb_replace.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        
        h_inputs.addWidget(self.lbl_find)
        h_inputs.addWidget(self.cb_find, 2) # weight 2
        h_inputs.addWidget(self.lbl_replace)
        h_inputs.addWidget(self.cb_replace, 2) 
        
        layout.addLayout(h_inputs, 0, 0, 1, 2)
        
        # Row 1: Source Chars (Hidden by default) - Translate Mode
        self.lbl_src_chars = QLabel("Source Chars:")
        self.cb_src_chars = QComboBox()
        self.cb_src_chars.setEditable(True)
        self.lbl_src_chars.setVisible(False)
        self.cb_src_chars.setVisible(False)
        
        h_src = QHBoxLayout()
        h_src.addWidget(self.lbl_src_chars)
        h_src.addWidget(self.cb_src_chars)
        layout.addLayout(h_src, 1, 0, 1, 2)

        # Row 2: Options
        opt_layout = QHBoxLayout()
        self.chk_case = QCheckBox("Case Sensitive")
        self.chk_word = QCheckBox("Whole Word")
        self.chk_regex = QCheckBox("Regex")
        self.chk_extended = QCheckBox("Extended (\\n, \\t)")
        
        opt_layout.addWidget(self.chk_case)
        opt_layout.addWidget(self.chk_word)
        opt_layout.addWidget(self.chk_regex)
        opt_layout.addWidget(self.chk_extended)
        layout.addLayout(opt_layout, 2, 0, 1, 2)
        
        # Row 3: Replace Mode
        h = QHBoxLayout()
        h.addWidget(QLabel("Replace Mode:"))
        self.combo_repl_mode = QComboBox()
        self.combo_repl_mode.addItems(["Normal", "Regex Group ($1)", "Python Lambda (match -> str)", "Translate (Char -> Char)"])
        self.combo_repl_mode.currentTextChanged.connect(self.on_mode_changed)
        h.addWidget(self.combo_repl_mode)
        layout.addLayout(h, 3, 0, 1, 2)
        
        # Row 4: Actions
        bg_act = QGroupBox("Actions")
        v_act = QVBoxLayout(bg_act)
        
        # Row 1: Find Next | Replace (Current)
        h_row1 = QHBoxLayout()
        btn_find = QPushButton("Find Next")
        btn_find.clicked.connect(self.on_find_next)
        btn_repl = QPushButton("Replace")
        btn_repl.clicked.connect(self.on_replace)
        h_row1.addWidget(btn_find)
        h_row1.addWidget(btn_repl)
        
        # Row 2: Find All (Page) | Find All (Global)
        h_row2 = QHBoxLayout()
        btn_find_all_page = QPushButton("Find All (Page)")
        btn_find_all_page.clicked.connect(self.on_find_all_page)
        btn_find_all_global = QPushButton("Find All (Global)")
        btn_find_all_global.clicked.connect(self.on_find_all_global)
        h_row2.addWidget(btn_find_all_page)
        h_row2.addWidget(btn_find_all_global)
        
        # Row 3: Count (Page) | Count (Global)
        h_row3 = QHBoxLayout()
        btn_count_page = QPushButton("Count (Page)")
        btn_count_page.clicked.connect(self.on_count_page)
        btn_count_global = QPushButton("Count (Global)")
        btn_count_global.clicked.connect(self.on_count_global)
        h_row3.addWidget(btn_count_page)
        h_row3.addWidget(btn_count_global)
        
        # Row 4: Replace All (Page) | Replace All (Global)
        h_row4 = QHBoxLayout()
        btn_repl_all = QPushButton("Replace All (Page)")
        btn_repl_all.clicked.connect(self.on_replace_all_page)
        btn_repl_global = QPushButton("Replace All (Global)")
        btn_repl_global.clicked.connect(self.on_replace_all_global)
        h_row4.addWidget(btn_repl_all)
        h_row4.addWidget(btn_repl_global)
        
        # Row 5: Review (Page) | Review (Global)
        h_row5 = QHBoxLayout()
        btn_review = QPushButton("Review (Page)")
        btn_review.clicked.connect(lambda: self.on_review(is_global=False))
        btn_review_global = QPushButton("Review (Global)")
        btn_review_global.clicked.connect(lambda: self.on_review(is_global=True))
        h_row5.addWidget(btn_review)
        h_row5.addWidget(btn_review_global)
        
        # Add to Layout
        v_act.addLayout(h_row1)
        v_act.addLayout(h_row2)
        v_act.addLayout(h_row3)
        v_act.addLayout(h_row4)
        v_act.addLayout(h_row5)
        
        layout.addWidget(bg_act, 4, 0, 1, 2)
        
        layout.setRowStretch(5, 1)
        
    def on_mode_changed(self, text):
        if "Translate" in text:
            # Change Labels
            self.tabs.setTabText(0, "Translate")
            self.lbl_find.setText("Filter (Optional):")
            self.lbl_replace.setText("Target Chars:")
            self.cb_find.setPlaceholderText("Filter Pattern (or leave empty)")
            self.cb_replace.setPlaceholderText("Target characters")

            # Show Source Chars
            self.lbl_src_chars.setVisible(True)
            self.cb_src_chars.setVisible(True)
        else:
             self.tabs.setTabText(0, "Standard / Regex")
             self.lbl_find.setText("Find:")
             self.lbl_replace.setText("Replace:")
             self.cb_find.setPlaceholderText("")
             self.cb_replace.setPlaceholderText("")
             
             self.lbl_src_chars.setVisible(False)
             self.cb_src_chars.setVisible(False)

    def on_find_next(self):
        editor = self.get_target_editor()
        
        # Use centralized regex compilation to handle Translate/Empty logic
        regex = self._compile_regex_from_ui()
        if not regex: return
        
        pattern = regex.pattern
            
        # Search
        found = False
        
        # Try finding using Python Regex manually
        text = editor.toPlainText()
        cursor = editor.textCursor()
        start_pos = cursor.position()
             
        try:
             match = regex.search(text, start_pos)
             
             if not match:
                 # Wrap around
                 match = regex.search(text, 0)
             
             if match:
                 # Select it
                 new_cursor = editor.textCursor()
                 new_cursor.setPosition(match.start())
                 new_cursor.setPosition(match.end(), QTextCursor.MoveMode.KeepAnchor)
                 editor.setTextCursor(new_cursor)
                 found = True
                 editor.centerCursor()
        except Exception as e:
             self.status_label.setText(f"Regex Error: {e}")
             return
                 
        if found:
            self.status_label.setText("Found.")
        else:
            self.status_label.setText("Not found.")

    def on_replace(self):
        editor = self.get_target_editor()
        cursor = editor.textCursor()
        if not cursor.hasSelection():
            self.on_find_next()
            return
            
        repl_text = self.cb_replace.currentText()
        if self.cb_replace.findText(repl_text) == -1: self.cb_replace.addItem(repl_text)
        
        selected_text = cursor.selectedText()
        new_text = repl_text
        
        # If Regex Mode or Python Mode, we need to re-evaluate based on the selection
        if self.chk_regex.isChecked():
            try:
                pattern = self.cb_find.currentText()
                flags = 0
                if not self.chk_case.isChecked(): flags |= re.IGNORECASE
                match = re.fullmatch(pattern, selected_text, flags)
                
                if match:
                    repl_mode = self.combo_repl_mode.currentText()
                    if "Python Lambda" in repl_mode:
                        try:
                            # Context: match
                            func = eval(f"lambda match: {repl_text}")
                            new_text = str(func(match))
                        except Exception as e:
                            self.status_label.setText(f"Python Error: {str(e)}")
                            return
                    elif "Regex Group" in repl_mode:
                        new_text = match.expand(repl_text)
            except Exception as e:
                self.status_label.setText(f"Replace Error: {e}")
                return

        # Normal/Extended Replace
        if not self.chk_regex.isChecked() and self.chk_extended.isChecked():
             new_text = new_text.replace('\\n', '\n').replace('\\t', '\t')

        cursor.insertText(new_text)
        self.status_label.setText("Replaced.")
        
        # Find next
        self.on_find_next()

    def on_replace_all_page(self):
        self.save_search_history()
        self._batch_replace([self.mainwindow.spin_page.text()], is_global=False)

    def on_replace_all_global(self):
        self.save_search_history()
        # Confirm
        ret = QMessageBox.warning(self, "Global Replace", 
            "This will replace ALL occurrences in the PROEJCT. \nEnsure you have reviewed or are confident.\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ret == QMessageBox.StandardButton.No: return
        
        pages = []
        if self.rb_left.isChecked(): pages = list(self.mainwindow.pages_left.keys())
        else: pages = list(self.mainwindow.pages_right_text.keys())
        
        self._batch_replace(pages, is_global=True)

    def _batch_replace(self, page_nums, is_global=False):
        regex = self._compile_regex_from_ui()
        if not regex: return
        
        repl_mode = self.combo_repl_mode.currentText()
        repl_text = self.cb_replace.currentText()
        
        # Prepare Repl Func
        import html
        repl_func = None

        if "Python Lambda" in repl_mode:
            try:
                user_lambda = eval(f"lambda match: {repl_text}")
                repl_func = lambda m: str(user_lambda(m))
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Lambda Error: {e}")
                return
        elif "Regex Group" in repl_mode:
            repl_func = lambda m: m.expand(repl_text)
        elif "Translate" in repl_mode:
             src = self.cb_src_chars.currentText()
             tgt = self.cb_replace.currentText()
             try:
                 table = str.maketrans(src, tgt)
                 repl_func = lambda m: m.group().translate(table)
             except Exception as e:
                  QMessageBox.critical(self, "Error", f"Translate Error: {e}")
                  return
        elif not self.chk_regex.isChecked() and self.chk_extended.isChecked():
             r_txt = repl_text.replace('\\n', '\n').replace('\\t', '\t')
             repl_func = lambda m: r_txt
        else:
             repl_func = lambda m: repl_text
             
        # Undo Snapshot
        if is_global:
            self.mainwindow.push_global_undo("Replace All Global")
        elif not is_global and len(page_nums) == 1:
             self.mainwindow.save_current_page_data()
        
        count = 0
        target_dict = self.mainwindow.pages_left if self.rb_left.isChecked() else self.mainwindow.pages_right_text
        
        for p_key in page_nums:
            try: p_num = int(p_key)
            except: continue
            
            # Load text
            if not is_global and str(p_num) == self.mainwindow.spin_page.text():
                 # Use editor text for current page
                 txt = self.get_target_editor().toPlainText()
            else:
                 txt = target_dict.get(p_num, "")
            
            # Apply Replace
            try:
                new_txt, n = regex.subn(repl_func, txt)
                if n > 0:
                    target_dict[p_num] = new_txt
                    self.mainwindow.mark_page_dirty(p_num, self.rb_left.isChecked())
                    count += n
            except Exception as e:
                print(f"Error replacing on page {p_num}: {e}")
                
        # Reload UI
        if is_global or (len(page_nums) > 0 and str(page_nums[0]) == self.mainwindow.spin_page.text()):
             self.mainwindow.force_ui_reload()

        self.status_label.setText(f"Replaced {count} occurrences.")

    def on_count_page(self):
        self._count_matches(is_global=False)

    def on_count_global(self):
        self._count_matches(is_global=True)

    def _count_matches(self, is_global):
        regex = self._compile_regex_from_ui()
        if not regex: return
        
        count = 0
        scope_str = "Current Page" if not is_global else "Global Project"
        
        if is_global:
            target_dict = self.mainwindow.pages_left if self.rb_left.isChecked() else self.mainwindow.pages_right_text
            for txt in target_dict.values():
                try: count += len(list(regex.finditer(txt)))
                except: pass
        else:
            editor = self.get_target_editor()
            text = editor.toPlainText()
            try: count = len(list(regex.finditer(text)))
            except: count = 0
            
        self.status_label.setText(f"Count ({scope_str}): {count} matches.")

    def init_tab_batch(self):
        layout = QVBoxLayout(self.tab_batch)
        
        # Template Selection
        h = QHBoxLayout()
        h.addWidget(QLabel("Template:"))
        self.combo_template = QComboBox()
        
        h.addWidget(self.combo_template, 1)
        layout.addLayout(h)
        
        # Tools
        bg_tools = QGroupBox("Template Management")
        l_tools = QHBoxLayout(bg_tools)
        
        btn_new = QPushButton("New")
        btn_new.clicked.connect(self.on_template_new)
        btn_edit = QPushButton("Edit")
        btn_edit.clicked.connect(self.on_template_edit)
        btn_del = QPushButton("Delete")
        btn_del.clicked.connect(self.on_template_delete)
        
        l_tools.addWidget(btn_new)
        l_tools.addWidget(btn_edit)
        l_tools.addWidget(btn_del)
        layout.addWidget(bg_tools)
        
        # Actions
        bg_run = QGroupBox("Execution")
        l_run = QVBoxLayout(bg_run)
        
        btn_run_page = QPushButton("Run Batch (Current Page)")
        btn_run_page.clicked.connect(self.on_batch_run_page)
        btn_run_global = QPushButton("Run Batch (All Pages)")
        btn_run_global.clicked.connect(self.on_batch_run_global)
        
        l_run.addWidget(btn_run_page)
        l_run.addWidget(btn_run_global)
        layout.addWidget(bg_run)
        
        layout.addStretch()

    def init_tab_diff(self):
        """Diff Filter UI"""
        layout = QVBoxLayout(self.tab_diff)
        
        # 1. Opcode Selection
        grp_op = QGroupBox("Select Changes to Apply (Opcodes)")
        l_op = QHBoxLayout(grp_op)
        self.chk_diff_insert = QCheckBox("Insert (Added text)")
        self.chk_diff_delete = QCheckBox("Delete (Removed text)")
        self.chk_diff_replace = QCheckBox("Replace (Modified text)")
        self.chk_diff_insert.setChecked(True)
        self.chk_diff_delete.setChecked(True)
        self.chk_diff_replace.setChecked(True)
        
        l_op.addWidget(self.chk_diff_insert)
        l_op.addWidget(self.chk_diff_delete)
        l_op.addWidget(self.chk_diff_replace)
        layout.addWidget(grp_op)
        
        # 2. Content Filters
        grp_filter = QGroupBox("Content Filter (Regex)")
        form = QFormLayout(grp_filter)
        
        # Original Text Filter (For Delete & Replace)
        self.txt_diff_filter_old = QLineEdit()
        self.txt_diff_filter_old.setPlaceholderText("Regex to match Original Text (Empty = All)")
        form.addRow("Original Match:", self.txt_diff_filter_old)
        
        # New Text Filter (For Insert & Replace)
        self.txt_diff_filter_new = QLineEdit()
        self.txt_diff_filter_new.setPlaceholderText("Regex to match New Text (Empty = All)")
        form.addRow("New Text Match:", self.txt_diff_filter_new)
        
        layout.addWidget(grp_filter)
        
        # 3. Actions
        h_btn = QHBoxLayout()
        btn_diff_page = QPushButton("Review Diff (Current Page)")
        btn_diff_global = QPushButton("Review Diff (Global)")
        
        btn_diff_page.clicked.connect(lambda: self.on_review_diff(False))
        btn_diff_global.clicked.connect(lambda: self.on_review_diff(True))
        
        h_btn.addWidget(btn_diff_page)
        h_btn.addWidget(btn_diff_global)
        layout.addLayout(h_btn)
        
        layout.addStretch()

    def refresh_combo_templates(self):
        curr = self.combo_template.currentText()
        self.combo_template.clear()
        names = self.template_manager.get_template_names()
        self.combo_template.addItems(names)
        if curr in names:
            self.combo_template.setCurrentText(curr)
        elif names:
             self.combo_template.setCurrentIndex(0)

    def on_template_new(self):
        dlg = TemplateEditorDialog(self, self.template_manager, None)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.refresh_combo_templates()
            
    def on_template_edit(self):
        curr = self.combo_template.currentText()
        if not curr: return
        dlg = TemplateEditorDialog(self, self.template_manager, curr)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.refresh_combo_templates()
            self.combo_template.setCurrentText(curr) 
            
    def on_template_delete(self):
        curr = self.combo_template.currentText()
        if not curr: return
        
        ret = QMessageBox.warning(self, "Delete Template", 
            f"Are you sure you want to delete template '{curr}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            
        if ret == QMessageBox.StandardButton.Yes:
            self.template_manager.delete_template(curr)
            self.refresh_combo_templates()
            
    def on_batch_run_page(self):
        self._run_batch_logic(is_global=False)
        
    def on_batch_run_global(self):
         # Confirm
        ret = QMessageBox.warning(self, "Global Batch", 
            "This will run multiple replacement rules on ALL pages. Ensure your template is correct.\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ret == QMessageBox.StandardButton.No: return
        
        self._run_batch_logic(is_global=True)
        
    def _run_batch_logic(self, is_global=False):
        name = self.combo_template.currentText()
        rules = self.template_manager.get_rules(name)
        if not rules:
             QMessageBox.warning(self, "Error", "No rules in template.")
             return
             
        # Undo Snapshot
        if is_global:
            self.mainwindow.push_global_undo(f"Batch: {name}")
            
        target_dict = None
        pages = []
        
        if is_global:
            if self.rb_left.isChecked(): target_dict = self.mainwindow.pages_left
            else: target_dict = self.mainwindow.pages_right_text
            pages = list(target_dict.keys())
        
        compiled_rules = []
        for r in rules:
            try:
                item = {}
                r_mode = r.get('replace_mode', 'Normal')
                pat = r['find']
                r_text = r['replace']
                
                if "Translate" in r_mode:
                    src = r.get('source', '')
                    if not src and not r['find']: continue
                    
                    target_src = src if src else r['find']
                    
                    try:
                        filter_pat = r['find']
                        if filter_pat:
                             item['regex'] = re.compile(filter_pat)
                        else:
                             item['regex'] = re.compile(f"[{re.escape(target_src)}]")
                             
                        table = str.maketrans(target_src, r_text)
                        
                        item['func'] = lambda m: m.group().translate(table)
                    except: continue
                else:
                    flags = 0
                    if not r['case_sensitive']: flags |= re.IGNORECASE
                    
                    if r['search_mode'] == 'Regex':
                        item['regex'] = re.compile(pat, flags)
                    else:
                        s_pat = pat
                        if r['search_mode'] == 'Extended':
                            s_pat = s_pat.replace('\\n', '\n').replace('\\t', '\t').replace('\\r', '\r')
                        
                        if not r['case_sensitive']:
                             item['regex'] = re.compile(re.escape(s_pat), flags)
                        else:
                             item['regex'] = re.compile(re.escape(s_pat))
                    
                    if "Python Lambda" in r_mode:
                         user_l = eval(f"lambda match: {r_text}")
                         item['func'] = lambda m, ul=user_l: str(ul(m))
                    elif "Regex Group" in r_mode:
                         item['func'] = lambda m, rt=r_text: m.expand(rt)
                    else:
                         final_r = r_text
                         item['func'] = lambda m, rt=final_r: rt
                     
                compiled_rules.append(item)
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Error compiling rule '{r['find']}': {e}")
                return

        count_total = 0
        
        if not is_global:
            editor = self.get_target_editor()
            text = editor.toPlainText()
            cursor = editor.textCursor()
            cursor.beginEditBlock()
            
            current_text = text
            for cr in compiled_rules:
                current_text, n = cr['regex'].subn(cr['func'], current_text)
                count_total += n
                
            if current_text != text:
                cursor.select(QTextCursor.SelectionType.Document)
                cursor.insertText(current_text)
            
            cursor.endEditBlock()
            
        else:
            for p in pages:
                if p not in target_dict: continue
                txt = target_dict[p]
                original = txt
                for cr in compiled_rules:
                    txt, n = cr['regex'].subn(cr['func'], txt)
                    count_total += n
                
                if txt != original:
                    target_dict[p] = txt
                    self.mainwindow.mark_page_dirty(p, self.rb_left.isChecked())
            
            self.mainwindow.reload_displayed_texts()
            
        self.status_label.setText(f"Batch completed. {count_total} replacements.")

    def on_find_all_page(self):
        self.on_review(is_global=False, find_only=True)
        
    def on_find_all_global(self):
        self.on_review(is_global=True, find_only=True)

    def select_all_review(self):
        if not self.review_table.model(): return
        for i in range(self.review_table.model().rowCount()):
            self.review_table.model()._data[i]['checked'] = True
        self.review_table.model().layoutChanged.emit()
        
    def deselect_all_review(self):
        if not self.review_table.model(): return
        for i in range(self.review_table.model().rowCount()):
            self.review_table.model()._data[i]['checked'] = False
        self.review_table.model().layoutChanged.emit()

    def toggle_review_selection(self):
        rows = sorted(set(index.row() for index in self.review_table.selectedIndexes()))
        if not rows or not self.review_table.model(): return
        m = self.review_table.model()
        for r in rows:
            m._data[r]['checked'] = not m._data[r]['checked']
        m.layoutChanged.emit()

    def on_review(self, is_global=False, find_only=False):
        self.current_review_items = []
        self.review_is_global = is_global
        
        regex = self._compile_regex_from_ui()
        if not regex: return
        
        pages = []
        if is_global:
            if self.rb_left.isChecked(): pages = list(self.mainwindow.pages_left.keys())
            else: pages = list(self.mainwindow.pages_right_text.keys())
        else:
            pages = [self.mainwindow.spin_page.text()]
            
        self.status_label.setText("Generating review...")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        
        try:
            target_dict = self.mainwindow.pages_left if self.rb_left.isChecked() else self.mainwindow.pages_right_text
            
            for p_key in pages:
                try: p_num = int(p_key)
                except: continue
                
                text = ""
                if not is_global and str(p_num) == self.mainwindow.spin_page.text():
                     text = self.get_target_editor().toPlainText()
                else:
                     text = target_dict.get(p_num, "")
                
                self._generate_review_items_for_page(p_num, text, regex, is_find_only=find_only)
                
            self.model = ReviewTableModel(self.current_review_items)
            self.review_table.setModel(self.model)
            self.review_table.resizeColumnsToContents()
            self.review_table.resizeRowsToContents()
            self.review_table.horizontalHeader().setStretchLastSection(True)
            self.review_table.setColumnWidth(0, 30)
            self.review_table.setColumnWidth(4, 400) 
                
        finally:
            QApplication.restoreOverrideCursor()
            
        if not self.current_review_items:
            self.status_label.setText("No matches found.")
            return

        self.tabs.setVisible(False)
        self.scope_group.setVisible(False)
        self.review_group.setVisible(True)
        self.status_label.setText(f"Found {len(self.current_review_items)} {'matches' if find_only else 'replacements'}.")
        
        if find_only:
            self.review_table.setColumnHidden(0, True) 
            self.review_top_widget.setVisible(False) 
            self.btn_rev_apply.setVisible(False)       
            self.btn_rev_cancel.setText("Back")        
        else:
            self.review_table.setColumnHidden(0, False)
            self.review_top_widget.setVisible(True)
            self.btn_rev_apply.setVisible(True)
            self.btn_rev_cancel.setText("Cancel Review")

    def _generate_review_items_for_page(self, page_num, text, regex, is_find_only=False):
        import html
        
        repl_func = None
        if not is_find_only:
            repl_mode = self.combo_repl_mode.currentText()
            repl_text = self.cb_replace.currentText()

            if "Python Lambda" in repl_mode:
                try:
                    user_lambda = eval(f"lambda match: {repl_text}")
                    repl_func = lambda m: str(user_lambda(m))
                except: return
            elif "Regex Group" in repl_mode:
                repl_func = lambda m: m.expand(repl_text)
            elif "Translate" in repl_mode:
                 src = self.cb_src_chars.currentText()
                 tgt = self.cb_replace.currentText()
                 try:
                     table = str.maketrans(src, tgt)
                     repl_func = lambda m: m.group().translate(table)
                 except: return
            elif not self.chk_regex.isChecked() and self.chk_extended.isChecked():
                 r_txt = repl_text.replace('\\n', '\n').replace('\\t', '\t')
                 repl_func = lambda m: r_txt
            else:
                 repl_func = lambda m: repl_text
        
        matches = list(regex.finditer(text))
        
        for m in matches:
            original = m.group()
            start, end = m.span()
            
            line = text.count('\n', 0, start) + 1
            last_nl = text.rfind('\n', 0, start)
            if last_nl == -1: col = start + 1
            else: col = start - last_nl
            
            new_t = original
            item_checked = True
            
            if not is_find_only and repl_func:
                try:
                    new_t = repl_func(m)
                except: new_t = "ERROR"
                
                if original == new_t: continue
            else:
                item_checked = False
            
            c_start = max(0, start - 20)
            c_end = min(len(text), end + 20)
            
            prefix = html.escape(text[c_start:start])
            suffix = html.escape(text[end:c_end])
            orig_esc = html.escape(original)
            
            diff_html = ""
            if is_find_only:
                diff_html = f"{prefix}<span style='background-color:#ffff00; color:black;'><b>{orig_esc}</b></span>{suffix}"
            else:
                new_esc = html.escape(new_t)
                diff_html = f"{prefix}<span style='background-color:#ffcccc; text-decoration:line-through;'>{orig_esc}</span> <span style='background-color:#ccffcc;'><b>{new_esc}</b></span>{suffix}"
            
            item_data = {
                'page_num': page_num,
                'line': line,
                'col': col,
                'span': (start, end), 
                'original': original,
                'new': new_t,
                'context_html': diff_html,
                'checked': item_checked
            }
            self.current_review_items.append(item_data)
        
    def close_review(self):
        self.review_group.setVisible(False)
        self.tabs.setVisible(True)
        self.scope_group.setVisible(True)
        self.status_label.setText("Review cancelled.")
        self.review_table.setModel(None) 
        
    def apply_review_selection(self):
        to_apply = [x for x in self.current_review_items if x['checked']]
        if not to_apply: return
        
        to_apply.sort(key=lambda x: (int(x['page_num']), x['span'][0]), reverse=True)
        
        self.mainwindow.save_current_page_data()
        
        if self.review_is_global:
            self.mainwindow.push_global_undo("Review Apply")
            
        from collections import defaultdict
        by_page = defaultdict(list)
        for item in to_apply:
            by_page[item['page_num']].append(item)
            
        count = 0
        target_dict = self.mainwindow.pages_left if self.rb_left.isChecked() else self.mainwindow.pages_right_text
            
        for p_num, items in by_page.items():
            txt = target_dict.get(p_num, "")
            items.sort(key=lambda x: x['span'][0], reverse=True)
            
            new_txt = txt
            for item in items:
                start, end = item['span']
                repl_t = item['new']
                if new_txt[start:end] == item['original']:
                     new_txt = new_txt[:start] + repl_t + new_txt[end:]
                     count += 1
            
            target_dict[p_num] = new_txt
            self.mainwindow.mark_page_dirty(p_num, self.rb_left.isChecked())
            
        if self.review_is_global:
            if hasattr(self.mainwindow, 'force_ui_reload'): self.mainwindow.force_ui_reload()
        else:
            if hasattr(self.mainwindow, 'force_ui_reload'): self.mainwindow.force_ui_reload()
            
        self.close_review()
        self.status_label.setText(f"Applied {count} changes.")
        
    def _compile_regex_from_ui(self):
        pattern = self.cb_find.currentText()
        repl_mode = self.combo_repl_mode.currentText()
        
        if "Translate" in repl_mode:
             if not pattern:
                 src = self.cb_src_chars.currentText()
                 if not src: return None
                 try:
                     return re.compile(f"[{re.escape(src)}]")
                 except: return None
        
        if not pattern: return None
        try:
            flags = 0
            if not self.chk_case.isChecked(): flags |= re.IGNORECASE
            if self.chk_regex.isChecked():
                return re.compile(pattern, flags)
            else:
                s_pat = pattern
                if self.chk_extended.isChecked():
                     s_pat = s_pat.replace('\\n', '\n').replace('\\t', '\t').replace('\\r', '\r')
                return re.compile(re.escape(s_pat), flags)
        except: return None

    def on_review_diff(self, is_global):
        self.review_group.setVisible(False)
        self.current_review_items = []
        self.review_is_global = is_global
        
        regex_old = None
        regex_new = None
        pat_old = self.txt_diff_filter_old.text()
        pat_new = self.txt_diff_filter_new.text()
        
        try:
             if pat_old: regex_old = re.compile(pat_old)
             if pat_new: regex_new = re.compile(pat_new)
        except Exception as e:
             QMessageBox.critical(self, "Regex Error", str(e))
             return

        target_is_left = self.rb_left.isChecked()
        
        pages = []
        if not is_global:
            try:
                p = int(self.mainwindow.spin_page.text())
                pages = [p]
            except: pages = []
        else:
             keys_l = set(self.mainwindow.pages_left.keys())
             keys_r = set(self.mainwindow.pages_right_text.keys())
             all_pages = sorted(list(keys_l | keys_r))
             pages = all_pages
             
        self.progress_dialog = QProgressDialog("Analyzing differences...", "Cancel", 0, len(pages), self)
        self.progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress_dialog.setMinimumDuration(200)
        self.progress_dialog.setValue(0)
        
        self.diff_worker_thread = ReviewDiffWorker(
            pages, 
            self.mainwindow.pages_left, 
            self.mainwindow.pages_right_text,
            target_is_left,
            regex_old, regex_new,
            self.chk_diff_insert.isChecked(),
            self.chk_diff_delete.isChecked(),
            self.chk_diff_replace.isChecked()
        )
        
        self.diff_worker_thread.progress.connect(self.progress_dialog.setValue)
        self.diff_worker_thread.finished.connect(self.on_diff_worker_finished)
        self.progress_dialog.canceled.connect(self.diff_worker_thread.requestInterruption)
        
        self.diff_worker_thread.start()

    def on_diff_worker_finished(self, items, msg):
        self.diff_worker_thread = None
        self.progress_dialog.close()
        
        if msg != "Done" and msg != "Cancelled": 
             if msg: QMessageBox.warning(self, "Diff Info", msg)
        
        self.current_review_items = items
        
        if not items:
            self.status_label.setText("No diff items found.")
            return

        self.model = ReviewTableModel(self.current_review_items)
        self.review_table.setModel(self.model)
        
        self.tabs.setVisible(False)
        self.scope_group.setVisible(False)
        self.review_group.setVisible(True)
        self.review_table.resizeRowsToContents()
        self.status_label.setText(f"Found {len(self.current_review_items)} diff items.")

    def on_review_table_dbl_click(self, index):
        if not index.isValid(): return
        
        row = index.row()
        if 0 <= row < len(self.current_review_items):
            item = self.current_review_items[row]
            page_num = item.get('page_num')
            span = item.get('span')
            
            if page_num:
                if str(page_num) != self.mainwindow.spin_page.text():
                    self.mainwindow.spin_page.setText(str(page_num))
                    self.mainwindow.jump_page()
                    QApplication.processEvents()
            
            if span:
                editor = self.get_target_editor()
                if not editor: return
                
                cursor = editor.textCursor()
                start, end = span
                
                try:
                    cursor.setPosition(start)
                    cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
                    editor.setTextCursor(cursor)
                    editor.ensureCursorVisible()
                    editor.setFocus()
                except: pass 

    def reset_review_ui(self):
        self.review_group.setVisible(False)
        self.tabs.setVisible(True)
        self.scope_group.setVisible(True)
        self.current_review_items = []
        self.review_table.setModel(None)
        self.status_label.setText("Review reset due to project switch.")

    def get_target_editor(self):
        if self.rb_left.isChecked():
            return self.mainwindow.edit_left
        return self.mainwindow.edit_right

    def get_search_flags(self):
        flags = QTextDocument.FindFlag(0)
        if self.chk_case.isChecked():
            flags |= QTextDocument.FindFlag.FindCaseSensitively
        if self.chk_word.isChecked():
            flags |= QTextDocument.FindFlag.FindWholeWords
        return flags
