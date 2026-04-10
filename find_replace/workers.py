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

class ReviewDiffWorker(QThread):
    progress = pyqtSignal(int)
    finished = pyqtSignal(list, str) # items, msg
    
    def __init__(self, pages, pages_left, pages_right, target_is_left, 
                 regex_old, regex_new, regex_scope,
                 check_insert, check_delete, check_replace):
        super().__init__()
        self.pages = pages
        self.pages_left = pages_left
        self.pages_right = pages_right
        self.target_is_left = target_is_left
        self.regex_old = regex_old
        self.regex_new = regex_new
        self.regex_scope = regex_scope
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
        
        # Pre-compute valid scope spans if regex_scope is defined
        scope_spans = []
        if self.regex_scope:
            scope_spans = [m.span() for m in self.regex_scope.finditer(text_a)]
        
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
            
             # Check Scope
             if self.regex_scope:
                 # Check if the diff overlap with any scope match
                 in_scope = False
                 for (start, end) in scope_spans:
                     if not (i2 < start or i1 > end): # Overlaps
                         in_scope = True
                         break
                 if not in_scope:
                     continue
            
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
            
             # Line/Col Calculation
             line = text_a.count('\n', 0, i1) + 1
             last_nl = text_a.rfind('\n', 0, i1)
             if last_nl == -1: col = i1 + 1
             else: col = i1 - last_nl

             item_data = {
                'page_num': page_num,
                'line': line,
                'col': col,
                'span': (i1, i2), 
                'original': old_segment,
                'new': new_segment,
                'context_html': diff_html,
                'checked': True
             }
             self.items.append(item_data)

