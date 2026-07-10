import os
import re
import json
import html
import difflib

from PyQt6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QPushButton, QLineEdit, QTextEdit, QPlainTextEdit,
    QCheckBox, QComboBox, QListWidget, QListWidgetItem,
    QTableView, QHeaderView, QAbstractItemView,
    QGroupBox, QRadioButton, QTabWidget, QGridLayout,
    QMessageBox, QProgressDialog, QWidget, QStyle,
    QStyledItemDelegate, QSizePolicy, QSplitter
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import (
    QColor, QFont, QTextDocument, QAbstractTextDocumentLayout,
    QTextCursor, QKeySequence, QShortcut
)

from .models import ReviewTableModel, HtmlDelegate, to_qt_pos
from .workers import ReviewDiffWorker
from .templates import TemplateManager, TemplateEditorDialog

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


class FindReplaceDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Find and Replace")
        self.resize(500, 450)
        self.setModal(False)
        self.mainwindow = parent
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
        # 查找和替换内容中的首尾空白可能具有实际意义，必须原样保存。
        f_txt = self.cb_find.currentText()
        r_txt = self.cb_replace.currentText()
        
        f_hist = list(self.mainwindow.global_config.get("find_history", []))
        r_hist = list(self.mainwindow.global_config.get("replace_history", []))
        
        # Helper to update list
        def update_list(lst, item):
            if item == "": return False
            if item in lst:
                lst.remove(item)
            lst.insert(0, item)
            # Limit size
            while len(lst) > 20: 
                lst.pop()
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
        self.save_search_history()
        regex = self._compile_regex_from_ui()
        if not regex: return
        
        editor = self.get_target_editor()
        cursor = editor.textCursor()
        
        # Search from current position
        text = editor.toPlainText()
        pos = cursor.position()
        
        match = regex.search(text, pos)
        
        if not match:
            # Wrap around?
            match = regex.search(text, 0)
            
        if match:
            start, end = match.span()
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            editor.setTextCursor(cursor)
            editor.ensureCursorVisible()
            self.status_label.setText(f"Found match at {start}-{end}")
        else:
            self.status_label.setText("No match found.")

    def on_replace(self):
        self.save_search_history()
        # Get current selection
        editor = self.get_target_editor()
        cursor = editor.textCursor()
        
        if not cursor.hasSelection():
            self.on_find_next()
            return

        # Check if selection matches regex
        text = editor.toPlainText()
        sel_start = cursor.selectionStart()
        sel_end = cursor.selectionEnd()
        sel_text = cursor.selectedText()
        
        regex = self._compile_regex_from_ui()
        if not regex: return
        
        # Verify if current selection is a match
        # match = regex.fullmatch(sel_text) # Might fail if regex expects context
        # Better: search at position
        match = regex.search(text, sel_start)
        
        if match and match.start() == sel_start and match.end() == sel_end:
            # It is a match, replace it
            repl_text = self.cb_replace.currentText()
            new_text = ""
            
            # Resolve replacement
            repl_mode = self.combo_repl_mode.currentText()
            try:
                if "Python Lambda" in repl_mode:
                    user_lambda = eval(f"lambda match: {repl_text}")
                    new_text = str(user_lambda(match))
                elif "Regex Group" in repl_mode:
                    new_text = match.expand(repl_text)
                elif "Translate" in repl_mode:
                     src = self.cb_src_chars.currentText()
                     tgt = self.cb_replace.currentText()
                     table = str.maketrans(src, tgt)
                     new_text = match.group().translate(table)
                elif not self.chk_regex.isChecked() and self.chk_extended.isChecked():
                     new_text = repl_text.replace('\\n', '\n').replace('\\t', '\t')
                else:
                     new_text = repl_text
            except Exception as e:
                QMessageBox.critical(self, "Replace Error", str(e))
                return

            cursor.insertText(new_text)
            self.status_label.setText("Replaced.")
            
            # Find next?
            self.on_find_next()
        else:
            # Selection is not a match, just find next
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
             
        # Ensure current page in active editor is saved before bulk replace
        self.mainwindow.save_current_page_data()

        # Undo Snapshot
        if is_global:
            self.mainwindow.push_global_undo("Replace All Global")
        elif not is_global and len(page_nums) == 1:
             # Page level undo handled by editor usually? 
             # But we are modifying data dict directly or editor?
             pass # Save already called above
        
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
        self.save_search_history()
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
        
        # Scope Text Filter
        self.txt_diff_scope_regex = QLineEdit()
        self.txt_diff_scope_regex.setPlaceholderText(r"Regex scope (e.g. \*\*.*?\*\*)")
        form.addRow("Scope Match:", self.txt_diff_scope_regex)

        scope_mode_widget = QWidget()
        scope_mode_layout = QHBoxLayout(scope_mode_widget)
        scope_mode_layout.setContentsMargins(0, 0, 0, 0)
        self.rb_diff_scope_include = QRadioButton("仅在正则匹配范围内")
        self.rb_diff_scope_exclude = QRadioButton("仅在正则匹配范围外")
        self.rb_diff_scope_include.setChecked(True)
        scope_mode_layout.addWidget(self.rb_diff_scope_include)
        scope_mode_layout.addWidget(self.rb_diff_scope_exclude)
        scope_mode_layout.addStretch()
        scope_mode_widget.setToolTip("选择差异必须位于正则匹配范围内，或排除匹配范围内的差异")
        form.addRow("Scope Mode:", scope_mode_widget)
        
        layout.addWidget(grp_filter)
        
        # 2.5 Custom Replace Format
        grp_fmt = QGroupBox("Custom Replace Format")
        l_fmt = QFormLayout(grp_fmt)
        self.chk_custom_replace = QCheckBox("Enable Custom Format")
        self.txt_custom_replace = QLineEdit()
        self.txt_custom_replace.setPlaceholderText(r"e.g. \1[\2] or \1.1\2.2 (\\ = backslash)")
        l_fmt.addRow(self.chk_custom_replace)
        l_fmt.addRow("Format:", self.txt_custom_replace)
        layout.addWidget(grp_fmt)
        
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
            # Select new
            # If name is known? TemplateEditorDialog saves it.
            # We can select it by name if we knew it, but refresh handles it.
            
    def on_template_edit(self):
        curr = self.combo_template.currentText()
        if not curr: return
        dlg = TemplateEditorDialog(self, self.template_manager, curr)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.refresh_combo_templates()
            self.combo_template.setCurrentText(curr) # Try maintain selection
            
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
             
        target_dict = None
        pages = []
        
        if is_global:
            if self.rb_left.isChecked(): target_dict = self.mainwindow.pages_left
            else: target_dict = self.mainwindow.pages_right_text
            pages = list(target_dict.keys())
        else:
             # Current Page only
             # Strategy: Modify current editor text directly?
             # Or modify dict then reload? 
             # If Modify Editor: we see updates live.
             # But we have multiple rules.
             pass
             
        editor = self.get_target_editor()
        
        # Pre-compile rules
        compiled_rules = []
        for r in rules:
            try:
                item = {}
                r_mode = r.get('replace_mode', 'Normal')
                pat = r['find']
                r_text = r['replace']
                
                if "Translate" in r_mode:
                    # Translate Mode: Source=Source Chars, Replace=Target, Find=Filter
                    src = r.get('source', '')
                    # If source missing (old rules), maybe fallback to 'find'?
                    # But 'find' is now Filter.
                    # Let's assume user updated rule. If src empty, maybe use find if it looks like char list?
                    # No, strict mapping: Source->Src, Find->Filter.
                    
                    if not src and not r['find']: continue
                    
                    target_src = src if src else r['find'] # Fallback for migration/lazy usage if filter empty
                    
                    try:
                        # Regex targets: Filter pattern OR Source Chars if filter empty
                        filter_pat = r['find']
                        
                        if filter_pat:
                             # Use user provided filter regex
                             # Note: We need to translate matches of this filter.
                             item['regex'] = re.compile(filter_pat)
                        else:
                             # No filter, find matches of source chars
                             item['regex'] = re.compile(f"[{re.escape(target_src)}]")
                             
                        table = str.maketrans(target_src, r_text)
                        
                        # Apply func:
                        # If we matched Filter, we translate the whole match using table?
                        # Or partial? Usually whole match translate.
                        # If we matched Source Chars directly, simple translate.
                        item['func'] = lambda m: m.group().translate(table)
                    except: continue
                else:
                    # Standard / Regex
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
                         # Normal
                         final_r = r_text
                         item['func'] = lambda m, rt=final_r: rt
                     
                compiled_rules.append(item)
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Error compiling rule '{r['find']}': {e}")
                return

        count_total = 0
        current_page = None
        if is_global:
            self.mainwindow.save_current_page_data()
            try:
                current_page = int(self.mainwindow.spin_page.text())
                if current_page not in pages:
                    pages.append(current_page)
            except (TypeError, ValueError):
                pass
            self.mainwindow.push_global_undo(f"Batch: {name}")
        
        if not is_global:
            # Local Page: Apply sequentially to editor text
            text = editor.toPlainText()
            
            # For each rule, apply to text
            # Note: sequential application meant rule 2 sees result of rule 1.
            
            cursor = editor.textCursor()
            cursor.beginEditBlock()
            
            # Optimize: apply to python string, then set ONCE?
            # Or apply step by step to keep granular Undo? 
            # If we setOnce, we lose granular undo of steps, but we have 1 Undo for "Batch Run". This is better.
            
            current_text = text
            for cr in compiled_rules:
                current_text, n = cr['regex'].subn(cr['func'], current_text)
                count_total += n
                
            if current_text != text:
                cursor.select(QTextCursor.SelectionType.Document)
                cursor.insertText(current_text)
            
            cursor.endEditBlock()
            
        else:
            editor_handles_current = (
                current_page is not None
                and (self.rb_left.isChecked() or self.mainwindow.is_text_source_selected())
            )
            for p in pages:
                if editor_handles_current and p == current_page:
                    continue
                if p not in target_dict: continue
                txt = target_dict[p]
                
                original = txt
                for cr in compiled_rules:
                    txt, n = cr['regex'].subn(cr['func'], txt)
                    count_total += n
                
                if txt != original:
                    target_dict[p] = txt
                    self.mainwindow.mark_page_dirty(p, self.rb_left.isChecked())

            if editor_handles_current:
                original = editor.toPlainText()
                current_text = original
                for cr in compiled_rules:
                    current_text, n = cr['regex'].subn(cr['func'], current_text)
                    count_total += n
                if current_text != original:
                    cursor = editor.textCursor()
                    cursor.beginEditBlock()
                    cursor.select(QTextCursor.SelectionType.Document)
                    cursor.insertText(current_text)
                    cursor.endEditBlock()
                    target_dict[current_page] = editor.toPlainText()
                    self.mainwindow.mark_page_dirty(current_page, self.rb_left.isChecked())

            self.mainwindow.finalize_global_action(changed=count_total > 0)
            
        self.status_label.setText(f"Batch completed. {count_total} replacements.")

    def on_replace_all_page(self):
        self.save_search_history()
        self._batch_replace([self.mainwindow.spin_page.text()], is_global=False)

    def on_replace_all_global(self):
        self.save_search_history()
        # Confirm
        ret = QMessageBox.warning(self, "Global Replace", 
            "This will replace text in ALL pages. You can undo this action later, but manual edits might be lost if you undo.\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ret == QMessageBox.StandardButton.No: return
        
        # Save current page first!
        self.mainwindow.save_current_page_data()
        
        # Get all page keys
        if self.rb_left.isChecked():
            pages = list(self.mainwindow.pages_left.keys())
        else:
            pages = list(self.mainwindow.pages_right_text.keys())
        
        self._batch_replace(pages, is_global=True)

    def _batch_replace(self, page_nums, is_global=False):
        """Generic batch replace logic"""
        # 1. Prepare Logic
        regex = self._compile_regex_from_ui()
        if not regex:
             # Check if it was because of empty pattern/src?
             # For standard modes, empty pattern is invalid.
             # For translate, empty src is invalid.
             return

        target_is_left = self.rb_left.isChecked()
        repl_text = self.cb_replace.currentText()
        repl_mode = self.combo_repl_mode.currentText()
        
        page_nums = list(page_nums)

        # Snapshot for Undo (if global). Save the active editor first so the
        # snapshot and page list include the actual current-page contents.
        if is_global:
            self.mainwindow.save_current_page_data()
            try:
                current_page = int(self.mainwindow.spin_page.text())
                if current_page not in page_nums:
                    page_nums.append(current_page)
            except (TypeError, ValueError):
                current_page = None
            self.mainwindow.push_global_undo("Replace All")
        else:
            current_page = None

        repl_func = None
        # Determine replacement string/func
        repl_mode = self.combo_repl_mode.currentText()
        
        if "Python Lambda" in repl_mode:
            try:
                # Security risk accepted by user
                user_lambda = eval(f"lambda match: {repl_text}")
                repl_func = lambda m: str(user_lambda(m))
            except Exception as e:
                 QMessageBox.critical(self, "Error", f"Invalid Python Code: {e}")
                 return
        elif "Regex Group" in repl_mode:
            repl_func = lambda m: m.expand(repl_text)
        elif "Translate" in repl_mode:
             src = self.cb_src_chars.currentText()
             tgt = self.cb_replace.currentText()
             try:
                 table = str.maketrans(src, tgt)
                 repl_func = lambda m: m.group().translate(table)
             except: return
        else:
            # Normal / Extended
            r_txt = repl_text
            if not self.chk_regex.isChecked() and self.chk_extended.isChecked():
                 r_txt = r_txt.replace('\\n', '\n').replace('\\t', '\t')
            repl_func = lambda m: r_txt

        # Statistics
        count = 0
        
        # Execution
        # If current page == editor, use editor API for Undo support (if not global or consistent)
        # Actually mixed approach: Editor for active, Dict for others?
        # To keep it simple: Update Dict, then Reload Editor. 
        # BUT losing editor undo stack for current page is annoying.
        # Strategy: 
        #   If is_global: Update Dicts. Reload. (Undo via Global Undo).
        #   If single page (current): Use Editor API (Undo via Ctrl+Z).
        
        editor = self.get_target_editor()
        
        if not is_global:
            # Local Page - Editor API
            text = editor.toPlainText()
            matches = list(regex.finditer(text))
            if not matches:
                self.status_label.setText("No matches found.")
                return
            
            cursor = editor.textCursor()
            cursor.beginEditBlock()
            # Reverse apply
            for m in reversed(matches):
                start, end = m.span()
                try:
                    new_s = repl_func(m)
                    cursor.setPosition(start)
                    cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
                    cursor.insertText(new_s)
                    count += 1
                except: pass
            cursor.endEditBlock()
            
        else:
            # Global: keep the current page in the QTextDocument so Ctrl+Z's
            # existing undo stack survives; update other pages through memory.
            target_dict = self.mainwindow.pages_left if target_is_left else self.mainwindow.pages_right_text
            editor_handles_current = (
                current_page is not None
                and (target_is_left or self.mainwindow.is_text_source_selected())
            )

            for p in page_nums:
                p_key = int(p) if isinstance(p, str) and p.isdigit() else p
                if editor_handles_current and p_key == current_page:
                    continue
                if p_key not in target_dict: continue
                
                txt = target_dict[p_key]
                # re.sub with function
                new_txt, n = regex.subn(repl_func, txt)
                
                if n > 0:
                    target_dict[p_key] = new_txt
                    self.mainwindow.mark_page_dirty(p_key, target_is_left)
                    count += n

            if editor_handles_current:
                text = editor.toPlainText()
                matches = list(regex.finditer(text))
                if matches:
                    cursor = editor.textCursor()
                    cursor.beginEditBlock()
                    for match in reversed(matches):
                        try:
                            replacement = repl_func(match)
                            start, end = match.span()
                            cursor.setPosition(start)
                            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
                            cursor.insertText(replacement)
                            count += 1
                        except Exception:
                            pass
                    cursor.endEditBlock()
                    target_dict[current_page] = editor.toPlainText()
                    self.mainwindow.mark_page_dirty(current_page, target_is_left)

            self.mainwindow.finalize_global_action(changed=count > 0)

        self.status_label.setText(f"Replaced {count} occurrences.")


    def on_count(self):
        regex = self._compile_regex_from_ui()
        if not regex: return
        
        # Current Page Count
        editor = self.get_target_editor()
        text_page = editor.toPlainText()
        try:
            count_page = len(list(regex.finditer(text_page)))
        except: count_page = 0
            
        # Global Count
        count_global = 0
        target_dict = self.mainwindow.pages_left if self.rb_left.isChecked() else self.mainwindow.pages_right_text
        for txt in target_dict.values():
            try:
                count_global += len(list(regex.finditer(txt)))
            except: pass
            
        self.status_label.setText(f"Matches: {count_page} (Current Page) / {count_global} (Global Project)")

    def on_find_all_page(self):
        self.on_review(is_global=False, find_only=True)
        
    def on_find_all_global(self):
        self.on_review(is_global=True, find_only=True)

    def select_all_review(self):
        if not self.review_table.model(): return
        for i in range(self.review_table.model().rowCount()):
            self.review_table.model()._data[i]['checked'] = True
        self.review_table.model().layoutChanged.emit() # Refresh all
        
    def deselect_all_review(self):
        if not self.review_table.model(): return
        for i in range(self.review_table.model().rowCount()):
            self.review_table.model()._data[i]['checked'] = False
        self.review_table.model().layoutChanged.emit()

    def toggle_review_selection(self):
        rows = sorted(set(index.row() for index in self.review_table.selectedIndexes()))
        if not rows or not self.review_table.model(): return
        
        # Toggle based on first selected item? Or flip each?
        # User said "Select items then check/uncheck".
        # If mixed, maybe set all to Checked? Or Unchecked?
        # Standard: Flip each.
        
        m = self.review_table.model()
        for r in rows:
            m._data[r]['checked'] = not m._data[r]['checked']
            
        # Optimize emit?
        # model.dataChanged for specific rows?
        # LayoutChanged is easiest for now.
        m.layoutChanged.emit()

    def on_review(self, is_global=False, find_only=False):
        # 1. Clear previous
        self.save_search_history()
        self.current_review_items = []
        self.review_is_global = is_global
        self.review_target_is_left = self.rb_left.isChecked()
        if is_global:
            self.mainwindow.save_current_page_data()
        
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
                if not is_global:
                     text = self.get_target_editor().toPlainText()
                else:
                     text = target_dict.get(p_num, "")
                
                self._generate_review_items_for_page(p_num, text, regex, is_find_only=find_only)
                
            # Set Model
            self.model = ReviewTableModel(self.current_review_items)
            self.review_table.setModel(self.model)
            # Resize
            self.review_table.resizeColumnsToContents()
            self.review_table.resizeRowsToContents()
            self.review_table.horizontalHeader().setStretchLastSection(True)
            # Set Col 0 width fix
            self.review_table.setColumnWidth(0, 30)
            self.review_table.setColumnWidth(4, 400) # Give more space to context
                
        finally:
            QApplication.restoreOverrideCursor()
            
        if not self.current_review_items:
            self.status_label.setText("No matches found.")
            return

        # 3. Show Review UI
        self.tabs.setVisible(False)
        self.scope_group.setVisible(False)
        self.review_group.setVisible(True)
        self.status_label.setText(f"Found {len(self.current_review_items)} {'matches' if find_only else 'replacements'}.")
        
        # Adjust UI for Find vs Replace
        if find_only:
            self.review_table.setColumnHidden(0, True) # Hide Checkboxes
            self.review_top_widget.setVisible(False)   # Hide Top Selection Buttons
            self.btn_rev_apply.setVisible(False)       # Hide Apply Button
            self.btn_rev_cancel.setText("Back")        # Change Cancel to Back
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
            
            # Line/Col Calculation
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
                
                # If no change, skip in replace mode
                if original == new_t: continue
            else:
                # Find Only mode: uncheck by default if we want? 
                # Or check to allow "Select and Delete"? No, Find All shouldn't delete.
                # Just check so user sees them highlighted.
                item_checked = False
            
            # Context HTML
            c_start = max(0, start - 20)
            c_end = min(len(text), end + 20)
            
            prefix = html.escape(text[c_start:start])
            suffix = html.escape(text[end:c_end])
            orig_esc = html.escape(original)
            
            diff_html = ""
            if is_find_only:
                # Highlight only
                diff_html = f"{prefix}<span style='background-color:#ffff00; color:black;'><b>{orig_esc}</b></span>{suffix}"
            else:
                # Diff style
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
        self.review_table.setModel(None) # Clear memory
        
    def apply_review_selection(self):
        # Use data from model (it might have been updated by checkboxes)
        to_apply = [x for x in self.current_review_items if x['checked']]
        if not to_apply: return
        
        to_apply.sort(key=lambda x: (int(x['page_num']), x['span'][0]), reverse=True)
        
        # Save current page first!
        self.mainwindow.save_current_page_data()
        
        if self.review_is_global:
            self.mainwindow.push_global_undo("Review Apply")
            
        from collections import defaultdict
        by_page = defaultdict(list)
        for item in to_apply:
            by_page[item['page_num']].append(item)
            
        count = 0
        target_dict = self.mainwindow.pages_left if self.rb_left.isChecked() else self.mainwindow.pages_right_text
        
        try: curr_p = int(self.mainwindow.spin_page.text())
        except: curr_p = -999
        target_editor = self.get_target_editor()
        editor_handles_current = (
            self.rb_left.isChecked() or self.mainwindow.is_text_source_selected()
        )
            
        for p_num, items in by_page.items():
            is_current_target = editor_handles_current and int(p_num) == curr_p
            txt = target_editor.toPlainText() if is_current_target else target_dict.get(p_num, "")
            
            # Create new text by applying patches
            # Since items are sorted reverse, index shifting is handled?
            # Items are sorted by page THEN span start desc.
            # But here `items` is list for ONE page.
            # Need to ensure `items` for this page are sorted reverse by start index.
            items.sort(key=lambda x: x['span'][0], reverse=True)
            
            new_txt = txt
            for item in items:
                start, end = item['span']
                repl_t = item['new']
                orig_t = item['original']
                
                # Verify context match
                if new_txt[start:end] == orig_t:
                     new_txt = new_txt[:start] + repl_t + new_txt[end:]
                     count += 1
            
            # Update memory
            target_dict[p_num] = new_txt
            self.mainwindow.mark_page_dirty(p_num, self.rb_left.isChecked())
            if is_current_target and new_txt != txt:
                cursor = target_editor.textCursor()
                cursor.beginEditBlock()
                cursor.select(QTextCursor.SelectionType.Document)
                cursor.insertText(new_txt)
                cursor.endEditBlock()
            
        if self.review_is_global:
            self.mainwindow.finalize_global_action(changed=count > 0)
            
        self.close_review()
        self.status_label.setText(f"Applied {count} changes.")
        
    def _compile_regex_from_ui(self):
        pattern = self.cb_find.currentText()
        repl_mode = self.combo_repl_mode.currentText()
        
        # Translate Mode Special Case
        if "Translate" in repl_mode:
             if not pattern:
                 # If no filter pattern, match ANY of the source chars
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
        """Handler for Diff Filter Review"""
        self.review_group.setVisible(False)
        self.current_review_items = []
        self.review_is_global = is_global
        
        # 1. Compile filters
        regex_old = None
        regex_new = None
        regex_scope = None
        pat_old = self.txt_diff_filter_old.text()
        pat_new = self.txt_diff_filter_new.text()
        pat_scope = self.txt_diff_scope_regex.text()
        
        try:
             if pat_old: regex_old = re.compile(pat_old)
             if pat_new: regex_new = re.compile(pat_new)
             if pat_scope: regex_scope = re.compile(pat_scope)
        except Exception as e:
             QMessageBox.critical(self, "Regex Error", str(e))
             return

        # 2. Determine target side
        # Use selection from "Target Text" groupbox which dictates "Target" side.
        # But for Diff, we usually compare Left vs Right.
        # If Target=Right, we modify Right to match Left? Or vice versa?
        # Standard: applying changes TO Target.
        # Implies Source is the OTHER side.
        
        target_is_left = self.rb_left.isChecked()
        self.review_target_is_left = target_is_left
        target_side = "Left" if target_is_left else "Right"
        
        # 3. Pages
        start_page = self.mainwindow.project_config.get('start_page', 1)
        end_page = self.mainwindow.project_config.get('end_page', 1) # Default 1? No usually max.
        
        # For current page:
        if not is_global:
            try:
                p = int(self.mainwindow.spin_page.text())
                pages = [p]
            except: pages = []
        else:
             # Need to know max pages?
             # From project config or scan?
             # Let's use start/end from config if available, or scan memory.
             # Memory pages keys.
             keys_l = set(self.mainwindow.pages_left.keys())
             keys_r = set(self.mainwindow.pages_right_text.keys())
             all_pages = sorted(list(keys_l | keys_r))
             pages = all_pages
             
        # 4. Process
        # 4. Start Worker
        self.progress_dialog = QProgressDialog("Analyzing differences...", "Cancel", 0, len(pages), self)
        self.progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress_dialog.setMinimumDuration(200)
        self.progress_dialog.setValue(0)
        
        self.diff_worker_thread = ReviewDiffWorker(
            pages, 
            self.mainwindow.pages_left, 
            self.mainwindow.pages_right_text,
            target_is_left,
            regex_old, regex_new, regex_scope,
            self.chk_diff_insert.isChecked(),
            self.chk_diff_delete.isChecked(),
            self.chk_diff_replace.isChecked(),
            self.txt_custom_replace.text() if self.chk_custom_replace.isChecked() else None,
            scope_exclude=self.rb_diff_scope_exclude.isChecked(),
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
        self.review_table.setColumnHidden(0, False)
        self.review_top_widget.setVisible(True)
        self.btn_rev_apply.setVisible(True)
        self.btn_rev_cancel.setText("Cancel Review")
        self.review_table.setColumnWidth(0, 30)
        self.review_table.setColumnWidth(4, 400)
        self.review_table.resizeRowsToContents()
        self.status_label.setText(f"Found {len(self.current_review_items)} diff items.")
        


    def on_review_table_dbl_click(self, index):
        """Double click to jump to page and exact location"""
        if not index.isValid(): return
        
        row = index.row()
        if 0 <= row < len(self.current_review_items):
            item = self.current_review_items[row]
            page_num = item.get('page_num')
            span = item.get('span')

            target_is_left = getattr(self, 'review_target_is_left', self.rb_left.isChecked())
            if not target_is_left and not self.mainwindow.is_text_source_selected():
                self.mainwindow.select_text_source()
                QApplication.processEvents()

            if page_num is not None:
                # 1. Switch Page
                if str(page_num) != self.mainwindow.spin_page.text():
                    self.mainwindow.spin_page.setText(str(page_num))
                    # Force load NOW to ensure editor has text
                    self.mainwindow.jump_page()
                    # Process events to allow UI update? 
                    QApplication.processEvents()
            
            # 2. Scroll to Span
            if span:
                editor = self.mainwindow.edit_left if target_is_left else self.mainwindow.edit_right
                if not editor: return

                cursor = editor.textCursor()
                text = editor.toPlainText()
                expected_start, expected_end = span
                start = max(0, min(int(expected_start), len(text)))
                end = max(start, min(int(expected_end), len(text)))
                original = item.get('original', '')

                # Review 生成后文本可能变化；原位置失效时选择距离旧位置最近的原文。
                if original and text[start:end] != original:
                    positions = [m.start() for m in re.finditer(re.escape(original), text)]
                    if positions:
                        start = min(positions, key=lambda pos: abs(pos - expected_start))
                        end = start + len(original)
                    else:
                        self.status_label.setText("定位失败：当前页面已找不到 Review 中的原文。")
                        return

                start_qt = to_qt_pos(text, start)
                end_qt = to_qt_pos(text, end)
                try:
                    cursor.setPosition(start_qt)
                    cursor.setPosition(end_qt, QTextCursor.MoveMode.KeepAnchor)
                    editor.setTextCursor(cursor)
                    editor.ensureCursorVisible()
                    editor.setFocus()
                    editor.highlight_line_at_index(start_qt)
                    self.mainwindow.request_highlight_other(editor, start_qt)
                    self.status_label.setText(
                        f"已定位：第 {page_num} 页，第 {item.get('line', '')} 行"
                    )
                except Exception as exc:
                    self.status_label.setText(f"定位失败：{exc}")

    def reset_review_ui(self):
        """Reset the review UI to initial search state"""
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
        # Note: FindUseRegularExpression is handled separately via QRegularExpression if needed,
        # or we use Python re for robustness (especially for complex replaces).
        # QPlainTextEdit.find uses QTextDocument.FindFlags.
        return flags

    def on_find_next(self):
        self.save_search_history()
        editor = self.get_target_editor()
        
        # Use centralized regex compilation to handle Translate/Empty logic
        regex = self._compile_regex_from_ui()
        if not regex: return
        
        pattern = regex.pattern
            
        # Search
        found = False
        
        # Try finding using Python Regex manually (since Qt regex is different, and we need full compat)
        # Especially for Translate mode which uses [chars] pattern
        
        text = editor.toPlainText()
        cursor = editor.textCursor()
        start_pos = cursor.position()
             
        try:
             # Search from current position
             match = regex.search(text, start_pos)
             
             # If exact match at current position (length > 0) and we want 'Next', we might need to advance?
             # But regex.search searches *forward*. If it matches at start_pos, it means current cursor is start of match.
             # If we want *next* occurrence, we should start from start_pos + 1?
             # Standard "Find Next" behavior: if we have selection, move past it?
             if cursor.hasSelection():
                  # If the selection *is* the match, we advance
                  sel_txt = cursor.selectedText()
                  # This is tricky. Let's just search from cursor.position() (end of selection usually).
                  # If cursor is at start of selection (reverse selection), position() is start.
                  # Logic: start search from max(cursor.position(), cursor.anchor())?
                  # QPlainTextEdit cursor position is usually the "moving" end.
                  # Let's search from max(cursor.selectionStart(), cursor.selectionEnd()) if selection exists?
                  # Actually let's just search from cursor.position().
                  pass
                  
             # However, if we just found a match, the cursor is at end of match (usually).
             # If we search from there, we find the next one.
             
             # Problem: If user clicks inside text, cursor is there.
             match = regex.search(text, start_pos)
             
             if not match:
                 # Wrap around
                 match = regex.search(text, 0)
             
             if match:
                 # Select it
                 new_cursor = editor.textCursor()
                 new_cursor.setPosition(match.start())
                 # Keep anchor? No, set selection.
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
        self.save_search_history()
        editor = self.get_target_editor()
        cursor = editor.textCursor()
        if not cursor.hasSelection():
            self.on_find_next()
            return
            
        # Verify selection matches find?
        # Actually standard Replace just replaces selection if it matches, or finds next.
        # Simplification: Just replace selection if user clicks replace
        # But we need to calculate replacement string.
        
        repl_text = self.cb_replace.currentText()
        
        selected_text = cursor.selectedText()
        new_text = repl_text
        
        # If Regex Mode or Python Mode, we need to re-evaluate based on the selection
        if self.chk_regex.isChecked():
            # If Regex Group mode ($1)
            # We need to match the selection again to get groups
            try:
                pattern = self.cb_find.currentText()
                flags = 0
                if not self.chk_case.isChecked(): flags |= re.IGNORECASE
                match = re.fullmatch(pattern, selected_text, flags)
                
                if match:
                    repl_mode = self.combo_repl_mode.currentText()
                    if "Python Lambda" in repl_mode:
                        # Dangerous!
                        try:
                            # Context: match
                            func = eval(f"lambda match: {repl_text}")
                            new_text = str(func(match))
                        except Exception as e:
                            self.status_label.setText(f"Python Error: {str(e)}")
                            return
                    elif "Regex Group" in repl_mode:
                        # Use re.sub logic
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


