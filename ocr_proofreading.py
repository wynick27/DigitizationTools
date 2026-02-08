import sys
import os
import json
import re
import fitz  # PyMuPDF
import difflib
import requests
import base64
import time

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                             QTextEdit, QPlainTextEdit, QLabel, QPushButton, QSplitter, QFileDialog, QInputDialog,
                             QMessageBox, QListWidget, QGraphicsView, QGraphicsScene, 
                             QGraphicsRectItem, QGraphicsPixmapItem, QLineEdit, 
                             QFormLayout, QDialog, QDialogButtonBox, QSpinBox, 
                             QTabWidget, QToolBar, QComboBox, QCheckBox, QMenu,
                             QRadioButton, QButtonGroup, QGroupBox, QListWidgetItem, QGridLayout,
                             QTableView, QHeaderView, QAbstractItemView, QStyle, QProgressDialog, QSizePolicy,
                             QStackedWidget)
from PyQt6.QtGui import (QTextCursor, QColor, QSyntaxHighlighter, QTextCharFormat, QTextFormat,
                         QAction, QPixmap, QImage, QPainter, QBrush, QPen, QFont, QImageReader, QTextOption,
                         QAbstractTextDocumentLayout, QTextDocument, QPalette)
from PyQt6.QtWidgets import QProgressBar, QStyledItemDelegate
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QEvent, QThread, pyqtSlot, QSize, QRect, QPoint, QAbstractTableModel, QModelIndex
import bisect

from ocr_lib.app_logic.find_replace import FindReplaceDialog
from ocr_lib.core.image_utils import get_page_image
from ocr_lib.core.text_utils import TextStripper
from ocr_lib.app_logic.workers import DiffWorker, OCRWorker
from ocr_lib.app_logic.image_export import TextToBBoxMapper, BBoxMerger, ImageStitcher



# ==========================================
# 0.2 Review Model / View (Refactored to ocr_lib)
# ==========================================

# ==========================================
# 0.5 Unicode Helpers
# ==========================================

def to_qt_pos(full_text: str, py_pos: int) -> int:
    """Convert Python string index to Qt TextCursor position (UTF-16 code units)."""
    head = full_text[:py_pos]
    return len(head.encode('utf-16-le')) // 2

def to_py_pos(full_text: str, qt_pos: int) -> int:
    """Convert Qt TextCursor position to Python string index."""
    curr_qt = 0
    for i, c in enumerate(full_text):
        if curr_qt >= qt_pos: 
            return i
        curr_qt += 2 if ord(c) > 0xFFFF else 1
    return len(full_text)


def get_page_image(doc, img_dir, real_page_num):
    """
    Helper: Extract image bytes from PDF or local directory.
    Priority:
    1. PDF Embedded Image (if single)
    2. PDF Render (High DPI)
    3. Local File (page_X.jpg/png)
    """
    img_bytes = None
    
    # 1. Try PDF
    if doc:
        try:
            if 0 < real_page_num <= len(doc):
                page = doc[real_page_num-1]
                
                # Try Raw Extraction (Preferred for embedded images)
                images = page.get_images()
                if len(images) == 1:
                    xref = images[0][0]
                    base_image = doc.extract_image(xref)
                    img_bytes = base_image["image"]
                else:
                    # Fallback High DPI Render
                    pix = page.get_pixmap(matrix=fitz.Matrix(3.0, 3.0))
                    img_bytes = pix.tobytes("png")
        except Exception as e:
            # print(f"PDF extract error: {e}")
            pass
            
    # 2. Try Local File
    if not img_bytes and img_dir:
        candidates = [f"page_{real_page_num}", f"{real_page_num}"]
        exts = [".jpg", ".jpeg", ".png", ".bmp"]
        found_path = None
        for c in candidates:
            for ext in exts:
                 p = os.path.join(img_dir, c + ext)
                 if os.path.exists(p):
                     found_path = p
                     break
            if found_path: break
            
        if found_path:
             try:
                 with open(found_path, "rb") as f:
                     img_bytes = f.read()
             except: pass
             
    return img_bytes


# ==========================================
# 0. 全局工具与配置管理
# ==========================================

# 尝试导入本地 OCR
HAS_LOCAL_OCR = False
try:
    from paddleocr import PaddleOCRVL
    HAS_LOCAL_OCR = True
    print("Local PaddleOCR detected.")
except ImportError:
    print("PaddleOCR not found. Local OCR disabled.")


DEFAULT_GLOBAL_CONFIG = {
    "ocr_api_url": "",
    "ocr_api_token": "",
    "ocr_engine": "remote", # remote or local
    "find_history": [],
    "replace_history": []
}

DEFAULT_PROJECT_CONFIG = {
    "name": "Default Project",
    "pdf_path": "",
    "image_dir": "",
    "start_page": 1,
    "end_page": 1,
    "page_offset": 0,
    "text_path_left": "",
    "text_path_right": "", # 第二版本文本
    "ocr_json_path": "ocr_results",       # OCR 数据目录
    "regex_left": r"^\*\*(.*?)\*\*",
    "regex_right": r"^([a-zA-Z]*?)",
    "regex_group_left": 0,
    "regex_group_right": 0,
    "use_pdf_render": False,
}

class ConfigManager:
    def __init__(self, filepath="config.json"):
        self.filepath = filepath
        self.data = {
            "global": DEFAULT_GLOBAL_CONFIG.copy(),
            "projects": [DEFAULT_PROJECT_CONFIG.copy()],
            "active_project": "Default Project"
        }
        self.load()

    def load(self):
        if not os.path.exists(self.filepath):
            return

        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                
            # Migration Logic: Check if it's flat (old style)
            if "projects" not in loaded:
                print("Migrating legacy config to new structure...")
                # It's a flat config, migrate to Default Project
                new_project = DEFAULT_PROJECT_CONFIG.copy()
                # Copy known project keys
                for key in new_project:
                    if key in loaded:
                        new_project[key] = loaded[key]
                new_project["name"] = "Default Project"
                
                # Copy known global keys
                if "ocr_api_url" in loaded:
                    self.data["global"]["ocr_api_url"] = loaded["ocr_api_url"]
                if "ocr_api_token" in loaded:
                    self.data["global"]["ocr_api_token"] = loaded["ocr_api_token"]
                    
                self.data["projects"] = [new_project]
            else:
                self.data = loaded
                # Ensure structure integrity
                if "global" not in self.data: 
                    self.data["global"] = DEFAULT_GLOBAL_CONFIG.copy()
                if "projects" not in self.data: 
                    self.data["projects"] = [DEFAULT_PROJECT_CONFIG.copy()]
                if "active_project" not in self.data:
                    self.data["active_project"] = self.data["projects"][0]["name"]
                    
        except Exception as e:
            print(f"Config load error: {e}")

    def save(self):
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"Config save error: {e}")

    def get_global(self):
        return self.data["global"]

    def get_projects(self):
        return self.data["projects"]

    def get_project(self, name):
        for p in self.data["projects"]:
            if p["name"] == name:
                return p
        return None

    def get_active_project(self):
        name = self.data.get("active_project")
        p = self.get_project(name)
        if p: return p
        # Fallback
        if self.data["projects"]:
            self.data["active_project"] = self.data["projects"][0]["name"]
            return self.data["projects"][0]
        return DEFAULT_PROJECT_CONFIG.copy()

    def set_active_project(self, name):
        if self.get_project(name):
            self.data["active_project"] = name
            self.save()

    def create_project(self, name):
        if self.get_project(name): return False
        new_p = DEFAULT_PROJECT_CONFIG.copy()
        new_p["name"] = name
        self.data["projects"].append(new_p)
        self.save()
        return True
        
    def delete_project(self, name):
        # Don't delete if it's the only one
        if len(self.data["projects"]) <= 1: return False
        
        self.data["projects"] = [p for p in self.data["projects"] if p["name"] != name]
        
        # Reset active if needed
        if self.data["active_project"] == name:
            self.data["active_project"] = self.data["projects"][0]["name"]
        
        self.save()
        return True

PAGE_PATTERN = re.compile(r"<(\d+)>")

def read_text_to_pages(file_path: str) -> dict[int, str]:
    pages = {}
    if not os.path.exists(file_path): 
        return pages
    try:
        current_page = None
        current_content = []
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                match = PAGE_PATTERN.fullmatch(line.strip())
                if match:
                    if current_page is not None:
                        pages[current_page] = "\n".join(current_content)
                    current_page = int(match.group(1))
                    current_content = []
                else:
                    current_content.append(line)
            if current_page is not None:
                pages[current_page] = "\n".join(current_content)
    except Exception as e:
        print(f"Read error {file_path}: {e}")
    return pages

def write_pages_to_file(pages: dict[int, str], file_path: str):
    try:
        sorted_pages = sorted(pages.keys())
        with open(file_path, 'w', encoding='utf8') as f:
            for page in sorted_pages:
                text = pages[page]
                f.write(f'<{page}>\n')
                f.write(f'{text}\n')
        print(f"Saved to {file_path}")
    except Exception as e:
        print(f"Save error: {e}")

# ==========================================
# 1. 自定义编辑器 (支持 Diff 交互) & Highlighter
# ==========================================

class DiffSyntaxHighlighter(QSyntaxHighlighter):
    def __init__(self, document):
        super().__init__(document)
        self.diff_ranges = [] # List of tuples (start, end)
        self.diff_starts = [] # List of start positions for bisect
        self.diff_ranges = [] # List of tuples (start, end)
        self.diff_starts = [] # List of start positions for bisect
        self.regex_pattern = None
        self.regex_group = 0
        
        # 预定义格式
        self.diff_fmt = QTextCharFormat()
        self.diff_fmt.setForeground(QColor("red"))
        self.diff_fmt.setBackground(QColor("#FFEEEE")) # 浅红背景
        
        self.regex_fmt = QTextCharFormat()
        self.regex_fmt.setBackground(QColor("#E0F0FF")) # 浅蓝
        
        # Merge Format (Diff FG + Regex BG)
        self.both_fmt = QTextCharFormat()
        self.both_fmt.setForeground(QColor("red"))
        self.both_fmt.setBackground(QColor("#E0F0FF"))
        
    def set_diff_data(self, opcodes, is_left):
        self.diff_ranges = []
        text = self.document().toPlainText()
        
        for tag, i1, i2, j1, j2 in opcodes:
            if tag == 'equal': continue
            s_py, e_py = (i1, i2) if is_left else (j1, j2)
            if s_py < e_py:
                s_qt = to_qt_pos(text, s_py)
                e_qt = to_qt_pos(text, e_py)
                self.diff_ranges.append((s_qt, e_qt))
        
        self.diff_ranges.sort() # Ensure sorted
        self.diff_starts = [r[0] for r in self.diff_ranges]
        self.rehighlight()
        
    def set_regex(self, regex_str, group_id=0):
        if not regex_str:
            self.regex_pattern = None
        else:
            try:
                self.regex_pattern = re.compile(regex_str)
            except:
                self.regex_pattern = None
        self.regex_group = group_id
        self.rehighlight()

    def highlightBlock(self, text):
        length = len(text)
        if length == 0: return

        # Optimization: use boolean array to track states
        has_diff = [False] * length
        has_regex = [False] * length
        
        block_start = self.currentBlock().position()
        block_end = block_start + length
        
        # 1. Fill Diff
        if self.diff_ranges:
            end_idx = bisect.bisect_right(self.diff_starts, block_end)
            start_search = bisect.bisect_right(self.diff_starts, block_start)
            if start_search > 0: start_search -= 1
            
            count = 0 
            for i in range(start_search, end_idx):
                if count > 1000: break 
                s, e = self.diff_ranges[i]
                
                intersect_start = max(s, block_start)
                intersect_end = min(e, block_end)
                
                if intersect_start < intersect_end:
                    rel_s = intersect_start - block_start
                    rel_e = intersect_end - block_start
                    has_diff[rel_s:rel_e] = [True] * (rel_e - rel_s)
                count += 1
        
        # 2. Fill Regex
        if self.regex_pattern:
            count = 0
            for match in self.regex_pattern.finditer(text):
                if count > 100: break 
                try:
                    s, e = match.start(self.regex_group), match.end(self.regex_group)
                except IndexError:
                    # Fallback if group not found
                    s, e = match.start(), match.end()
                    
                # Bound checks although finditer on text should be within text
                s = max(0, s); e = min(length, e)
                if s < e:
                    has_regex[s:e] = [True] * (e - s)
                count += 1

        # 3. Apply Formats
        # Run-Length Encoding approach to minimize setFormat calls
        current_start = 0
        current_type = (has_diff[0], has_regex[0]) 
        
        for i in range(1, length):
            new_type = (has_diff[i], has_regex[i])
            if new_type != current_type:
                self.apply_format_chunk(current_start, i - current_start, current_type)
                current_start = i
                current_type = new_type
        
        self.apply_format_chunk(current_start, length - current_start, current_type)

    def apply_format_chunk(self, start, length, flags):
        is_diff, is_regex = flags
        if not is_diff and not is_regex: return
        
        fmt = None
        if is_diff and is_regex:
            fmt = self.both_fmt
        elif is_diff:
            fmt = self.diff_fmt
        elif is_regex:
            fmt = self.regex_fmt
            
        if fmt:
            self.setFormat(start, length, fmt)


class LineNumberArea(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.codeEditor = editor

    def sizeHint(self):
        return QSize(self.codeEditor.line_number_area_width(), 0)

    def paintEvent(self, event):
        self.codeEditor.lineNumberAreaPaintEvent(event)


class DiffTextEdit(QPlainTextEdit):
    """
    支持 Ctrl+Hover 高亮和 Ctrl+Click 应用补丁的文本框
    """
    focus_in_signal = pyqtSignal()
    # 信号：点击了某个 Diff 块，请求应用到另一侧 (self_index_range, target_text)
    apply_patch_signal = pyqtSignal(tuple, str)
    # 信号：Alt+Click 将本侧内容推送到另一侧 (target_range, my_content)
    push_patch_signal = pyqtSignal(tuple, str)
    # 信号：Ctrl+Wheel 缩放请求 (delta)
    zoom_signal = pyqtSignal(int)

    def focusInEvent(self, event):
        self.focus_in_signal.emit()
        super().focusInEvent(event)
    
    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            # Emit zoom signal, consume event
            self.zoom_signal.emit(event.angleDelta().y())
            event.accept()
        else:
            super().wheelEvent(event)
    
    def __init__(self, side="left"):
        super().__init__()
        self.side = side # 'left' or 'right'
        self.diff_opcodes = [] # 存储 difflib 的 opcodes
        self.other_text_content = "" # 另一侧的完整文本，用于提取
        self.setFont(QFont("Consolas", 11))
        
        # 启用鼠标追踪以支持 Hover
        self.setMouseTracking(True)
        self._hovering_diff = False
        
        self.line_number_area = LineNumberArea(self)
        self.blockCountChanged.connect(self.update_line_number_area_width)
        self.updateRequest.connect(self.update_line_number_area)
        self.update_line_number_area_width(0)

    def line_number_area_width(self):
        digits = 1
        max_val = max(1, self.blockCount())
        while max_val >= 10:
            max_val //= 10
            digits += 1
        space = 3 + self.fontMetrics().horizontalAdvance('9') * digits + 5 # Margin
        return space

    def update_line_number_area_width(self, new_block_count):
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def update_line_number_area(self, rect, dy):
        if dy:
            self.line_number_area.scroll(0, dy)
        else:
            self.line_number_area.update(0, rect.y(), self.line_number_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self.update_line_number_area_width(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self.line_number_area.setGeometry(QRect(cr.left(), cr.top(), self.line_number_area_width(), cr.height()))

    def lineNumberAreaPaintEvent(self, event):
        painter = QPainter(self.line_number_area)
        painter.fillRect(event.rect(), QColor("#F0F0F0")) # Background

        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        bottom = top + self.blockBoundingRect(block).height()

        painter.setPen(Qt.GlobalColor.black)
        
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                number = str(block_number + 1)
                painter.drawText(0, int(top), self.line_number_area.width() - 3, self.fontMetrics().height(),
                                 Qt.AlignmentFlag.AlignRight, number)
            
            block = block.next()
            top = bottom
            bottom = top + self.blockBoundingRect(block).height()
            block_number += 1

    def highlight_line_at_index(self, idx):
        """高亮指定字符索引所在的行"""
        self.blockSignals(True)
        
        # 清除之前的 ExtraSelections (除了 Diff 高亮?)
        # 实际上 diff 高亮是直接作用于 TextCharFormat 的，而 ExtraSelections 是独立的图层
        # 这里仅用于行高亮
        
        cursor = self.textCursor()
        cursor.setPosition(idx)
        
        selection = QTextEdit.ExtraSelection()
        selection.format.setBackground(QColor("#FFFFAA")) # 淡黄色高亮
        fmt = selection.format
        fmt.setProperty(QTextFormat.Property.FullWidthSelection, True)
        selection.format = fmt
        selection.cursor = cursor
        selection.cursor.clearSelection() #只是定位
        
        self.setExtraSelections([selection])
        
        self.blockSignals(False)

    def set_diff_data(self, opcodes, other_text):
        self.diff_opcodes = opcodes
        self.other_text_content = other_text

    def get_opcode_at_position(self, pos):
        """根据鼠标坐标获取对应的 opcode"""
        cursor = self.cursorForPosition(pos)
        qt_idx = cursor.position()
        
        # Convert to Python index for opcode lookup
        text = self.toPlainText()
        idx = to_py_pos(text, qt_idx)
        
        # 遍历 opcodes 查找当前索引是否在差异区间内
        for tag, i1, i2, j1, j2 in self.diff_opcodes:
            if tag == 'equal': continue
            
            # 判断是在左侧还是右侧
            if self.side == 'left':
                if i1 <= idx <= i2:
                    return (tag, i1, i2, j1, j2)
            else:
                if j1 <= idx <= j2:
                    return (tag, i1, i2, j1, j2)
        return None

    def mouseMoveEvent(self, event):
        # 检查是否按住 Ctrl
        modifiers = QApplication.keyboardModifiers()
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            opcode = self.get_opcode_at_position(event.pos())
            if opcode:
                self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
                self._hovering_diff = True
            else:
                self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
                self._hovering_diff = False
        elif modifiers & Qt.KeyboardModifier.AltModifier:
            opcode = self.get_opcode_at_position(event.pos())
            if opcode:
                self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
                self._hovering_diff = True
            else:
                self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
                self._hovering_diff = False
        else:
            self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
            self._hovering_diff = False
        super().mouseMoveEvent(event)

    def clear_highlight(self):
        """清除高亮（ExtraSelections）"""
        self.setExtraSelections([])

    def mousePressEvent(self, event):
        # 处理 Ctrl + Click
        modifiers = QApplication.keyboardModifiers()
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            opcode = self.get_opcode_at_position(event.pos())
            if opcode:
                tag, i1, i2, j1, j2 = opcode
                # Apply Patch Logic
                # Left Click: Apply Change?
                # Right now, emit signal
                
                # Extract text from OTHER side
                # For Left Editor, other side is Right.
                # If Replace/Insert, we need text from Right(j1:j2)
                # But self.other_text_content needs to be set!
                
                start_idx, end_idx = (i1, i2) if self.side == 'left' else (j1, j2)
                
                target_text = ""
                if tag == 'replace':
                    # Need corresponding text from OTHER side
                    o_start, o_end = (j1, j2) if self.side == 'left' else (i1, i2)
                    if self.other_text_content:
                        target_text = self.other_text_content[o_start:o_end]
                elif tag == 'delete':
                    target_text = "" # Delete means replace with empty
                elif tag == 'insert':
                     # Insert means we are missing something present in other side
                     o_start, o_end = (j1, j2) if self.side == 'left' else (i1, i2)
                     if self.other_text_content:
                        target_text = self.other_text_content[o_start:o_end]
                        
                self.apply_patch_signal.emit((start_idx, end_idx), target_text)
                return

        super().mousePressEvent(event)


class PreviewTextEdit(QTextEdit):
    """
    Rich Text Preview with Diff Interaction Support
    Inherits QTextEdit instead of QPlainTextEdit
    """
    # Signals mirroring DiffTextEdit for compatibility
    apply_patch_signal = pyqtSignal(tuple, str)
    # Scroll Sync Signal
    scroll_sync_signal = pyqtSignal(int)
    
    def __init__(self, side="left"):
        super().__init__()
        self.side = side
        self.diff_opcodes = []
        self.other_text_content = "" # Raw text of other side
        self.setFont(QFont("Consolas", 11))
        self.setMouseTracking(True)
        
    def set_diff_data(self, opcodes, other_text):
        self.diff_opcodes = opcodes
        self.other_text_content = other_text
        
    def get_opcode_at_position(self, pos):
        # QTextEdit cursor lookup
        cursor = self.cursorForPosition(pos)
        qt_idx = cursor.position()
        
        # Approximate mapping: using PlainText index
        # This might be slightly off for Rich Text / HTML if hidden tags exist?
        # extra logic needed for exact mapping?
        # For now, assume toPlainText() index aligns with OpCode index (which is based on stripped/plain text)
        
        # But wait, opcodes are based on STRIPPED text if ignore_tags is On.
        # Preview displays STRIPPED text? No, Preview displays RENDERED text.
        # If we used setMarkdown, the text content IS the stripped text (mostly).
        # So we can try mapping directly.
        
        idx = qt_idx # Simplify for now
        
        for tag, i1, i2, j1, j2 in self.diff_opcodes:
            if tag == 'equal': continue
            
            if self.side == 'left':
                if i1 <= idx <= i2:
                    return (tag, i1, i2, j1, j2)
            else:
                if j1 <= idx <= j2:
                    return (tag, i1, i2, j1, j2)
        return None

    def mouseMoveEvent(self, event):
        modifiers = QApplication.keyboardModifiers()
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            if self.get_opcode_at_position(event.pos()):
                self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
            else:
                self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        else:
            self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        modifiers = QApplication.keyboardModifiers()
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            opcode = self.get_opcode_at_position(event.pos())
            if opcode:
                # Same Logic as DiffTextEdit
                tag, i1, i2, j1, j2 = opcode
                start_idx, end_idx = (i1, i2) if self.side == 'left' else (j1, j2)
                
                target_text = ""
                if tag == 'replace' or tag == 'insert':
                    o_start, o_end = (j1, j2) if self.side == 'left' else (i1, i2)
                    if self.other_text_content:
                        target_text = self.other_text_content[o_start:o_end]
                        
                self.apply_patch_signal.emit((start_idx, end_idx), target_text)
                return
                
        super().mousePressEvent(event)
        
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
             event.ignore() # Let parent handle zoom? or impl zoom
        else:
             super().wheelEvent(event)

    def handle_patch_click(self, opcode):
        tag, i1, i2, j1, j2 = opcode
        
        # 逻辑：点击某侧的差异块，意为“将这一块的内容变成另一侧的样子”
        # 或者“将这一块的内容推送到另一侧”。
        # 通常 Beyond Compare 的逻辑是：点击箭头将当前侧内容覆盖到另一侧。
        # 这里的实现：点击红色区域 -> 将该区域内容替换为另一侧对应区域的内容 (Accept Change)
        
        target_text = ""
        my_range = (0, 0)
        
        if self.side == 'left':
            my_range = (i1, i2)
            # 获取右侧对应文本 (j1:j2)
            target_text = self.other_text_content[j1:j2]
        else:
            my_range = (j1, j2)
            # 获取左侧对应文本 (i1:i2)
            target_text = self.other_text_content[i1:i2]
            
        # 发射信号，由主窗口执行替换操作
        self.apply_patch_signal.emit(my_range, target_text)

    def handle_push_click(self, opcode):
        tag, i1, i2, j1, j2 = opcode
        
        # Logic: Alt+Click = 将“我”的内容推送到“另一侧”
        # 我是 left: 我的内容在 i1:i2, 目标在 j1:j2
        # 我是 right: 我的内容在 j1:j2, 目标在 i1:i2
        
        my_range = (0, 0)
        target_range = (0, 0)
        text_to_push = ""
        current_text = self.toPlainText()
        
        if self.side == 'left':
            my_range = (i1, i2)
            target_range = (j1, j2)
            text_to_push = current_text[i1:i2]
        else:
            my_range = (j1, j2) # Index in right text
            target_range = (i1, i2) # Index in left text
            text_to_push = current_text[j1:j2]
            
        # 发射信号: (目标区间, 要替换成的内容)
        self.push_patch_signal.emit(target_range, text_to_push)


# ==========================================
# 2. 图像画布 (支持缩放、BBox)
# ==========================================

class ImageCanvas(QGraphicsView):
    bbox_clicked = pyqtSignal(int)
    def __init__(self):
        super().__init__()
        self.scene = QGraphicsScene()
        self.setScene(self.scene)
        #self.setRenderHint(QPixmap.TransformationMode.SmoothTransformation)
        self.scale_factor = 1.0
        # 拖拽相关
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)

    def load_content(self, pixmap, ocr_data=None):
        self.scene.clear()
        self.highlight_item = None # Fix: Reset C++ object wrapper
        if pixmap:
            self.scene.addPixmap(pixmap)
            self.setSceneRect(0, 0, pixmap.width(), pixmap.height())
            if ocr_data:
                self.draw_bboxes(ocr_data)
        # self.scale_factor = 1.0 # Removed to persist zoom
        # self.resetTransform()   # Removed to persist zoom
        
    def draw_bboxes(self, ocr_data):
        pen = QPen(QColor(255, 0, 0, 200))
        pen.setWidth(2)
        
        for i, item in enumerate(ocr_data):
            # 兼容 PaddleOCR 格式
            # item 可能是 dict {'bbox':...} (v3代码) 或 list [points, (text, conf)]
            x, y, w, h = 0, 0, 0, 0
            text = ""
            
            if isinstance(item, dict) and 'bbox' in item:
                bbox = item['bbox'] # [x1, y1, x2, y2]
                x, y = bbox[0], bbox[1]
                w, h = bbox[2]-x, bbox[3]-y
                text = item.get('text', '')
            elif isinstance(item, list) and len(item) == 2:
                # Paddle raw: [[[x1,y1],...], ("text", conf)]
                pts = item[0]
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                x, y = min(xs), min(ys)
                w, h = max(xs)-x, max(ys)-y
                text = item[1][0]
            
            rect = QGraphicsRectItem(x, y, w, h)
            rect.setPen(pen)
            rect.setToolTip(text) # 鼠标悬停显示文字
            rect.setData(0, i)
            self.scene.addItem(rect)

    def mousePressEvent(self, event):
        if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) and (event.button() == Qt.MouseButton.LeftButton):
             items = self.items(event.pos())
             for item in items:
                 idx = item.data(0)
                 if idx is not None:
                     self.bbox_clicked.emit(idx)
                     event.accept()
                     return
        super().mousePressEvent(event)
        
    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if event.angleDelta().y() > 0:
                self.zoom(1.1)
            else:
                self.zoom(0.9)
            event.accept()
        else:
            super().wheelEvent(event)

    def zoom(self, factor):
        self.scale(factor, factor)
        self.scale_factor *= factor

    def ensure_visible_bbox(self, x, y, w, h):
        """确保指定的矩形区域在视图中可见"""
        # 获取场景坐标对应的 Rect
        # 这里 x,y,w,h 已经是场景坐标（基于 Pixmap）
        self.ensureVisible(x, y, w, h, 50, 50) # margin 50

    def set_highlight_bbox(self, x, y, w, h):
        """设置高亮矩形 (蓝色)"""
        # 移除旧的 highlight
        if hasattr(self, 'highlight_item') and self.highlight_item:
            try:
                self.scene.removeItem(self.highlight_item)
            except RuntimeError:
                pass # Already deleted by C++
            self.highlight_item = None
            
        if w > 0 and h > 0:
            pen = QPen(QColor(0, 0, 255, 200)) # Blue
            pen.setWidth(3)
            self.highlight_item = QGraphicsRectItem(x, y, w, h)
            self.highlight_item.setPen(pen)
            self.highlight_item.setZValue(10) # Top layer
            self.scene.addItem(self.highlight_item)
            self.ensure_visible_bbox(x, y, w, h)



# ==========================================
# 2.2 OCR Worker (Async) - Imported from ocr_lib
# ==========================================

# ==========================================
# 4. Smart Image Export Helpers - Imported from ocr_lib
# ==========================================



class ImageExportWorker(QThread):
    progress = pyqtSignal(str) # Message
    progress_val = pyqtSignal(int) # Value
    finished = pyqtSignal(bool, str) # Success, Msg

    def __init__(self, entries, project_config, fmt, export_dir, side, force_overwrite=False):
        super().__init__()
        self.entries = entries
        self.project_config = project_config
        self.fmt = fmt # 'json' or 'mdx'
        self.export_dir = export_dir
        self.side = side # 'left' or 'right'
        self.force_overwrite = force_overwrite
        
        self.is_running = True
        
        # Helpers
        self.mapper = TextToBBoxMapper(project_config.get('ocr_json_path', 'ocr_results'), project_config.get('page_offset', 0))
        self.merger = BBoxMerger()
        self.stitcher = ImageStitcher()

    def run(self):
        try:
            self.progress.emit("Initializing export...")
            
            # Prepare Image Dir
            out_img_dir = os.path.join(self.export_dir, "output_slices")
            if not os.path.exists(out_img_dir):
                os.makedirs(out_img_dir)
                
            # Load PDF
            doc = None
            pdf_path = self.project_config.get('pdf_path', '')
            if pdf_path and os.path.exists(pdf_path):
                try: doc = fitz.open(pdf_path)
                except: pass
                
            total = len(self.entries)
            
            for i, entry in enumerate(self.entries):
                if not self.is_running: break
                
                headword = entry['headword']
                # Limit filename length/chars
                safe_hw = re.sub(r'[\\/*?:"<>|]', '_', headword)
                safe_hw = safe_hw.strip()[:50]
                
                safe_hw = safe_hw.strip()[:50]
                
                # OPTIMIZATION: Throttle UI updates (every 10 items or last item)
                if i % 10 == 0 or i == total - 1:
                    self.progress.emit(f"Processing ({i+1}/{total}): {headword}...")
                    self.progress_val.emit(i + 1)
                
                # 1. Map
                bboxes = self.mapper.find_bboxes(entry['text'], entry['pages'])
                
                if bboxes:
                    # 2. Merge
                    merged = self.merger.merge(bboxes)
                    
                    # 3. Predict Size & Check
                    # Determine orientation from first box label?
                    is_vertical = any(b['label'] == 'vertical_text' for b in bboxes)
                    
                    pred_w, pred_h = self.stitcher.predict_size(merged, is_vertical)
                    
                    if pred_w > 65500 or pred_h > 65500:
                         self.progress.emit(f"SKIPPED {headword}: Size {pred_w}x{pred_h} > 65500")
                         continue

                    # 4. Save Path
                    # Format: {page_num}_{page_index}.jpg
                    page_num = entry['pages'][0] if entry['pages'] else 0
                    page_idx = entry.get('page_index', 0)
                    img_filename = f"{page_num}_{page_idx}.jpg"
                    save_path = os.path.join(out_img_dir, img_filename)
                    
                    # 5. Skip Check (Force Re-split)
                    should_stitch = True
                    if not self.force_overwrite and os.path.exists(save_path):
                        # check size
                        try:
                            reader = QImageReader(save_path)
                            size = reader.size()
                            if size.isValid():
                                # Tolerance +/- 5px
                                diff_w = abs(size.width() - pred_w)
                                diff_h = abs(size.height() - pred_h)
                                if diff_w < 5 and diff_h < 5:
                                    should_stitch = False
                                    # self.progress.emit(f"Skipped {headword} (Exists & Size Match)")
                                # else:
                                #    print(f"[DEBUG] Re-stitch {headword}: Existing {size.width()}x{size.height()} vs Pred {pred_w}x{pred_h}")
                            # else:
                            #    print(f"[DEBUG] Re-stitch {headword}: Existing invalid")
                        except Exception as e:
                             # print(f"[DEBUG] Re-stitch {headword}: Error check {e}")
                             pass
                    
                    if should_stitch:
                        stitched_img = self.stitcher.stitch(merged, doc, self.project_config.get('image_dir'), 
                                                            self.project_config.get('page_offset', 0), is_vertical)
                        
                        if stitched_img:
                            stitched_img.save(save_path)
                    
                    # 6. Link
                    rel_path = img_filename
                    entry['image_path'] = rel_path
                        
                # Update progress
                
            # Save Final Text File
            self.progress.emit("Saving index file...")
            
            project_name = self.project_config.get("name", "project")
            base_name = f"{project_name}_{self.side}.{self.fmt if self.fmt=='json' else 'txt'}"
            final_path = os.path.join(self.export_dir, base_name)
            
            with open(final_path, 'w', encoding='utf-8') as f:
                if self.fmt == 'json':
                    json.dump(self.entries, f, ensure_ascii=False, indent=2)
                elif self.fmt == 'mdx':
                    for e in self.entries:
                        f.write(f"{e['headword']}\n")
                        f.write(f"{e['text']}\n".replace('\n','<br>\n'))
                        if 'image_path' in e:
                            # MDX generic image tag
                            f.write(f'<img src="{e["image_path"]}" />\n')
                        f.write("</>\n")
            
            self.finished.emit(True, f"Export success! Saved to {final_path}")
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.finished.emit(False, str(e))
        finally:
            if doc: doc.close()

    def stop(self):
        self.is_running = False

# ==========================================
# 5. Export Logic (Generic Parser)
# ==========================================

class ExportParser:

    def __init__(self, pages_dict: dict, regex_str: str, group_id: int = 0):
        self.pages_dict = pages_dict # {page_num: text}
        self.group_id = group_id
        if regex_str:
            try:
                self.regex = re.compile(regex_str)
            except:
                self.regex = None
        else:
            self.regex = None
        
    def parse(self):
        """
        Returns list of entries:
        [
            {
                "headword": str,
                "text": str, # merged text
                "pages": [int],
                "page_index": int
            }, ...
        ]
        """
        entries = []
        if not self.pages_dict or not self.regex: 
            return entries
            
        sorted_pages = sorted(self.pages_dict.keys())
        current_entry = None
        
        # Buffer for text that appears before first headword on non-first page
        # This text belongs to the PREVIOUS entry (if exists)
        
        for page_num in sorted_pages:
            page_text = self.pages_dict[page_num]
            lines = page_text.split('\n')
            
            # Find all headwords in this page
            page_headword_indices = [] # list of (line_idx, match_obj)
            for i, line in enumerate(lines):
                m = self.regex.search(line)
                if m:
                    page_headword_indices.append((i, m))
                    
            if not page_headword_indices:
                # Whole page has no headword -> append to current entry
                if current_entry:
                    self._append_text_to_entry(current_entry, lines, page_num)
                # Else: Orphan text? (Before first entry of first page... ignore or new entry?)
                continue
                
            # Process segments
            prev_line_idx = 0
            
            # 1. Text BEFORE first headword on this page
            first_hw_line_idx = page_headword_indices[0][0]
            if first_hw_line_idx > 0:
                pre_text_lines = lines[0:first_hw_line_idx]
                if current_entry:
                    self._append_text_to_entry(current_entry, pre_text_lines, page_num)
                
            # 2. Iterate headwords
            for k, (line_idx, match) in enumerate(page_headword_indices):
                try:
                    headword = match.group(self.group_id)
                except IndexError:
                    headword = match.group(0)
                    
                # Content for this entry ranges from line_idx to next_headword_line_idx
                
                content_lines = []
                line_content = lines[line_idx]
                content_lines.append(line_content)
                
                # Content from next lines
                start_next = line_idx + 1
                end_next = len(lines) 
                
                if k < len(page_headword_indices) - 1:
                    end_next = page_headword_indices[k+1][0]
                    
                content_lines.extend(lines[start_next:end_next])
                
                current_entry = {
                    "headword": headword,
                    "text": "", 
                    "pages": [page_num], # Set
                    "page_index": k + 1
                }
                entries.append(current_entry)
                
                # Add content
                self._append_text_to_entry(current_entry, content_lines, page_num)
             
        return entries

    def _append_text_to_entry(self, entry, lines, page_num):
        # Filter empty lines
        valid_lines = [l.strip() for l in lines if l.strip()]
        if not valid_lines: return
        
        # Add page number if not present
        if page_num not in entry["pages"]:
            entry["pages"].append(page_num)
            
        text_chunk = "\n".join(valid_lines)
        
        # Merge logic
        if not entry["text"]:
            entry["text"] = text_chunk
        else:
            # Check previous text ending
            prev_text = entry["text"]
            if prev_text.endswith('-'):
                # Hyphen: Remove hyphen, join directly
                entry["text"] = prev_text[:-1] + text_chunk
            else:
                last_char = prev_text[-1]
                # CJK Check (Simple range)
                is_cjk = ('\u4e00' <= last_char <= '\u9fff')
                
                if is_cjk:
                    entry["text"] = prev_text + text_chunk
                else:
                    entry["text"] = prev_text + " " + text_chunk
                    

# ==========================================



# ==========================================
# 2.5 路径/头部组件 (Beyond Compare Style)
# ==========================================

class FileHeaderWidget(QWidget):
    """
    显示文件路径、浏览按钮、保存按钮
    """
    def __init__(self, parent_window, side="left"):
        super().__init__()
        self.main_window = parent_window
        self.side = side
        self.setFixedHeight(40)
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText(f"{side} data source path...")
        
        btn_browse = QPushButton("...")
        btn_browse.setFixedWidth(30)
        btn_browse.clicked.connect(self.browse_file)
        
        btn_save = QPushButton("Save")
        btn_save.clicked.connect(self.save_file)
        
        layout.addWidget(QLabel(f"{side.upper()}:"))
        layout.addWidget(self.path_edit)
        layout.addWidget(btn_browse)
        layout.addWidget(btn_save)
        
    def set_path(self, path):
        self.path_edit.setText(path)
        
    def browse_file(self):
        if not self.main_window.check_unsaved_changes():
            return
        filename, _ = QFileDialog.getOpenFileName(self, "Open File", "", "Text Files (*.txt);;All Files (*)")
        if filename:
            self.set_path(filename)
            # Update config and reload
            if self.side == "left":
                self.main_window.project_config['text_path_left'] = filename
            else:
                self.main_window.project_config['text_path_right'] = filename
            self.main_window.config_manager.save()
            self.main_window.reload_all_data()

    def save_file(self):
        if self.side == "left":
            self.main_window.save_left_data()
        elif self.side == "right":
            self.main_window.save_right_data()

# ==========================================
# 2.6 项目管理对话框
# ==========================================

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
        
        self.input_api_url = QLineEdit()
        self.input_api_token = QLineEdit()
        
        # Load values
        g = self.config_manager.get_global()
        self.input_api_url.setText(g.get("ocr_api_url", ""))
        self.input_api_token.setText(g.get("ocr_api_token", ""))
        
        # Connect save
        self.input_api_url.textChanged.connect(self.save_global)
        self.input_api_token.textChanged.connect(self.save_global)
        
        layout.addRow("OCR API URL:", self.input_api_url)
        layout.addRow("OCR API Token:", self.input_api_token)
        
    def save_global(self):
        g = self.config_manager.get_global()
        g["ocr_api_url"] = self.input_api_url.text()
        g["ocr_api_token"] = self.input_api_token.text()
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
             # Find item with new name? Or just use index?
             # Let's rely on index for stability if possible, but projects list might reorder?
             # List order is usually stable.
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
        # We need to know which project we are editing.
        # Use self.current_project_original_name as key
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
                # If this was active project, update active ref key (handled in manager? No, key string in ConfigManager needs update)
                # ConfigManager uses list of dicts. "Active Project" is a separate string key.
                # If we rename, we must update active_project string if it matches.
                if self.config_manager.data["active_project"] == self.current_project_original_name:
                    self.config_manager.data["active_project"] = new_name
                
                self.current_project_original_name = new_name
                # Refresh list to show new name (and keep selection)
                # Trigger refresh at end
                
        # 2. Save Fields
        p["pdf_path"] = self.inp_pdf.text()
        p["image_dir"] = self.inp_img_dir.text()
        p["text_path_left"] = self.inp_left_txt.text()
        p["text_path_right"] = self.inp_right_txt.text()
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
        
        # If renamed, refresh list items
        current_list_item = self.list_projects.currentItem()
        if current_list_item and current_list_item.text() != self.current_project_original_name:
             current_list_item.setText(self.current_project_original_name)

    def block_signals_inputs(self, block):
        inputs = [self.inp_pdf, self.inp_img_dir, self.inp_left_txt, self.inp_right_txt, 
                  self.inp_ocr_json, self.inp_export_dir, self.inp_reg_l, self.inp_reg_r, self.inp_name,
                  self.spin_start, self.spin_end, self.spin_offset,
                  self.spin_reg_grp_l, self.spin_reg_grp_r]
        for inp in inputs:
            inp.blockSignals(block)

    def add_project(self):
        name, ok = QInputDialog.getText(self, "New Project", "Project Name:")
        if ok and name:
            if self.config_manager.create_project(name):
                self.refresh_project_list()
                # Select new
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


# ==========================================
# 3. 主窗口
# ==========================================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("OCR 校对工具 v6")
        self.resize(1600, 900)
        
        self.resize(1600, 900)
        
        # self.config = DEFAULT_CONFIG.copy()
        # self.load_config()
        self.config_manager = ConfigManager()
        self.global_config = self.config_manager.get_global()
        self.project_config = self.config_manager.get_active_project()
        
        self.last_active_editor = None
        self._is_navigating_from_image = False
        self._is_loading = False
        self._is_program_scrolling = False # 防止滚动条死循环
        self._is_syncing_cursor = False    # 防止光标同步死循环
        
        # Compat: Map self.config to project_config for easy refactor, 
        # but manual access to global_config is needed for OCR.
        # Ideally, we replace all `self.config` with `self.project_config`
        # and `self.config.get('ocr_...')` with `self.global_config...`
        
        
        # 数据缓存
        self.pages_left = {}  # {page_num: text}
        self.pages_right_text = {} # {page_num: text} (Data Source 2)
        self.current_ocr_data = [] 
        
        self.doc = None # PDF Document
        
        # 脏标记 (Session Sets)
        self.dirty_pages_left = set()
        self.dirty_pages_right = set()
        self.dirty_pages_right = set()
        self._is_updating_diff = False # Recursion Guard
        
        # Global Undo Stack (for Find/Replace)
        self.global_undo_stack = [] 
        self.find_replace_dialog = None
        self.last_manual_edit_time = 0
        self.current_loaded_page = None # Track actual loaded page index
        
        # Preview Mode
        self.is_preview_mode = False
        self.preview_map_left = None
        self.preview_map_right = None
        # Regex for Markdown and HTML tags. 
        # <[^>]+> : HTML tags
        # \[.*?\]\(.*?\) : Markdown Links
        # [*_`#]+ : Markdown formatting chars
        # !\[.*?\]\(.*?\) : Images
        # Active Strippers (Initialized to Plain, updated by UI)
        self.stripper_l_active = TextStripper("plain")
        self.stripper_r_active = TextStripper("plain")
        self.text_stripper = self.stripper_l_active # Compatibility fallback for legacy calls
        
        # 初始化界面
        self.init_ui()
        
        # 加载数据
        self.reload_all_data()
        
    def closeEvent(self, event):
        """退出前检查未保存的更改"""
        if self.check_unsaved_changes():
            event.accept()
        else:
            event.ignore()
        
    def init_ui(self):
        # --- 工具栏 ---
        toolbar = QToolBar()
        self.addToolBar(toolbar)
        
        # Project Controls
        toolbar.addWidget(QLabel("Project: "))
        self.combo_project = QComboBox()
        self.combo_project.setMinimumWidth(150)
        self.update_project_combo()
        self.combo_project.activated.connect(self.on_project_switched)
        toolbar.addWidget(self.combo_project)

        # Menu Bar
        menubar = self.menuBar()
        edit_menu = menubar.addMenu("Edit")
        
        act_find = QAction("Find and Replace", self)
        act_find.setShortcut("Ctrl+F")
        act_find.triggered.connect(self.show_find_replace)
        edit_menu.addAction(act_find)
        
        act_undo_global = QAction("Undo Global Replace", self)
        act_undo_global.triggered.connect(self.undo_global)
        edit_menu.addAction(act_undo_global)
        
        btn_manage = QPushButton("Settings / Manage")
        btn_manage.clicked.connect(self.open_project_manager)
        toolbar.addWidget(btn_manage)
        
        toolbar.addSeparator()
        
        # 页码控制
        self.spin_page = QLineEdit()
        self.spin_page.setFixedWidth(50)
        self.spin_page.returnPressed.connect(self.jump_page)
        
        btn_prev = QPushButton("<"); btn_prev.setFixedWidth(30); btn_prev.clicked.connect(self.prev_page)
        btn_next = QPushButton(">"); btn_next.setFixedWidth(30); btn_next.clicked.connect(self.next_page)
        
        toolbar.addWidget(QLabel("页码: "))
        toolbar.addWidget(btn_prev)
        toolbar.addWidget(self.spin_page)
        toolbar.addWidget(btn_next)
        toolbar.addSeparator()
        
        # 数据源选择
        toolbar.addWidget(QLabel(" 右侧数据源: "))
        self.combo_source = QComboBox()
        self.combo_source.addItems(["Text File B", "OCR Results"])
        self.combo_source.currentIndexChanged.connect(lambda: self.load_current_page())
        toolbar.addWidget(self.combo_source)
        
        toolbar.addSeparator()
        
        
        # OCR 工具栏
        toolbar.addSeparator()
        toolbar.addSeparator()
        
        # Word Wrap Toggle
        self.cb_word_wrap = QCheckBox("Wrap")
        self.cb_word_wrap.setChecked(True) # Default On
        self.cb_word_wrap.toggled.connect(self.toggle_word_wrap)
        toolbar.addWidget(self.cb_word_wrap)
        
        # Preview Mode Toggle
        # Preview Mode Toggle (Button)
        self.btn_preview = QPushButton("Preview (Stripped)")
        self.btn_preview.setCheckable(True)
        self.btn_preview.toggled.connect(self.toggle_preview_mode)
        toolbar.addWidget(self.btn_preview)
        
        # Ignore Tags Toggle (Removed)
        # self.cb_ignore_tags = QCheckBox("Ignore Tags")
        # self.cb_ignore_tags.setChecked(False)
        # self.cb_ignore_tags.toggled.connect(self.run_diff)
        # toolbar.addWidget(self.cb_ignore_tags)
        
        toolbar.addSeparator()
        toolbar.addWidget(QLabel(" OCR Engine: "))
        self.combo_ocr_engine = QComboBox()
        self.combo_ocr_engine.addItem("Remote API", "remote")
        if HAS_LOCAL_OCR:
            self.combo_ocr_engine.addItem("PaddleOCR (Local)", "local")
            
        # Select current
        current_engine = self.global_config.get("ocr_engine", "remote")
        idx = self.combo_ocr_engine.findData(current_engine)
        if idx >= 0: self.combo_ocr_engine.setCurrentIndex(idx)
        
        self.combo_ocr_engine.currentIndexChanged.connect(self.on_ocr_engine_changed)
        toolbar.addWidget(self.combo_ocr_engine)
        
        btn_ocr_cur = QPushButton("OCR当前页面")
        btn_ocr_cur.clicked.connect(self.run_current_ocr_unified)
        toolbar.addWidget(btn_ocr_cur)
        
        self.btn_batch = QPushButton("OCR所有缺失页面")
        self.btn_batch.clicked.connect(self.run_batch_ocr)
        toolbar.addWidget(self.btn_batch)

        
        
        # Export Menu Button
        self.btn_export_menu = QPushButton("导出...")
        self.menu_export = QMenu(self)
        
        # Actions
        def add_action(text, func):
            a = QAction(text, self)
            a.triggered.connect(func)
            self.menu_export.addAction(a)
            
        add_action("导出当前页面切图", self.export_slices)
        add_action("导出当前页面OCR文本", self.export_ocr_dict_current)
        self.menu_export.addSeparator()
        add_action("导出所有页面OCR文本", self.export_all_ocr_txt)
        self.menu_export.addSeparator()
        add_action("导出左侧文本(.json)", lambda: self.export_parsed("left", "json"))
        add_action("导出右侧文本(.json)", lambda: self.export_parsed("right", "json"))
        add_action("导出左侧文本(.mdx.txt)", lambda: self.export_parsed("left", "mdx"))
        add_action("导出右侧文本(.mdx.txt)", lambda: self.export_parsed("right", "mdx"))
        self.menu_export.addSeparator()
        add_action("导出左侧文本(.json)+图片", lambda: self.export_parsed_with_images("left", "json"))
        add_action("导出右侧文本(.json)+图片", lambda: self.export_parsed_with_images("right", "json"))
        add_action("导出左侧文本(.mdx.txt)+图片", lambda: self.export_parsed_with_images("left", "mdx"))
        add_action("导出右侧文本(.mdx.txt)+图片", lambda: self.export_parsed_with_images("right", "mdx"))
        
        self.menu_export.addSeparator()
        self.action_force_recreate = QAction("强制重新生成图片", self, checkable=True)
        self.action_force_recreate.setChecked(False)
        self.menu_export.addAction(self.action_force_recreate)
        
        self.btn_export_menu.setMenu(self.menu_export)
        toolbar.addWidget(self.btn_export_menu)

        # --- 主布局 ---
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QHBoxLayout(main_widget)
        
        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter)
        
        # 1. 左侧：图片
        self.image_view = ImageCanvas()
        splitter.addWidget(self.image_view)
        
        # 2. 右侧：文本对比
        right_container = QWidget()
        right_layout = QVBoxLayout(right_container)
        right_layout.setContentsMargins(0,0,0,0)
        
        # 2.1 正则配置区 & Type Selectors (Combined)
        regex_layout = QHBoxLayout()
        
        # --- Left Controls ---
        # Type Selector Left
        regex_layout.addWidget(QLabel("L Type:"))
        self.combo_type_left = QComboBox()
        self.combo_type_left.addItems(["Plain Text", "Markdown", "HTML"])
        self.combo_type_left.setItemData(0, "plain")
        self.combo_type_left.setItemData(1, "markdown")
        self.combo_type_left.setItemData(2, "html")
        self.combo_type_left.currentIndexChanged.connect(self.on_type_changed)
        regex_layout.addWidget(self.combo_type_left)
        
        # Regex Left
        
        self.regex_input_left = QLineEdit()
        self.regex_input_left.setPlaceholderText("左侧词头正则")
        self.regex_input_left.setText(self.project_config.get("regex_left", ""))
        self.regex_input_left.editingFinished.connect(self.on_regex_changed)
        
        self.regex_input_right = QLineEdit()
        self.regex_input_right.setPlaceholderText("右侧词头正则")
        self.regex_input_right.setText(self.project_config.get("regex_right", ""))
        self.regex_input_right.editingFinished.connect(self.on_regex_changed)
        
        regex_layout.addWidget(QLabel("L正则:"))
        regex_layout.addWidget(self.regex_input_left)
        
        self.spin_reg_grp_l = QSpinBox()
        self.spin_reg_grp_l.setRange(0, 99)
        self.spin_reg_grp_l.setValue(self.project_config.get("regex_group_left", 0))
        self.spin_reg_grp_l.valueChanged.connect(self.on_regex_changed)
        regex_layout.addWidget(self.spin_reg_grp_l)
        
        # Spacer
        regex_layout.addSpacing(20)
        
        # --- Right Controls ---
        # Type Selector Right
        regex_layout.addWidget(QLabel("R Type:"))
        self.combo_type_right = QComboBox()
        self.combo_type_right.addItems(["Plain Text", "Markdown", "HTML"])
        self.combo_type_right.setItemData(0, "plain")
        self.combo_type_right.setItemData(1, "markdown")
        self.combo_type_right.setItemData(2, "html")
        self.combo_type_right.currentIndexChanged.connect(self.on_type_changed)
        regex_layout.addWidget(self.combo_type_right)
        
        regex_layout.addWidget(QLabel("R正则:"))
        regex_layout.addWidget(self.regex_input_right)
        
        self.spin_reg_grp_r = QSpinBox()
        self.spin_reg_grp_r.setRange(0, 99)
        self.spin_reg_grp_r.setValue(self.project_config.get("regex_group_right", 0))
        self.spin_reg_grp_r.valueChanged.connect(self.on_regex_changed)
        regex_layout.addWidget(self.spin_reg_grp_r)
        
        right_layout.addLayout(regex_layout)
        
        # 2.2 文本编辑器区域 (改为带 Header 的布局)
        text_splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # --- Left Side Container ---
        left_container = QWidget()
        left_box = QVBoxLayout(left_container)
        left_box.setContentsMargins(0,0,0,0)
        left_box.setSpacing(0)
        
        self.header_left = FileHeaderWidget(self, "left")
        
        # Type Selector Left (Moved to Top)
        # hl_l = QHBoxLayout() ... (Removed)
        # left_box.addLayout(hl_l)

        # Stack for Preview
        self.stack_left = QStackedWidget()
        
        self.edit_left = DiffTextEdit("left")
        self.stack_left.addWidget(self.edit_left)
        
        self.edit_left_preview = PreviewTextEdit("left") # Custom Rich Text Editor
        self.edit_left_preview.setReadOnly(False)
        self.edit_left_preview.setReadOnly(False) # Editable per user request
        self.edit_left_preview.setPlaceholderText("Preview Mode...")
        self.stack_left.addWidget(self.edit_left_preview)

        left_box.addWidget(self.header_left)
        left_box.addWidget(self.stack_left)
        
        # --- Right Side Container ---
        right_container_widget = QWidget() # Rename to avoid conflict with right_container (outer)
        right_box = QVBoxLayout(right_container_widget)
        right_box.setContentsMargins(0,0,0,0)
        right_box.setSpacing(0)
        
        self.header_right = FileHeaderWidget(self, "right")
        
        # Type Selector Right (Moved to Top)
        # hl_r = QHBoxLayout() ... (Removed)
        # right_box.addLayout(hl_r)
        
        # Stack for Preview
        self.stack_right = QStackedWidget()
        
        self.edit_right = DiffTextEdit("right")
        self.stack_right.addWidget(self.edit_right)
        
        self.edit_right_preview = PreviewTextEdit("right") # Custom Rich Text Editor
        self.edit_right_preview.setReadOnly(False)
        self.edit_right_preview.setReadOnly(False) # Editable per user request
        self.edit_right_preview.setPlaceholderText("Preview Mode...")
        self.stack_right.addWidget(self.edit_right_preview)
        
        right_box.addWidget(self.header_right)
        right_box.addWidget(self.stack_right)
        
        # 初始化 Highlighters (Only for Code Editors)
        self.highlighter_left = DiffSyntaxHighlighter(self.edit_left.document())
        self.highlighter_right = DiffSyntaxHighlighter(self.edit_right.document())
        # Preview uses separate highlighting logic (Rich Text Background)
        
        # 绑定信号
        self.edit_left.textChanged.connect(self.on_text_changed_left)
        self.edit_right.textChanged.connect(self.on_text_changed_right)
        
        # Preview Sync Signals
        self.edit_left_preview.textChanged.connect(self.on_preview_left_changed)
        self.edit_right_preview.textChanged.connect(self.on_preview_right_changed)
        
        # 绑定 Patch 信号
        self.edit_left.apply_patch_signal.connect(lambda r, t: self.apply_patch(self.edit_left, r, t))
        self.edit_right.apply_patch_signal.connect(lambda r, t: self.apply_patch(self.edit_right, r, t))

        # 绑定 Push Patch 信号 (Alt+Click) : 源自 Left -> 改 Right
        self.edit_left.push_patch_signal.connect(lambda r, t: self.apply_patch(self.edit_right, r, t))
        self.edit_right.push_patch_signal.connect(lambda r, t: self.apply_patch(self.edit_left, r, t))
        
        # 使用自定义的滚动监听，因为需要判断是否由用户触发
        self.edit_left.verticalScrollBar().valueChanged.connect(lambda v: self.on_scroll(self.edit_left, self.edit_right))
        self.edit_right.verticalScrollBar().valueChanged.connect(lambda v: self.on_scroll(self.edit_right, self.edit_left))
        
        # 绑定光标移动 (高亮对齐 & 自动滚动)
        # 绑定光标移动 (高亮对齐 & 自动滚动)
        self.edit_left.cursorPositionChanged.connect(self.on_cursor_left)
        self.edit_right.cursorPositionChanged.connect(self.on_cursor_right)

        # Preview Signals Connection
        self.edit_left_preview.verticalScrollBar().valueChanged.connect(lambda v: self.on_scroll(self.edit_left_preview, self.edit_right_preview))
        self.edit_right_preview.verticalScrollBar().valueChanged.connect(lambda v: self.on_scroll(self.edit_right_preview, self.edit_left_preview))
        
        # Preview Cursor (for BBox sync) - Simplified mapping for now
        # self.edit_left_preview.cursorPositionChanged.connect(self.on_preview_cursor_left) 
        
        # Preview Apply Patch
        self.edit_left_preview.apply_patch_signal.connect(lambda r, t: self.apply_patch(self.edit_left, r, t))
        self.edit_right_preview.apply_patch_signal.connect(lambda r, t: self.apply_patch(self.edit_right, r, t))
        
        # 绑定缩放 (Ctrl+Wheel)
        self.edit_left.zoom_signal.connect(self.on_zoom_request)
        self.edit_right.zoom_signal.connect(self.on_zoom_request)
        
        # 标记是否正在编程滚动，防止死循环
        self._is_program_scrolling = False
        
        # 进度条 (Added to Status Bar)
        # 进度条 (Added to Status Bar)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.statusBar().addPermanentWidget(self.progress_bar)

        # Finalize Layout
        text_splitter.addWidget(left_container)
        text_splitter.addWidget(right_container_widget)
        right_layout.addWidget(text_splitter)
        
        self.image_view.bbox_clicked.connect(self.on_image_bbox_click)
        self.edit_left.focus_in_signal.connect(self.on_editor_focus)
        self.edit_right.focus_in_signal.connect(self.on_editor_focus)

        splitter.addWidget(right_container)
        splitter.setSizes([600, 1000]) # 初始比例

    # ================= 逻辑处理 =================

    def reload_all_data(self):
        # Prevent auto-save of old content into new data
        self.current_loaded_page = None
        self.last_loaded_source = "Text File B" # Track source for saving
        self.current_ocr_data = None      # 1. 加载文本
        self.pages_left = read_text_to_pages(self.project_config['text_path_left'])
        self.pages_right_text = read_text_to_pages(self.project_config['text_path_right'])
        
        # 2. 加载 PDF
        self.doc = None
        if self.project_config['pdf_path'] and os.path.exists(self.project_config['pdf_path']):
            try:
                self.doc = fitz.open(self.project_config['pdf_path'])
            except:
                self.doc = None
        
        # 3. Update Headers
        self.header_left.set_path(self.project_config.get('text_path_left', ''))
        self.header_right.set_path(self.project_config.get('text_path_right', ''))

        self.dirty_pages_left.clear()
        self.dirty_pages_right.clear()
        self.load_current_page()

 

    def load_current_page(self):
        # Session-based dirty tracking: No prompts here.
        # Save previous page first
        self.save_current_page_data()

        self._is_loading = True
        try:
            page_num = self.project_config.get('start_page', 1)
            try:
                page_num = int(self.spin_page.text())
            except: pass
            
            # Update tracker
            self.current_loaded_page = page_num
            
            self.spin_page.setText(str(page_num))
        
            # 1. Load OCR Data
            ocr_data = self.load_ocr_json(page_num)
            self.current_ocr_data = ocr_data # Store for highlighting
            
            # 2. Load Image (High Res)
            if self.doc or self.project_config.get('image_dir'):
                # Check OCR status
                ocr_state = " (OCR Done)" if ocr_data else " (No OCR)"
                self.statusBar().showMessage(f"Page {page_num} Loaded{ocr_state}")
                
                pix = self.get_page_pixmap(page_num)
                if pix:
                    self.image_view.load_content(pix, ocr_data)
                else:
                    self.image_view.load_content(None)
            
            # 3. 构建 OCR 映射 (如果存在)
            self.ocr_text_full = ""
            self.ocr_char_map = [] # [(start, end, bbox), ...]

            if ocr_data:
                current_idx = 0
                for item in ocr_data:
                    text, bbox = "", []
                    if isinstance(item, dict):
                        text = item.get('text', '')
                        bbox = item.get('bbox', [])
                    elif isinstance(item, list) and len(item) == 2:
                        text = item[1][0]
                        # Parse Paddle points to rect
                        pts = item[0]
                        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
                        bbox = [min(xs), min(ys), max(xs), max(ys)]
                    
                    # Append with newline
                    chunk = text + "\n"
                    length = len(chunk)
                    # Map range [current, current+length) -> bbox
                    # bbox format for map: [x1, y1, x2, y2] usually
                    # We need x,y,w,h or x1,y1,x2,y2. Let's store [x,y,w,h] for easy use
                    if len(bbox) == 4:
                        # Check if it is x1,y1,x2,y2 (Paddle usually points.. but here normalized)
                        # My load_ocr_json returns [x,y,w,h] for dict? Let's check load_ocr_json
                        # Wait, load_ocr_json logic for layout parser returns [x,y,x2,y2]??
                        # Let's standardize bbox in process loop below
                        pass
                        
                    self.ocr_text_full += chunk
                    self.ocr_char_map.append({
                        'start_index': current_idx,
                        'end_index': current_idx + len(text), # exclude newline for click mapping
                        'bbox': bbox
                    })
                    current_idx += length
            
            is_ocr_mode = (self.combo_source.currentText() == "OCR Results")
            
            # Draw bboxes (Use red for all detected, or maybe lighter if not in OCR mode)
            #self.image_view.load_content(pix, ocr_data if ocr_data else [])
            
            # 3. 设置文本
            # 左侧
            left_text = self.pages_left.get(page_num, "")
            
            # 右侧
            right_text = ""
            if is_ocr_mode:
                # Simple join from full text (which includes newlines)
                right_text = self.ocr_text_full
            else:
                right_text = self.pages_right_text.get(page_num, "")

            # 避免触发 textChanged 导致死循环 (以及标记 modified)
            self.edit_left.blockSignals(True)
            self.edit_right.blockSignals(True)
            
            self.edit_left.setPlainText(left_text)
            self.edit_right.setPlainText(right_text)
            
            if self.is_preview_mode:
                 # Update strippers just in case (though usually fixed by mode toggle)
                 # But we need stripping for diff mapping? 
                 # Yes, DiffWorker uses active mode.
                 pass
                 
                 # Render Left
                 self.edit_left_preview.blockSignals(True)
                 mode_l = self.combo_type_left.currentText().lower()
                 if "markdown" in mode_l:
                     self.edit_left_preview.setMarkdown(left_text)
                 elif "html" in mode_l:
                     self.edit_left_preview.setHtml(left_text)
                 else:
                     self.edit_left_preview.setPlainText(left_text)
                 self.edit_left_preview.blockSignals(False)

                 # Render Right
                 self.edit_right_preview.blockSignals(True)
                 mode_r = self.combo_type_right.currentText().lower()
                 if "markdown" in mode_r:
                     self.edit_right_preview.setMarkdown(right_text)
                 elif "html" in mode_r:
                     self.edit_right_preview.setHtml(right_text)
                 else:
                     self.edit_right_preview.setPlainText(right_text)
                 self.edit_right_preview.blockSignals(False)
            
            self.edit_left.blockSignals(False)
            self.edit_right.blockSignals(False)
            
            # 重置脏标记
            self.modified_left = False
            self.modified_right = False
            
            # 4. 执行对比 (Init Timer first)
            if not hasattr(self, 'diff_timer'):
                self.init_diff_timer()
            
            # Regex (Update Highlighters with Group ID)
            self.highlighter_left.set_regex(self.project_config.get("regex_left"), self.project_config.get("regex_group_left", 0))
            self.highlighter_right.set_regex(self.project_config.get("regex_right"), self.project_config.get("regex_group_right", 0))

            # Force run immediately for first load? Or use deferred?
            # Use deferred to keep it async
            self.deferred_run_diff()
            
            # Record last loaded source
            self.last_loaded_source = "OCR Results" if is_ocr_mode else "Text File B"

        finally:
            self._is_loading = False

    def force_ui_reload(self):
        """Force reload current page texts from memory dicts"""
        self._is_loading = True
        try:
            p = self.current_loaded_page
            if p is None: return
            
            # Left
            text_l = self.pages_left.get(p, "")
            self.edit_left.setPlainText(text_l)
            
            # Right
            if self.combo_source.currentText() == "Text File B":
                text_r = self.pages_right_text.get(p, "")
                self.edit_right.setPlainText(text_r)
                
            if self.is_preview_mode:
                 text_l_curr = self.edit_left.toPlainText()
                 self.edit_left_preview.blockSignals(True)
                 mode_l = self.combo_type_left.currentText().lower()
                 if "markdown" in mode_l:
                      self.edit_left_preview.setMarkdown(text_l_curr)
                 elif "html" in mode_l:
                      self.edit_left_preview.setHtml(text_l_curr)
                 else:
                      self.edit_left_preview.setPlainText(text_l_curr)
                 self.edit_left_preview.blockSignals(False)
                 
                 text_r_curr = self.edit_right.toPlainText()
                 self.edit_right_preview.blockSignals(True)
                 mode_r = self.combo_type_right.currentText().lower()
                 if "markdown" in mode_r:
                      self.edit_right_preview.setMarkdown(text_r_curr)
                 elif "html" in mode_r:
                      self.edit_right_preview.setHtml(text_r_curr)
                 else:
                      self.edit_right_preview.setPlainText(text_r_curr)
                 self.edit_right_preview.blockSignals(False)
                
            # Trigger diff update
            self.deferred_run_diff()
        finally:
            self._is_loading = False


    def get_best_page_image_bytes(self, doc, page_num):
        """Extract High-Res or Raw image from PDF"""
        try:
             real_page_num = page_num + self.project_config.get('page_offset', 0)
             if 0 < real_page_num <= len(doc):
                 page = doc[real_page_num - 1]
                 
                 # 1. Try Extract Raw Image (scanned PDF)
                 try:
                     images = page.get_images()
                     if len(images) == 1:
                         xref = images[0][0]
                         base_image = doc.extract_image(xref)
                         return base_image["image"]
                 except: pass # some implementation might fail
                 
                 # 2. Fallback: High DPI Render
                 pix = page.get_pixmap(matrix=fitz.Matrix(3.0, 3.0))
                 return pix.tobytes("png")
        except: pass
        return None

    def get_page_pixmap(self, page_num):
        """Helper for image cropping etc (still needed?) -> Refactor to use get_best..."""
        if self.doc:
            b = self.get_best_page_image_bytes(self.doc, page_num)
            if b:
                img = QImage.fromData(b)
                return QPixmap.fromImage(img)
        img_dir = self.project_config['image_dir']
        if img_dir and os.path.exists(img_dir):
            real_page_num = page_num + self.project_config.get('page_offset', 0)
            # 尝试 page_1.jpg 或 1.jpg
            names = [f"page_{real_page_num}", f"{real_page_num}"]
            exts = [".jpg", ".png", ".jpeg"]
            for n in names:
                for e in exts:
                    p = os.path.join(img_dir, n + e)
                    if os.path.exists(p):
                        return QPixmap(p)
        return None

    def load_ocr_json(self, page_num):
        """加载 PaddleOCR 格式 JSON"""
        path = self.project_config['ocr_json_path']
        real_page_num = page_num + self.project_config.get('page_offset', 0)
        f_path = os.path.join(path, f"page_{real_page_num}.json")
        if not os.path.exists(f_path):
            # 尝试直接数字
            f_path = os.path.join(path, f"{real_page_num}.json")
        
        if os.path.exists(f_path):
            try:
                with open(f_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # 简单适配逻辑：
                    # 如果是标准 Paddle list: [[points, (text, conf)], ...]
                    # 如果是 layout parser: data['fullContent']... (需要解析)
                    
                    if isinstance(data, list):
                        return data
                    elif isinstance(data, dict) and ("fullContent" in data or "layoutParsingResults" in data):
                        # 简化解析 PaddleOCR VL
                        res = []
                        if "fullContent" in data:
                            data = data["fullContent"]
                        if "layoutParsingResults" in data:
                            data = data["layoutParsingResults"][0]
                        blocks = data.get("prunedResult", {}).get("parsing_res_list", [])
                        for b in blocks:
                            if b.get('block_label') in ['text','paragraph_title','vertical_text']:
                                res.append({
                                    'block_label': b.get('block_label'),
                                    'text': b.get('block_content'),
                                    'bbox': b.get('block_bbox')
                                })
                        return res
            except Exception as e:
                print(f"JSON Load error: {e}")
        return []

    # ================= Diff 核心 =================
    
    def run_ocr_mapping_diff(self, left_text):
        """计算 Left Text 到 OCR Full Text 的 Diff，用于坐标映射"""
        if not self.ocr_text_full: 
            self.ocr_diff_opcodes = []
            return
            
        matcher = difflib.SequenceMatcher(None, left_text, self.ocr_text_full, autojunk=False)
        self.ocr_diff_opcodes = matcher.get_opcodes()

    def init_diff_timer(self):
        self.diff_timer = QTimer(self)
        self.diff_timer.setSingleShot(True)
        self.diff_timer.setInterval(200) # 200ms debounce
        self.diff_timer.timeout.connect(self.run_diff_async)

    def run_diff(self):
        # Compatibility wrapper
        self.deferred_run_diff()

    def deferred_run_diff(self):
        if hasattr(self, 'diff_timer'):
            self.diff_timer.start()

    def run_diff_async(self):
        if self._is_updating_diff: return
        self._is_updating_diff = True
        
        text_l = self.edit_left.toPlainText()
        text_r = self.edit_right.toPlainText()
        
        need_ocr_map = (self.combo_source.currentText() != "OCR Results" and bool(self.ocr_text_full))
        
        # ignore_tags = self.cb_ignore_tags.isChecked() (Removed)
        
        # Get Modes from Combos
        # Get Modes from Combos
        # Map UI Text to internal mode string
        mode_map = {"Plain Text": "plain", "Markdown": "markdown", "HTML": "html"}
        mode_l = mode_map.get(self.combo_type_left.currentText(), "plain")
        mode_r = mode_map.get(self.combo_type_right.currentText(), "plain")
        
        # Default Ignore Tags Logic:
        # If either side is NOT plain text, we assume user wants to diff stripped content (Ignore Tags).
        # Unless user explicitly demanded plain diff? User said "ignore_tags should be default option... no need for separate option".
        ignore_tags = (mode_l != "plain" or mode_r != "plain")
        
        self.diff_worker = DiffWorker(text_l, text_r, self.ocr_text_full, need_ocr_map, ignore_tags=ignore_tags, mode_l=mode_l, mode_r=mode_r)
        self.diff_worker.result_ready.connect(self.on_diff_finished)
        self.diff_worker.finished.connect(self.on_diff_thread_finished)
        self.diff_worker.start()

    def on_diff_thread_finished(self):
        self._is_updating_diff = False
        self.diff_worker = None

    @pyqtSlot(list, list, list)
    def on_diff_finished(self, opcodes, ocr_opcodes, raw_opcodes):
        text_l = self.edit_left.toPlainText()
        text_r = self.edit_right.toPlainText()
        
        # 2. Preview Highlighting (If Active)
        if self.is_preview_mode:
            # Highlight Preview using helper (since QTextEdit doesn't have set_diff_data)
            self.edit_left_preview.blockSignals(True)
            self.edit_right_preview.blockSignals(True)
            
            self.highlight_preview(self.edit_left_preview, raw_opcodes, True)
            self.highlight_preview(self.edit_right_preview, raw_opcodes, False)
            
            self.edit_left_preview.blockSignals(False)
            self.edit_right_preview.blockSignals(False)
            
        # Highlight (Triggers repaint)
        self.edit_left.blockSignals(True)
        self.edit_right.blockSignals(True)
        self.highlighter_left.set_diff_data(opcodes, is_left=True)
        self.highlighter_right.set_diff_data(opcodes, is_left=False)
        
        # OCR Mapping
        if ocr_opcodes:
            self.ocr_diff_opcodes = ocr_opcodes
        else:
            if self.combo_source.currentText() == "OCR Results":
                 self.ocr_diff_opcodes = opcodes



    # ================= 交互 =================

    def toggle_word_wrap(self, checked):
        mode = QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere if checked else QTextOption.WrapMode.NoWrap
        self.edit_left.setWordWrapMode(mode)
        self.edit_right.setWordWrapMode(mode)
        # QPlainTextEdit standard: setLineWrapMode(QPlainTextEdit.LineWrapMode)
        mode_pte = QPlainTextEdit.LineWrapMode.WidgetWidth if checked else QPlainTextEdit.LineWrapMode.NoWrap
        self.edit_left.setLineWrapMode(mode_pte)
        self.edit_right.setLineWrapMode(mode_pte)

    def apply_patch(self, editor, rng, target_text):
        """应用 Diff 补丁：将 range 区间的内容替换为 target_text"""
        start_py, end_py = rng
        text = editor.toPlainText()
        
        start_qt = to_qt_pos(text, start_py)
        end_qt = to_qt_pos(text, end_py)
        
        cursor = editor.textCursor()
        cursor.setPosition(start_qt)
        cursor.setPosition(end_qt, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(target_text)
        # 插入后 textChanged 会触发，自动重新 diff

    def check_unsaved_changes(self):
        """
        检查未保存 (Exit Only). 如果有，弹窗提示。
        """
        if self.dirty_pages_left or self.dirty_pages_right:
            msg = "Unsaved changes in:\n"
            if self.dirty_pages_left: msg += "- Left Text\n"
            if self.dirty_pages_right: msg += "- Right Text\n"
            msg += "Do you want to save?"
            
            reply = QMessageBox.question(
                self, 
                "Unsaved Changes", 
                msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel
            )
            
            if reply == QMessageBox.StandardButton.Cancel:
                return False
            elif reply == QMessageBox.StandardButton.Yes:
                if self.dirty_pages_left: self.save_left_data()
                if self.dirty_pages_right: self.save_right_data()
                return True
        return True

    def mark_page_dirty(self, page_num, is_left):
        try: p = int(page_num)
        except: return
        
        if is_left:
            self.dirty_pages_left.add(p)
        else:
            self.dirty_pages_right.add(p)
            
    def update_memory_cache(self):
        """Update memory dicts from editors"""
        try:
            page_num = self.current_loaded_page
            # Logic handled in save_current_page_data mostly, but for live updates:
            self.pages_left[page_num] = self.edit_left.toPlainText()
            if self.combo_source.currentText() == "Text File B":
                 self.pages_right_text[page_num] = self.edit_right.toPlainText()
        except: pass

    def on_text_changed_left(self):
        if self._is_loading: return
        self.last_manual_edit_time = time.time()
        if not self._is_updating_diff:
            try:
                p = int(self.spin_page.text())
                self.dirty_pages_left.add(p)
            except: pass
            self.update_memory_cache()
        self.deferred_run_diff()
        
    def on_text_changed_right(self):
        if self._is_loading: return
        self.last_manual_edit_time = time.time()
        if not self._is_updating_diff:
            try:
                p = int(self.spin_page.text())
                self.dirty_pages_right.add(p)
            except: pass
            self.update_memory_cache()
        self.deferred_run_diff()

    def on_preview_left_changed(self):
             self.edit_left.blockSignals(False)
             # Update Map (Optimistic)
             _, self.preview_map_left, st_l = self.stripper_l_active.strip(new_orig)
             # Re-apply styles effectively? 
             # If user typed "a", it has no style.
             # Ideally we should re-highlight.
             # But modifying text in preview clears styles usually.
             # We should re-apply styles for the whole doc?
             # Yes, simplest way to keep it correct.
             self.edit_left_preview.blockSignals(True) # Prevent recursive loop
             # We need to save cursor pos
             cursor = self.edit_left_preview.textCursor()
             pos = cursor.position()
             # self.edit_left_preview.setPlainText( ... ) # Wait, we are editing IT. 
             # If we setPlainText, we interrupt editing.
             # Just apply styles?
             self.apply_styles_to_editor(self.edit_left_preview, st_l)
             cursor.setPosition(pos)
             self.edit_left_preview.setTextCursor(cursor)
             self.edit_left_preview.blockSignals(False)
             
             # Mark Dirty
             self.on_text_changed_left() 

    def on_preview_right_changed(self):
        if self._is_loading or not self.is_preview_mode: return
        # Sync Preview -> Source
        new_strip = self.edit_right_preview.toPlainText()
        orig = self.edit_right.toPlainText()
        new_orig = self.stripper_r_active.apply_stripped_diff(orig, new_strip, self.preview_map_right)
        
        if new_orig != orig:
             self.edit_right.blockSignals(True)
             self.edit_right.setPlainText(new_orig)
             self.edit_right.blockSignals(False)
             _, self.preview_map_right, st_r = self.stripper_r_active.strip(new_orig)
             
             self.edit_right_preview.blockSignals(True)
             cursor = self.edit_right_preview.textCursor()
             pos = cursor.position()
             self.apply_styles_to_editor(self.edit_right_preview, st_r)
             cursor.setPosition(pos)
             self.edit_right_preview.setTextCursor(cursor)
             self.edit_right_preview.blockSignals(False)
             
             self.on_text_changed_right()

    def on_regex_changed(self):
        self.project_config["regex_left"] = self.regex_input_left.text()
        self.project_config["regex_right"] = self.regex_input_right.text()
        self.project_config["regex_group_left"] = self.spin_reg_grp_l.value()
        self.project_config["regex_group_right"] = self.spin_reg_grp_r.value()
        self.config_manager.save()
        
        # Update Highlighters
        self.highlighter_left.set_regex(self.project_config["regex_left"], self.project_config["regex_group_left"])
        self.highlighter_right.set_regex(self.project_config["regex_right"], self.project_config["regex_group_right"])
        
        self.run_diff()

    def on_type_changed(self, index):
        # Trigger re-diff or re-preview
        if self.is_preview_mode:
            self.toggle_preview_mode(True) # Re-apply stripping with new mode
        else:
            self.run_diff()

    def on_zoom_request(self, delta):
        """Synchronized Font Zoom (Ctrl+Wheel)"""
        font = self.edit_left.font()
        size = font.pointSize()
        
        if delta > 0:
            size += 1
        else:
            size -= 1
            
        # Clamp
        size = max(6, min(size, 72))
        
        font.setPointSize(size)
        self.edit_left.setFont(font)
        self.edit_right.setFont(font)
        
        # Force line number update
        self.edit_left.update_line_number_area_width(0)
        self.edit_right.update_line_number_area_width(0)

    def toggle_word_wrap(self, checked):
        mode = QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere if checked else QTextOption.WrapMode.NoWrap
        self.edit_left.setWordWrapMode(mode)
        self.edit_right.setWordWrapMode(mode)
        # QPlainTextEdit standard: setLineWrapMode(QPlainTextEdit.LineWrapMode)
        mode_pte = QPlainTextEdit.LineWrapMode.WidgetWidth if checked else QPlainTextEdit.LineWrapMode.NoWrap
        self.edit_left.setLineWrapMode(mode_pte)
        self.edit_right.setLineWrapMode(mode_pte)


    # ================= 交互增强 (Sync & Highlight) =================

    def get_mapped_index(self, qt_idx, is_left_source):
        """获取索引映射 (Qt Index -> Qt Index)"""
        # 1. Convert Src Qt -> Src Py
        src_editor = self.edit_left if is_left_source else self.edit_right
        src_text = src_editor.toPlainText()
        py_idx = to_py_pos(src_text, qt_idx)
        
        opcodes = self.edit_left.diff_opcodes
        mapped_py_idx = -1
        
        for tag, i1, i2, j1, j2 in opcodes:
             # src range, dst range (Py indices)
             s1, s2 = (i1, i2) if is_left_source else (j1, j2)
             d1, d2 = (j1, j2) if is_left_source else (i1, i2)
             
             if s1 <= py_idx <= s2:
                 if tag == 'equal':
                     offset = py_idx - s1
                     mapped_py_idx = d1 + offset
                     if mapped_py_idx > d2: mapped_py_idx = d2
                 else:
                     mapped_py_idx = d1
                 break
        
        if mapped_py_idx == -1: return -1
        
        # 2. Convert Dst Py -> Dst Qt
        dst_editor = self.edit_right if is_left_source else self.edit_left
        dst_text = dst_editor.toPlainText()
        return to_qt_pos(dst_text, mapped_py_idx)

    def save_current_page_data(self):
        """Explicitly save editor content to memory dicts if changed"""
        if self.current_loaded_page is None: return
        
        # If Preview Mode, sync first
        if self.is_preview_mode:
            # Sync Left
            new_strip_l = self.edit_left_preview.toPlainText()
            orig_l = self.edit_left.toPlainText()
            # Use active stripper
            new_orig_l = self.stripper_l_active.apply_stripped_diff(orig_l, new_strip_l, self.preview_map_left)
            if new_orig_l != orig_l:
                 self.edit_left.setPlainText(new_orig_l)
                 _, self.preview_map_left = self.stripper_l_active.strip(new_orig_l)
                 
            # Sync Right
            new_strip_r = self.edit_right_preview.toPlainText()
            orig_r = self.edit_right.toPlainText()
            new_orig_r = self.stripper_r_active.apply_stripped_diff(orig_r, new_strip_r, self.preview_map_right)
            if new_orig_r != orig_r:
                 self.edit_right.setPlainText(new_orig_r)
                 _, self.preview_map_right = self.stripper_r_active.strip(new_orig_r)

        p = self.current_loaded_page
        
        # Left
        current_left = self.edit_left.toPlainText()
        saved_left = self.pages_left.get(p, "")
        if current_left != saved_left:
            self.pages_left[p] = current_left
            self.mark_page_dirty(p, True)
            
        # Right (Check Last Loaded Source!)
        if hasattr(self, 'last_loaded_source') and self.last_loaded_source == "Text File B":
            current_right = self.edit_right.toPlainText()
            saved_right = self.pages_right_text.get(p, "")
            if current_right != saved_right:
                 self.pages_right_text[p] = current_right
                 self.mark_page_dirty(p, False)
        elif not hasattr(self, 'last_loaded_source'):
            # Fallback for initialization or if variable missing
             if self.combo_source.currentText() == "Text File B": # This was the buggy line if switching!
                 # But if we are here, we might be switching.
                 # Safe default: Do NOT save if we are unsure? 
                 # Or save if combo says Text File B? 
                 # The bug was relying on combo text during switch.
                 # If we don't have last_loaded_source, we skip saving right to be safe?
                 pass 

    def on_scroll(self, source, target):
        """Percentage-based scroll sync"""
        if self._is_program_scrolling: return
        # Don't sync scroll if we are actively syncing cursor (which handles its own visibility)
        if hasattr(self, '_is_syncing_cursor') and self._is_syncing_cursor: return
        
        self._is_program_scrolling = True
        
        # Calculate ratio
        s_bar = source.verticalScrollBar()
        t_bar = target.verticalScrollBar()
        
        if s_bar.maximum() > 0:
            ratio = s_bar.value() / s_bar.maximum()
            t_val = int(ratio * t_bar.maximum())
            t_bar.setValue(t_val)
            
        self._is_program_scrolling = False

    def request_highlight_other(self, source_editor, idx):
        """根据当前光标位置，高亮另一侧对应位置"""
        # 1. 清除本侧高亮 (避免双方都有黄色条，只保留光标在当前侧，高亮在另一侧)
        source_editor.clear_highlight()
        
        # 2. 确定方向
        is_left_source = (source_editor == self.edit_left)
        target_editor = self.edit_right if is_left_source else self.edit_left
        
        mapped_idx = self.get_mapped_index(idx, is_left_source)
        
        if mapped_idx >= 0:
            target_editor.highlight_line_at_index(mapped_idx)
            
            # Ensure Visible
            cursor = target_editor.textCursor()
            cursor.setPosition(mapped_idx)
            
            # Check if visual rect is in viewport
            r = target_editor.cursorRect(cursor)
            viewport_rect = target_editor.viewport().rect()
            
            if not viewport_rect.contains(r):
                # Move actual cursor to center it
                target_editor.setTextCursor(cursor)
                target_editor.centerCursor()
        else:
            # 如果没找到映射（比如超出范围），也清除对面
            target_editor.clear_highlight()

    def check_auto_scroll_bbox(self, editor, idx):
        """检查是否需要自动滚动图片 (针对 PaddleOCR 结果)"""
        if not self.current_ocr_data: return
        
        # 如果是 Right Editor 且处于 OCR 模式，直接用行号 (旧逻辑保留，简单快速)
        is_ocr_mode = (self.combo_source.currentText() == "OCR Results")
        if editor == self.edit_right and is_ocr_mode:
            self._handle_right_editor_ocr_scroll(editor, idx)
            return

        # 如果是 Left Editor (或者 Right Editor 非 OCR 模式)
        # 使用 Diff Mapping 映射到 OCR Index
        
        target_ocr_idx = -1
        
        # 1. 确定 Mapping Source
        if editor == self.edit_left:
             # Use ocr_diff_opcodes (Left -> OCR)
             opcodes = getattr(self, 'ocr_diff_opcodes', [])
             
             # Convert Qt Index -> Py Index
             text = editor.toPlainText()
             src_py_idx = to_py_pos(text, idx)
             
             # Map src_py_idx to ocr_idx
             for tag, i1, i2, j1, j2 in opcodes:
                 if i1 <= src_py_idx <= i2:
                     if tag == 'equal':
                         target_ocr_idx = j1 + (src_py_idx - i1)
                         if target_ocr_idx > j2: target_ocr_idx = j2
                     else:
                         target_ocr_idx = j1
                     break
        
        # 2. Find BBox for target_ocr_idx
        if target_ocr_idx >= 0 and hasattr(self, 'ocr_char_map'):
            for mapping in self.ocr_char_map:
                # {start_index, end_index, bbox}
                # Use loose check: if index falls in line range
                if mapping['start_index'] <= target_ocr_idx <= mapping['end_index'] + 1: # +1 includes newline
                    bbox = mapping['bbox']
                    # standardize bbox to x,y,w,h
                    x, y, w, h = 0,0,0,0
                    if len(bbox) == 4:
                         x, y = bbox[0], bbox[1]
                         w, h = bbox[2]-bbox[0], bbox[3]-bbox[1]
                         
                    self.image_view.set_highlight_bbox(x, y, w, h)
                    return

    def _handle_right_editor_ocr_scroll(self, editor, idx):
        # New Logic: Char based mapping using to_py_pos and ocr_char_map
        text = editor.toPlainText()
        py_idx = to_py_pos(text, idx)
        
        if not hasattr(self, 'ocr_char_map'): return
        
        for mapping in self.ocr_char_map:
            if mapping['start_index'] <= py_idx <= mapping['end_index'] + 1:
                bbox = mapping['bbox']
                x, y, w, h = 0, 0, 0, 0
                if len(bbox) == 4:
                     x, y = bbox[0], bbox[1]
                     w, h = bbox[2]-bbox[0], bbox[3]-bbox[1]
                self.image_view.set_highlight_bbox(x, y, w, h)
                return

    def on_cursor_left(self):
        if self._is_syncing_cursor: return
        self._is_syncing_cursor = True
        try:
            idx = self.edit_left.textCursor().position()
            self.request_highlight_other(self.edit_left, idx)
            # 增加：检查左侧光标对应的 BBox
            if not self._is_navigating_from_image:
                 self.check_auto_scroll_bbox(self.edit_left, idx)
        finally:
            self._is_syncing_cursor = False

    def on_cursor_right(self):
        if self._is_syncing_cursor: return
        self._is_syncing_cursor = True
        try:
            idx = self.edit_right.textCursor().position()
            self.request_highlight_other(self.edit_right, idx)
            if not self._is_navigating_from_image:
                 self.check_auto_scroll_bbox(self.edit_right, idx)
        finally:
            self._is_syncing_cursor = False

    # ================= 功能 =================

    def prev_page(self):
        # No check needed
        try:
            p = int(self.spin_page.text())
            self.spin_page.setText(str(p - 1))
            self.load_current_page()
        except: pass

    def next_page(self):
        # No check needed
        try:
            p = int(self.spin_page.text())
            self.spin_page.setText(str(p + 1))
            self.load_current_page()
        except: pass
        
    def jump_page(self):
        # No check needed
        self.load_current_page()

    def save_left_data(self):
        path = self.project_config.get('text_path_left')
        if not path:
             # Provide Save As?
             path, _ = QFileDialog.getSaveFileName(self, "Save Left", "", "Text (*.txt)")
             if path: 
                 self.project_config['text_path_left'] = path
        
        if path:
            write_pages_to_file(self.pages_left, path)
            self.dirty_pages_left.clear()
            QMessageBox.information(self, "保存", f"Left data saved to {path}")
            self.config_manager.save()

    def save_right_data(self):
        if self.combo_source.currentText() != "Text File B":
            QMessageBox.warning(self, "Error", "Right side is not a text file.")
            return

        path = self.project_config.get('text_path_right')
        if not path:
             path, _ = QFileDialog.getSaveFileName(self, "Save Right", "", "Text (*.txt)")
             if path: 
                 self.project_config['text_path_right'] = path
        
        if path:
            write_pages_to_file(self.pages_right_text, path)
            self.dirty_pages_right.clear()
            QMessageBox.information(self, "保存", f"Right data saved to {path}")
            self.config_manager.save()

    def run_batch_ocr(self):
        """批量 OCR / Cancel"""
        # Toggle Logic: Cancel
        if hasattr(self, 'ocr_thread') and self.ocr_thread and self.ocr_thread.isRunning():
            self.ocr_thread.stop()
            self.btn_batch.setText("Stopping...")
            self.btn_batch.setEnabled(False)
            return

        # Check prereqs based on engine
        engine = self.global_config.get("ocr_engine", "remote")
        
        if engine == "remote":
            api_url = self.global_config.get("ocr_api_url")
            token = self.global_config.get("ocr_api_token")
            if not api_url or not token:
                QMessageBox.warning(self, "Config", "Missing URL/Token for Remote OCR")
                return
        elif engine == "local":
            if not HAS_LOCAL_OCR:
                QMessageBox.warning(self, "Config", "Local OCR module missing")
                return

            
        start = self.project_config.get("start_page", 1)
        end = self.project_config.get("end_page", 100)
        
        missing_pages = []
        save_dir = self.project_config.get("ocr_json_path", "ocr_results")
        
        for p in range(start, end + 1):
            real_page_num = p + self.project_config.get("page_offset", 0)
            if not os.path.exists(os.path.join(save_dir, f"page_{real_page_num}.json")):
                missing_pages.append(p)
                
        if not missing_pages:
            QMessageBox.information(self, "Info", "No missing OCR pages found.")
            return
            
        # Direct Start with Cancel Option
        self.btn_batch.setText("Cancel OCR")
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, len(missing_pages))
        self.progress_bar.setValue(0)
        
        self.start_ocr_thread('batch', missing_pages)

    def start_ocr_thread(self, mode, pages):
        # OCRWorker(mode, pages, project_config, global_config, engine, pdf_path)
        engine = self.global_config.get("ocr_engine", "remote")
        
        # Ensure pdf path is passed properly or handled in thread
        pdf_path = self.project_config.get('pdf_path')
        
        worker = OCRWorker(mode, pages, self.project_config, self.global_config, engine)
        worker.project_name = self.project_config.get("name") # Tag with project name
        worker.progress.connect(self.on_ocr_progress)
        worker.finished.connect(self.on_ocr_finished)
        
        self.ocr_thread = worker
        self.ocr_thread.start()

    def on_ocr_progress(self, msg):
        worker = self.sender()
        if not worker: return
        
        # Display global progress with project context
        proj_name = getattr(worker, 'project_name', 'Unknown')
        self.statusBar().showMessage(f"[{proj_name}] {msg}")
        
        # Update progress bar for any batch job
        if worker.mode == 'batch':
             val = self.progress_bar.value()
             self.progress_bar.setValue(val + 1)

    def on_ocr_finished(self, success, msg):
        worker = self.sender()
        if not worker: return
        
        is_current_project = (getattr(worker, 'project_name', None) == self.project_config.get("name"))

        if is_current_project:
            QApplication.restoreOverrideCursor()
            self.statusBar().showMessage(msg, 5000)
            
            # Reset Batch UI
            if hasattr(self, 'btn_batch'):
                self.btn_batch.setText("OCR所有缺失页面")
                self.btn_batch.setEnabled(True)
            self.progress_bar.setVisible(False)
        else:
            # Background completion for other project
            print(f"Background OCR finished for {getattr(worker, 'project_name', 'Unknown')}")
            return # Do not update UI
        
        if success:
            if worker.mode == 'single':
                # Reload current page
                self.combo_source.setCurrentText("OCR Results")
                self.load_current_page()
            else:
                QMessageBox.information(self, "Batch Done", msg)
        else:
            if "Program interrupted" not in msg: # Don't error on manual stop
                 if is_current_project:
                    QMessageBox.critical(self, "OCR Failed", msg)
        
        if self.ocr_thread == worker:
            self.ocr_thread = None

    def export_slices(self):
        """如果当前是 OCR 模式且有 BBox 数据，则切割"""
        if self.combo_source.currentText() != "OCR Results" or not self.current_ocr_data:
            QMessageBox.warning(self, "Warning", "当前不是 OCR 模式或没有 OCR 数据，无法切割。")
            return
            
        out_dir = "output_slices"
        if not os.path.exists(out_dir): os.makedirs(out_dir)
        
        page_num = self.spin_page.text()
        pix = self.get_page_pixmap(int(page_num))
        if not pix: return
        
        img = pix.toImage()
        
        count = 0
        for i, item in enumerate(self.current_ocr_data):
            # 获取 bbox
            x, y, w, h = 0, 0, 0, 0
            if isinstance(item, dict):
                b = item['bbox']
                x, y, w, h = b[0], b[1], b[2]-b[0], b[3]-b[1]
            elif isinstance(item, list):
                pts = item[0]
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                x, y = min(xs), min(ys)
                w, h = max(xs)-x, max(ys)-y
            
            # 切割
            rect = img.copy(int(x), int(y), int(w), int(h))
            rect.save(os.path.join(out_dir, f"{page_num}_{i}.jpg"))
            count += 1
            
        QMessageBox.information(self, "Export", f"已导出 {count} 张切片到 {out_dir}")

    def export_ocr_txt(self):
        """Export OCR text to TXT"""
        if not self.ocr_text_full:
             QMessageBox.warning(self, "Export", "No OCR text available.")
             return
        
        filename, _ = QFileDialog.getSaveFileName(self, "Export OCR Text", "ocr_export.txt", "Text Files (*.txt)")
        if filename:
            try:
                with open(filename, 'w', encoding='utf-8') as f:
                    f.write(self.ocr_text_full)
                QMessageBox.information(self, "Export", f"Saved to {filename}")
            except Exception as e:
                QMessageBox.critical(self, "Error", str(e))

    def export_ocr_dict_current(self):
        # Alias for old export_ocr_txt but explicit
        self.export_ocr_txt()

    def export_all_ocr_txt(self):
        # Dump all available OCR jsons to single text? Or per page?
        # Usually user wants all text concatenated.
        save_dir = self.project_config.get("ocr_json_path", "ocr_results")
        start = self.project_config.get("start_page", 1)
        end = self.project_config.get("end_page", 100)
        
        full_text = ""
        for p in range(start, end + 1):
             real_p = p + self.project_config.get("page_offset", 0)
             # Reuse load_ocr_json logic? But that returns list. 
             # We need generic method to get text from page.
             # load_current_page logic duplicates this.
             # Let's simple load
             data = self.load_ocr_json(p) # Takes user page num
             if data:
                 t = self._extract_text_from_ocr_data(data)
                 full_text += f"<{p}>\n{t}\n"
                 
        filename, _ = QFileDialog.getSaveFileName(self, "Export All OCR", f"{self.project_config.get('project_name','all')}_ocr.txt", "Text Files (*.txt)")
        if filename:
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(full_text)
            QMessageBox.information(self, "Done", "Exported all OCR text.")

    def _extract_text_from_ocr_data(self, ocr_data):
        # Helper to join text from list
        txt = []
        for item in ocr_data:
             if isinstance(item, dict): txt.append(item.get('text', ''))
             elif isinstance(item, list): txt.append(item[1][0])
        return "\n".join(txt)
        
    def export_parsed(self, side, fmt):
        """Export parsed JSON/MDX"""
        # 1. Get Pages Dict
        pages = self.pages_left if side == 'left' else self.pages_right_text
        if not pages:
            QMessageBox.warning(self, "Error", f"No data for {side} side.")
            return
            
        # 2. Get Regex
        reg = self.project_config.get("regex_left" if side == 'left' else "regex_right")
        if not reg:
            QMessageBox.warning(self, "Error", f"No regex configured for {side} side.")
            return
            
        grp = self.project_config.get("regex_group_left" if side == 'left' else "regex_group_right", 0)
            
        # 3. Parse
        try:
            parser = ExportParser(pages, reg, grp)
            entries = parser.parse()
            
            if not entries:
                QMessageBox.warning(self, "Result", "No entries parsing found (check regex).")
                return
                
                
            # 4. Save
            project_name = self.project_config.get("name", "project")
            base_name = f"{project_name}_{side}.{fmt if fmt=='json' else 'txt'}"
            
            export_dir = self.project_config.get("export_dir")
            filename = ""
            
            if export_dir and os.path.exists(export_dir):
                filename = os.path.join(export_dir, base_name)
                if not self._confirm_overwrite_if_exists(filename): return
            else:
                filename, _ = QFileDialog.getSaveFileName(self, f"Export {side.upper()} {fmt.upper()}", base_name, 
                                                          f"{fmt.upper()} (*.{fmt if fmt=='json' else 'txt'})")
            
            if not filename: return

            with open(filename, 'w', encoding='utf-8') as f:
                if fmt == 'json':
                    json.dump(entries, f, ensure_ascii=False, indent=2)
                elif fmt == 'mdx':
                    for e in entries:
                        f.write(f"{e['headword']}\n")
                        f.write(f"{e['text']}\n".replace('\n','<br>\n'))
                        f.write("</>\n")
                        
            QMessageBox.information(self, "Success", f"Exported {len(entries)} entries to {filename}")

        except Exception as e:
            import traceback
            traceback.print_exc()
            QMessageBox.critical(self, "Error", str(e))

    def export_parsed_with_images(self, side, fmt):
        """Export parsed content AND generate stitched images"""
        
        # 1. Output Dir Check
        export_dir = self.project_config.get("export_dir")
        if not export_dir or not os.path.exists(export_dir):
            export_dir = QFileDialog.getExistingDirectory(self, "Select Export Directory")
            if not export_dir: return
            
            # Update config if new selection
            self.project_config["export_dir"] = export_dir
            self.config_manager.save()
            
        force_overwrite = self.action_force_recreate.isChecked()
        
        # 2. Get Data
        pages = self.pages_left if side == 'left' else self.pages_right_text
        if not pages:
            QMessageBox.warning(self, "Error", f"No data for {side} side.")
            return

        reg = self.project_config.get("regex_left" if side == 'left' else "regex_right")
        if not reg:
            QMessageBox.warning(self, "Error", f"No regex configured for {side} side.")
            return
            
        grp = self.project_config.get("regex_group_left" if side == 'left' else "regex_group_right", 0)

        # 3. Parse entries first (Synchronous, fast enough usually)
        try:
            parser = ExportParser(pages, reg, grp)
            entries = parser.parse()
            if not entries:
                QMessageBox.warning(self, "Result", "No entries found.")
                return
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Parse failed: {e}")
            return

        # 4. Start Background Worker
        self.img_export_worker = ImageExportWorker(entries, self.project_config, fmt, export_dir, side, force_overwrite)
        
        # Progress Dialog
        from PyQt6.QtWidgets import QProgressDialog
        self.progress_dlg = QProgressDialog("Exporting Images...", "Cancel", 0, len(entries), self)
        self.progress_dlg.setWindowTitle("Exporting")
        self.progress_dlg.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress_dlg.setMinimumDuration(0)
        self.progress_dlg.show()
        
        self.img_export_worker.progress.connect(self.progress_dlg.setLabelText)
        self.img_export_worker.progress_val.connect(self.progress_dlg.setValue)
        self.img_export_worker.finished.connect(self.on_img_export_finished)
        self.progress_dlg.canceled.connect(self.img_export_worker.stop) 
        
        self.img_export_worker.start()

    def on_img_export_finished(self, success, msg):
        self.progress_dlg.close()
        if success:
            QMessageBox.information(self, "Export Success", msg)
        else:
            QMessageBox.critical(self, "Export Failed", msg)
        self.img_export_worker = None

    def _confirm_overwrite_if_exists(self, filename):
        if os.path.exists(filename):
             ret = QMessageBox.question(self, "Overwrite", f"File exists:\n{filename}\nOverwrite?", 
                                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
             return ret == QMessageBox.StandardButton.Yes
        return True
            


    def on_ocr_engine_changed(self):
        engine = self.combo_ocr_engine.currentData()
        self.global_config["ocr_engine"] = engine
        self.config_manager.save()

    def run_current_ocr_unified(self):
        """Unified entry point for Single Page OCR"""
        try:
            page_num = int(self.spin_page.text())
        except: return
        
        # Disable button?
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self.start_ocr_thread('single', [page_num])

    def update_project_combo(self):
        self.combo_project.blockSignals(True)
        self.combo_project.clear()
        projects = self.config_manager.get_projects()
        active = self.config_manager.get_active_project()
        
        idx = 0
        for i, p in enumerate(projects):
            self.combo_project.addItem(p["name"])
            if p["name"] == active["name"]:
                idx = i
        self.combo_project.setCurrentIndex(idx)
        self.combo_project.blockSignals(False)

    def on_project_switched(self):
        if not self.check_unsaved_changes():
            # Revert combo box
            old_name = self.project_config["name"]
            idx = self.combo_project.findText(old_name)
            if idx >= 0: self.combo_project.setCurrentIndex(idx)
            return

        name = self.combo_project.currentText()
        self.config_manager.set_active_project(name)
        self.project_config = self.config_manager.get_active_project()
        
        # Cleanup Old Project State
        if self.find_replace_dialog:
            self.find_replace_dialog.reset_review_ui()
            
        # Reset View to Start Page
        start_p = self.project_config.get('start_page', 1)
        self.spin_page.setText(str(start_p))
        
        # Reload
        self.reload_all_data()
        QMessageBox.information(self, "Project Switched", f"Switched to {name}")

    def open_project_manager(self):
        if not self.check_unsaved_changes(): return
        dlg = ProjectManagerDialog(self, self.config_manager)
        dlg.exec()
        
        # After close: Config might have changed
        self.global_config = self.config_manager.get_global()
        self.project_config = self.config_manager.get_active_project()
        
        self.update_project_combo()
        self.reload_all_data()

    def on_editor_focus(self):
        self.last_active_editor = self.sender()
        
    def on_image_bbox_click(self, ocr_idx):
        """Handle Shift+Click on Image BBox"""
        if not hasattr(self, 'ocr_char_map') or not self.ocr_char_map: return
        
        # Get Py Index from ocr_idx
        # ocr_char_map[ocr_idx] corresponds to the block at index ocr_idx?
        # bbox_clicked emits index in ocr_data list.
        # But ocr_char_map is flattened per char. 
        # Wait, I need to map `item index` to `char index`.
        
        # Re-check logic: 
        # load_current_page builds ocr_char_map alongside ocr_text_full.
        # ocr_char_map is a list of dicts.
        # But `i` in draw_bboxes is index in `ocr_data`.
        # `ocr_data` is list of blocks.
        # So I need to find the char range for block `i`.
        
        # Since ocr_char_map is flattened characters? No.
        # Let's check `load_current_page` logic again.
        # Line 1337: `for item in ocr_data:`
        # Line 1363: `self.ocr_char_map.append({...})`
        # So `ocr_char_map` has SAME length as `ocr_data`. 
        # It maps Block -> BBox/Indices. (It's not char map, it's Block Map!)
        # So `ocr_idx` from click IS index in `ocr_char_map`.
        
        if ocr_idx < 0 or ocr_idx >= len(self.ocr_char_map): return
        
        data = self.ocr_char_map[ocr_idx]
        start_py_idx = data['start_index']
        
        # Determine target
        target = self.last_active_editor
        if not target: target = self.edit_left # Default
        
        target_pos = -1
        
        if target == self.edit_right and self.combo_source.currentText() == "OCR Results":
             # OCR Mode: Right IS OCR.
             target_pos = to_qt_pos(self.edit_right.toPlainText(), start_py_idx)
        else:
             # Need Mapping: OCR -> Left
             # opcodes: Left <-> OCR
             opcodes = getattr(self, 'ocr_diff_opcodes', [])
             if not opcodes: return
             
             # Locate Left Index corresponding to OCR Index `start_py_idx`
             # OCR is "right" side in ocr_diff_opcodes (j1, j2)
             left_py_idx = -1
             for tag, i1, i2, j1, j2 in opcodes:
                 if j1 <= start_py_idx <= j2:
                     if tag == 'equal':
                         left_py_idx = i1 + (start_py_idx - j1)
                     else:
                         left_py_idx = i1 # Approximation for changed block
                     break
             
             if left_py_idx != -1:
                 if target == self.edit_left:
                     text = self.edit_left.toPlainText()
                     target_pos = to_qt_pos(text, left_py_idx)
                 else:
                     # Target is Right (Text B)
                     # Map Left -> Right
                     right_qt_pos = self.get_mapped_index(to_qt_pos(self.edit_left.toPlainText(), left_py_idx), True)
                     target_pos = right_qt_pos
        
        if target_pos != -1:
            self._is_navigating_from_image = True
            try:
                cursor = target.textCursor()
                cursor.setPosition(target_pos)
                target.setTextCursor(cursor)
                target.ensureCursorVisible()
                target.setFocus() # Bring focus back to text
            finally:
                self._is_navigating_from_image = False

    # ================= Find / Replace / Global Undo =================

    def reload_displayed_texts(self):
        self.load_current_page()

    def show_find_replace(self):
        if not self.find_replace_dialog:
            self.find_replace_dialog = FindReplaceDialog(self)
            
        # Auto-focus correct scope
        if hasattr(self, 'last_active_editor') and self.last_active_editor == self.edit_left:
            self.find_replace_dialog.rb_left.setChecked(True)
            self.find_replace_dialog.rb_right.setChecked(False)
        else:
             # Default to Right (or whatever last active)
             self.find_replace_dialog.rb_right.setChecked(True)
             self.find_replace_dialog.rb_left.setChecked(False)
             
        self.find_replace_dialog.show()
        self.find_replace_dialog.raise_()
        self.find_replace_dialog.activateWindow()

    def push_global_undo(self, description="Global Action"):
        import copy
        snapshot = {
            'time': time.time(),
            'desc': description,
            'left': copy.deepcopy(self.pages_left),
            'right': copy.deepcopy(self.pages_right_text)
        }
        self.global_undo_stack.append(snapshot)
        if len(self.global_undo_stack) > 10:
            self.global_undo_stack.pop(0)
            
    def undo_global(self):
        if not self.global_undo_stack:
            QMessageBox.information(self, "Undo", "Nothing to undo.")
            return
            
        last_snapshot = self.global_undo_stack[-1]
        if self.last_manual_edit_time > last_snapshot['time']:
            ret = QMessageBox.warning(self, "Undo Warning", 
                "You have manually edited text since the last global replace.\n"
                "Undoing will OVERWRITE your manual edits.\n\n"
                "Are you sure you want to undo?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ret == QMessageBox.StandardButton.No: return
        
        snapshot = self.global_undo_stack.pop()
        self.pages_left = snapshot['left']
        self.pages_right_text = snapshot['right']
        
        self.reload_displayed_texts()
        # self.status_label is not mainwindow status bar? 
        # MainWindow usually has self.statusBar().
        self.statusBar().showMessage(f"Undone: {snapshot['desc']}", 5000)
        QMessageBox.information(self, "Undo", f"Reverted: {snapshot['desc']}")

    def toggle_preview_mode(self, checked):
        self.is_preview_mode = checked
        if checked:
            self.btn_preview.setText("Exit Preview")
        else:
            self.btn_preview.setText("Preview (Stripped)")
        
        if checked:
            # 1. Update Active Strippers from Combos
            mode_l = self.combo_type_left.currentData()
            # If "Plain Text" -> "plain". If "Markdown" -> "markdown"
            # Mapping logic might be needed if combo data is not set correctly or text used.
            # In init_ui: setItemData(0, "plain")...
            # So currentData() should be correct.
            self.stripper_l_active = TextStripper(mode=mode_l)
            
            mode_r = self.combo_type_right.currentData()
            self.stripper_r_active = TextStripper(mode=mode_r)
            
            # 2. Source -> Preview (Left)
            # Use ORIGINAL text for rendering to get formatting.
            text_l = self.edit_left.toPlainText()
            
            # We still need stripper to map diffs later? 
            # Yes, DiffWorker uses Stripper. 
            # Preview needs to display Formatted Text.
            # And we need to map Stripped Indices (from Diff) to Formatted Text indices.
            # Assumption: stripped text content == formatted text content.
            
            # Calculate strips for internal state (sync)
            s_l, m_l, st_l = self.stripper_l_active.strip(text_l)
            self.preview_map_left = m_l
            
            self.edit_left_preview.blockSignals(True)
            self.edit_left_preview.clear()
            if mode_l == "markdown":
                 self.edit_left_preview.setMarkdown(text_l)
            elif mode_l == "html":
                 self.edit_left_preview.setHtml(text_l)
            else:
                 self.edit_left_preview.setPlainText(s_l) # Plain: show stripped (or original?)
                 # Usually plain == stripped.
            self.edit_left_preview.blockSignals(False)
            
            self.stack_left.setCurrentIndex(1)
            
            # 3. Source -> Preview (Right)
            text_r = self.edit_right.toPlainText()
            s_r, m_r, st_r = self.stripper_r_active.strip(text_r)
            self.preview_map_right = m_r
            
            self.edit_right_preview.blockSignals(True)
            self.edit_right_preview.clear()
            if mode_r == "markdown":
                 self.edit_right_preview.setMarkdown(text_r)
            elif mode_r == "html":
                 self.edit_right_preview.setHtml(text_r)
            else:
                 self.edit_right_preview.setPlainText(s_r)
            self.edit_right_preview.blockSignals(False)
                 
            self.stack_right.setCurrentIndex(1)
            
            # Trigger Diff Refresh to Highlight Preview
            self.run_diff()
            
        else:
            # Preview -> Source
            # Sync Logic REMOVED per user request/issue.
            # Preview is ReadOnly, so no changes can propagate.
            # Programmatic updates to Preview cause Format Changes which break Original Data if synced back.
            
            self.stack_left.setCurrentIndex(0)
            self.stack_right.setCurrentIndex(0)

    def highlight_preview(self, editor, opcodes, is_left):
        """Highlight QTextEdit (Rich Text) based on opcodes"""
        doc = editor.document()
        cursor = QTextCursor(doc)
        
        # Clear existing background (Optimistic)
        # We assume re-rendering clears it? 
        # Yes, setHtml clears document.
        
        green_bg = QColor("#E6FFEC")
        red_bg = QColor("#FFEBE9")
        
        text = editor.toPlainText() # Plain text representation of HTML
        
        for tag, i1, i2, j1, j2 in opcodes:
            if tag == 'equal': continue
            
            s, e = (i1, i2) if is_left else (j1, j2)
            
            # Map valid range
            if s >= len(text): continue
            
            # Convert python index to qt index (if needed for utf-16, depends on python build)
            # Usually strict index mapping works if text matches.
            
            cursor.setPosition(s)
            cursor.setPosition(min(e, len(text)), QTextCursor.MoveMode.KeepAnchor)
            
            fmt = QTextCharFormat()
            if tag == 'replace':
                fmt.setBackground(red_bg if is_left else green_bg)
            elif tag == 'delete':
                fmt.setBackground(red_bg)
            elif tag == 'insert':
                fmt.setBackground(green_bg)
                
            cursor.mergeCharFormat(fmt)

    def apply_styles_to_editor(self, editor, styles):
        """Apply formatting to QPlainTextEdit based on styles list"""
        if not styles: return
        
        doc = editor.document()
        cursor = QTextCursor(doc)
        
        # Reset format? Actually easier to assume plain if we just setPlainText
        
        for start, length, style_type in styles:
            cursor.setPosition(start)
            cursor.setPosition(start + length, QTextCursor.MoveMode.KeepAnchor)
            
            fmt = QTextCharFormat()
            if style_type == 'bold':
                fmt.setFontWeight(QFont.Weight.Bold)
            elif style_type == 'italic':
                fmt.setFontItalic(True)
            elif style_type.startswith('header'):
                fmt.setFontWeight(QFont.Weight.Bold)
                # fmt.setForeground(QColor("blue")) # Optional
                
            cursor.mergeCharFormat(fmt)

    # ================= Preview Sync =================
    def on_preview_left_changed(self):
        """Sync Preview Edits -> Source (Left)"""
        # Only if NOT blocking signals (User Edit)
        mode = self.combo_type_left.currentText().lower()
        new_text = ""
        
        if "markdown" in mode:
            new_text = self.edit_left_preview.toMarkdown()
        elif "html" in mode:
            new_text = self.edit_left_preview.toHtml()
        else:
            new_text = self.edit_left_preview.toPlainText()
            
        # Update Source
        self.edit_left.blockSignals(True) # Prevent cycle
        self.edit_left.setPlainText(new_text)
        self.edit_left.blockSignals(False)
        
        self.deferred_run_diff()

    def on_preview_right_changed(self):
        """Sync Preview Edits -> Source (Right)"""
        mode = self.combo_type_right.currentText().lower()
        new_text = ""
        
        if "markdown" in mode:
            new_text = self.edit_right_preview.toMarkdown()
        elif "html" in mode:
            new_text = self.edit_right_preview.toHtml()
        else:
            new_text = self.edit_right_preview.toPlainText()
            
        # Update Source
        self.edit_right.blockSignals(True)
        self.edit_right.setPlainText(new_text)
        self.edit_right.blockSignals(False)
        
        self.deferred_run_diff()

    def on_preview_cursor_left(self):
        """Sync Preview Cursor -> Image & Other Side"""
        if self._is_program_scrolling: return
        
        # 1. Image Highlight using Source Editor Helper
        # We need mapping from Preview Index -> Source Index
        # For plain text mapping, it's 1:1. Rich text: approximate.
        # We will map Preview Cursor -> Source Cursor -> Trigger logic
        
        cursor = self.edit_left_preview.textCursor()
        idx_preview = cursor.position()
        
        # Simplified: Assume idx matches Source
        # Trigger Image Highlight by calling logic from on_cursor_left but with overridden index?
        # on_cursor_left reads self.edit_left.textCursor().
        
        # Strategy:
        # 1. Find BBox for idx_preview
        # 2. Highlight Image
        
        if self.current_ocr_data:
             # Logic from on_cursor_left duplicated/adapted
             bboxes = []
             # Find bbox containing idx_preview
             # This depends on OCR data structure (list of dicts with 'text', 'bbox'?)
             # self.current_ocr_data is list of {text, box} usually?
             # Actually on_cursor_left logic is complex (uses TextToBBoxMapper).
             pass
             
        # For now, just implement rudimentary sync to Other Preview
        # Map idx_preview to Other Side using Diff Opcodes?
        
        # Sync Scroll (handled by scroll bar signal)
        pass

    def on_preview_cursor_right(self):
        pass

if __name__ == "__main__":
    app = QApplication(sys.argv)
    
    # 全局字体设置，防止显示过小
    font = QFont("Microsoft YaHei", 10)
    app.setFont(font)
    
    window = MainWindow()
    window.show()
    sys.exit(app.exec())