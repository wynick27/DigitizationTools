import sys
import os
import json
import re
import fitz  # PyMuPDF
import difflib
import time

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                             QTextEdit, QPlainTextEdit, QLabel, QPushButton, QSplitter, QFileDialog,
                             QMessageBox, QGraphicsView, QGraphicsScene, 
                             QGraphicsRectItem, QLineEdit, QSpinBox, QToolBar, QComboBox, QCheckBox,
                             QDialog, QListWidget)
from PyQt6.QtGui import (QTextCursor, QColor, QSyntaxHighlighter, QTextCharFormat, QTextFormat,
                         QAction, QPixmap, QImage, QPainter, QPen, QFont, QTextOption)
from PyQt6.QtWidgets import QProgressBar
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QThread, pyqtSlot, QSize, QRect
import bisect

from tools.pdf_tools import SplitPdfDialog, ExportPdfImageDialog
from tools.text_tools import MergeTextDialog, read_text_to_pages, write_pages_to_file, PAGE_PATTERN
from tools.furigana import generate_furigana_string, HAS_FURIGANA, HAS_KAKASI
from tools.project_manager_ui import ProjectManagerDialog
from tools.export_manager import ExportManager
from tools.similarity_tools import SimilarityDialog, calculate_page_similarities, text_similarity
from tools.headword_compare_tools import HeadwordCompareDialog
from tools.report_review_tools import ReportReviewDialog

from find_replace import FindReplaceDialog
from lang.i18n import text_from_config




# ==========================================
# 0.0 Unicode Helpers
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

# ==========================================
# 0.1 Default Configuration
# ==========================================

DEFAULT_GLOBAL_CONFIG = {
    "ocr_api_token": "",
    "ocr_api_model": "PaddleOCR-VL-1.6",
    "ocr_retry_count": 3,
    "ocr_concurrent_tasks": 2,
    "ocr_engine": "paddleocr",
    "find_history": [],
    "replace_history": [],
    "shortcuts_alt": [""] * 10,
    "shortcut_furigana": "Ctrl+Shift+F",
    "furigana_left_marker": "[",
    "furigana_right_marker": "]",
    "furigana_kana_type": "hiragana",
    "furigana_use_jmdict_split": True,
    "ui_lang": "zh"
}

DEFAULT_PROJECT_CONFIG = {
    "name": "Default Project",
    "pdf_path": "",
    "image_dir": "",
    "start_page": 1,
    "end_page": 1,
    "page_offset": 0,
    "text_path_left": "",
    "text_path_right": "",
    "ocr_json_path": "ocr_results",
    "regex_left": r"^\*\*(.*?)\*\*",
    "regex_right": r"^([a-zA-Z]*?)",
    "regex_group_left": 0,
    "regex_group_right": 0,
    "use_pdf_render": False,
}

# ==========================================
# 0.1b OCR Utility Imports & Detection
# ==========================================

from ocr.ocr_utils import get_page_image, get_page_image_path, TextToBBoxMapper, BBoxMerger, ImageStitcher
from ocr.ocr_worker import OCRWorker, ImageExportWorker, get_available_engines, refresh_remote_engine_label, V2_MODELS
from ocr.ocr_engines import discover_ocr_results, normalize_ocr_result, PADDLE_ENGINE_ID, canonical_engine_id, sort_ocr_results_by_priority


# ==========================================
# 0.2 Config Manager
# ==========================================
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
                else:
                    for k, v in DEFAULT_GLOBAL_CONFIG.items():
                        if k not in self.data["global"]:
                            self.data["global"][k] = v
                if "projects" not in self.data: 
                    self.data["projects"] = [DEFAULT_PROJECT_CONFIG.copy()]
                if "active_project" not in self.data:
                    self.data["active_project"] = self.data["projects"][0]["name"]
                if self.data.get("global", {}).get("ocr_engine") == "remote":
                    self.data["global"]["ocr_engine"] = "paddleocr"
                    
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

# ==========================================
# 0.6 Language Dictionary (i18n)
# ==========================================



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
        selection.format.setBackground(QColor("#FFFFAA")) # 淡黄色行高亮
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
        if (modifiers & Qt.KeyboardModifier.ControlModifier) and event.button() == Qt.MouseButton.LeftButton:
            opcode = self.get_opcode_at_position(event.pos())
            if opcode:
                self.handle_patch_click(opcode)
                return # 拦截事件，不移动光标

        # 处理 Alt + Click (Push)
        if (modifiers & Qt.KeyboardModifier.AltModifier) and event.button() == Qt.MouseButton.LeftButton:
            opcode = self.get_opcode_at_position(event.pos())
            if opcode:
                self.handle_push_click(opcode)
                return
                
        super().mousePressEvent(event)

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
    bbox_clicked = pyqtSignal(int, int)  # OCR 块索引、块内字符偏移
    def __init__(self):
        super().__init__()
        self.scene = QGraphicsScene()
        self.setScene(self.scene)
        #self.setRenderHint(QPixmap.TransformationMode.SmoothTransformation)
        self.scale_factor = 1.0
        self.bboxes_visible = True
        self.bbox_items = []
        self.highlight_items = []
        self.bbox_click_targets = []
        # 拖拽相关
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)

    def load_content(self, pixmap, ocr_data=None):
        self.scene.clear()
        self.highlight_item = None # Fix: Reset C++ object wrapper
        self.bbox_items = []
        self.highlight_items = []
        self.bbox_click_targets = []
        if pixmap:
            self.scene.addPixmap(pixmap)
            self.setSceneRect(0, 0, pixmap.width(), pixmap.height())
            if ocr_data:
                self.draw_bboxes(ocr_data)
        # self.scale_factor = 1.0 # Removed to persist zoom
        # self.resetTransform()   # Removed to persist zoom

    def normalize_bbox_for_scene(self, bbox, coordinate_type=None):
        if not bbox or len(bbox) != 4:
            return None
        x1, y1, x2, y2 = bbox
        sw = self.sceneRect().width()
        sh = self.sceneRect().height()
        if sw <= 0 or sh <= 0:
            return [x1, y1, x2, y2]
        if coordinate_type == "mineru_page_1000":
            return [
                x1 * sw / 1000.0,
                y1 * sh / 1000.0,
                x2 * sw / 1000.0,
                y2 * sh / 1000.0,
            ]
        if max(abs(x1), abs(y1), abs(x2), abs(y2)) <= 1.5:
            return [x1 * sw, y1 * sh, x2 * sw, y2 * sh]
        return [x1, y1, x2, y2]
        
    def draw_bboxes(self, ocr_data):
        pen = QPen(QColor(255, 0, 0, 200))
        pen.setWidth(3)
        pen.setCosmetic(True)  # 缩放图片时保持固定的屏幕线宽
        
        for i, item in enumerate(ocr_data):
            # 兼容 PaddleOCR 格式
            # item 可能是 dict {'bbox':...} (v3代码) 或 list [points, (text, conf)]
            x, y, w, h = 0, 0, 0, 0
            text = ""
            
            if isinstance(item, dict) and 'bbox' in item:
                bbox = item['bbox'] # [x1, y1, x2, y2]
                scene_bbox = self.normalize_bbox_for_scene(bbox, item.get('bbox_coordinate_type'))
                if not scene_bbox:
                    continue
                x, y, x2, y2 = scene_bbox
                w, h = x2 - x, y2 - y
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
            rect.setVisible(self.bboxes_visible)
            self.bbox_items.append(rect)
            self.scene.addItem(rect)

            area = max(0.0, w) * max(0.0, h)
            self.bbox_click_targets.append((3, area, i, 0, (x, y, w, h)))
            if isinstance(item, dict):
                priority = {'char': 0, 'word': 1, 'line': 2}
                for sub in item.get('sub_items') or []:
                    sub_bbox = self.normalize_bbox_for_scene(
                        sub.get('bbox'),
                        sub.get('bbox_coordinate_type') or item.get('bbox_coordinate_type'),
                    )
                    if not sub_bbox:
                        continue
                    sx1, sy1, sx2, sy2 = sub_bbox
                    sw, sh = sx2 - sx1, sy2 - sy1
                    if sw <= 0 or sh <= 0:
                        continue
                    level = sub.get('level') or 'block'
                    self.bbox_click_targets.append((
                        priority.get(level, 3),
                        sw * sh,
                        i,
                        int(sub.get('start', 0) or 0),
                        (sx1, sy1, sw, sh),
                    ))

    def set_bboxes_visible(self, visible):
        """显示或隐藏普通 OCR 框及当前定位框。"""
        self.bboxes_visible = bool(visible)
        for item in self.bbox_items:
            item.setVisible(self.bboxes_visible)
        for item in self.highlight_items:
            item.setVisible(self.bboxes_visible)

    def mousePressEvent(self, event):
        if (event.modifiers() & Qt.KeyboardModifier.ControlModifier) and (event.button() == Qt.MouseButton.LeftButton):
             scene_pos = self.mapToScene(event.pos())
             matches = []
             for priority, area, block_idx, local_offset, (x, y, w, h) in self.bbox_click_targets:
                 if x <= scene_pos.x() <= x + w and y <= scene_pos.y() <= y + h:
                     matches.append((priority, area, block_idx, local_offset))
             if matches:
                 _priority, _area, block_idx, local_offset = min(matches)
                 self.bbox_clicked.emit(block_idx, local_offset)
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
        """设置单个定位矩形。"""
        self.set_highlight_bboxes([(x, y, w, h, 'block')])

    def set_highlight_bboxes(self, bboxes, ensure_visible=True):
        """同时显示字符/词及其所属行的定位框。"""
        for item in getattr(self, 'highlight_items', []):
            try:
                self.scene.removeItem(item)
            except RuntimeError:
                pass # Already deleted by C++
        self.highlight_items = []
        self.highlight_item = None

        valid_bboxes = [bbox for bbox in bboxes if bbox[2] > 0 and bbox[3] > 0]
        colors = {
            'char': QColor(0, 180, 0, 220),       # 绿色：字符级
            'word': QColor(255, 140, 0, 220),     # 橙色：词级
            'line': QColor(0, 102, 255, 210),     # 蓝色：行级
            'block': QColor(0, 102, 255, 210),    # 蓝色：无细粒度回退框
        }
        z_values = {'char': 13, 'word': 12, 'line': 11, 'block': 11}
        for bbox in valid_bboxes:
            x, y, w, h = bbox[:4]
            level = bbox[4] if len(bbox) > 4 else 'block'
            pen = QPen(colors.get(level, colors['block']))
            pen.setWidth(4)
            pen.setCosmetic(True)  # 定位框始终比普通 OCR 框更醒目
            item = QGraphicsRectItem(x, y, w, h)
            item.setPen(pen)
            level_names = {'char': '字符级', 'word': '词级', 'line': '行级', 'block': '行/块级'}
            item.setToolTip(f"当前定位：{level_names.get(level, '块级')}")
            item.setZValue(z_values.get(level, 11))
            item.setVisible(self.bboxes_visible)
            self.scene.addItem(item)
            self.highlight_items.append(item)
        if self.highlight_items:
            self.highlight_item = self.highlight_items[0]
        if valid_bboxes and ensure_visible:
            # 滚动位置优先保证所属整行可见；细粒度框仍照常绘制。
            navigation_bbox = next(
                (bbox for bbox in valid_bboxes if len(bbox) > 4 and bbox[4] == 'line'),
                None,
            )
            if navigation_bbox is None:
                navigation_bbox = next(
                    (bbox for bbox in valid_bboxes if len(bbox) > 4 and bbox[4] == 'block'),
                    valid_bboxes[0],
                )
            x, y, w, h = navigation_bbox[:4]
            self.ensure_visible_bbox(x, y, w, h)


# ==========================================
# 2.2 OCR Worker (Async)
# ==========================================

# OCRWorker moved to ocr.ocr_worker

class DiffWorker(QThread):
    result_ready = pyqtSignal(list, list) # opcodes, ocr_opcodes

    def __init__(self, text_l, text_r, ocr_text_full, need_ocr_map):
        super().__init__()
        self.text_l = text_l
        self.text_r = text_r
        self.ocr_text_full = ocr_text_full
        self.need_ocr_map = need_ocr_map
    
    def run(self):
        # Main Diff
        matcher = difflib.SequenceMatcher(None, self.text_l, self.text_r, autojunk=False)
        opcodes = matcher.get_opcodes()
        
        # OCR Mapping Diff
        ocr_opcodes = []
        if self.need_ocr_map and self.ocr_text_full:
             m2 = difflib.SequenceMatcher(None, self.text_l, self.ocr_text_full, autojunk=False)
             ocr_opcodes = m2.get_opcodes()
             
        self.result_ready.emit(opcodes, ocr_opcodes)

# ==========================================
# 4. Smart Image Export Helpers
# ==========================================

# TextToBBoxMapper, BBoxMerger, ImageStitcher moved to ocr.ocr_utils
# ImageExportWorker moved to ocr.ocr_worker

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
                self.main_window.config_manager.save()
                self.main_window.reload_all_data()
            else:
                self.main_window.set_current_right_text_path(filename)

    def save_file(self):
        if self.side == "left":
            self.main_window.save_left_data()
        elif self.side == "right":
            self.main_window.save_right_data()

# ==========================================
# 2.6 项目管理对话框
# ==========================================

# Removed ProjectManagerDialog -> tools/project_manager_ui.py


# ==========================================
# 3. 主窗口
# ==========================================

# ==========================================
# 2.5 Config & Templates & Workers
# ==========================================

class GlobalReplaceHistoryDialog(QDialog):
    def __init__(self, main_window):
        super().__init__(main_window)
        self.main_window = main_window
        self.setWindowTitle("全局替换历史")
        self.resize(620, 420)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("选择一个状态进行恢复；撤销和重做也会在此时间线中移动。"))
        self.history_list = QListWidget()
        self.history_list.itemDoubleClicked.connect(lambda _item: self.restore_selected())
        layout.addWidget(self.history_list)

        buttons = QHBoxLayout()
        self.btn_undo = QPushButton("撤销")
        self.btn_redo = QPushButton("重做")
        self.btn_restore = QPushButton("恢复到选中状态")
        btn_close = QPushButton("关闭")
        self.btn_undo.clicked.connect(self.undo)
        self.btn_redo.clicked.connect(self.redo)
        self.btn_restore.clicked.connect(self.restore_selected)
        btn_close.clicked.connect(self.close)
        buttons.addWidget(self.btn_undo)
        buttons.addWidget(self.btn_redo)
        buttons.addStretch()
        buttons.addWidget(self.btn_restore)
        buttons.addWidget(btn_close)
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self):
        self.history_list.clear()
        history = self.main_window.global_undo_stack
        current = self.main_window.global_history_index
        for index, snapshot in enumerate(history):
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(snapshot.get('time', 0)))
            marker = "  ← 当前" if index == current else ""
            self.history_list.addItem(f"{stamp}  {snapshot.get('desc', '全局状态')}{marker}")
        if 0 <= current < self.history_list.count():
            self.history_list.setCurrentRow(current)
        self.btn_undo.setEnabled(current > 0)
        self.btn_redo.setEnabled(0 <= current < len(history) - 1)
        self.btn_restore.setEnabled(bool(history))

    def restore_selected(self):
        row = self.history_list.currentRow()
        if row >= 0 and self.main_window.restore_global_history_index(row):
            self.refresh()

    def undo(self):
        if self.main_window.undo_global():
            self.refresh()

    def redo(self):
        if self.main_window.redo_global():
            self.refresh()


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
        self.exporter = ExportManager(self)
        
        # Sync engine label with saved model name
        refresh_remote_engine_label(self.global_config.get("ocr_api_model", "PaddleOCR-VL-1.6"))
        
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
        self.dirty_pages_right = {}
        self._is_updating_diff = False # Recursion Guard
        
        # Global Undo Stack (for Find/Replace)
        self.global_undo_stack = []
        self.global_history_index = -1
        self._pending_global_description = None
        self.global_history_dialog = None
        self.find_replace_dialog = None
        self.last_manual_edit_time = 0
        self.current_loaded_page = None # Track actual loaded page index
        self.last_active_editor = None # Track last focused editor for shortcuts
        self.last_loaded_right_candidate_index = 0
        
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
        
    def setup_shortcuts(self):
        from PyQt6.QtGui import QShortcut, QKeySequence
        
        # Furigana
        if hasattr(self, 'shortcut_furi') and self.shortcut_furi:
            self.shortcut_furi.deleteLater()
            self.shortcut_furi = None
            
        furi_seq = self.global_config.get("shortcut_furigana", "Ctrl+Shift+F")
        if furi_seq:
            self.shortcut_furi = QShortcut(QKeySequence(furi_seq), self)
            self.shortcut_furi.activated.connect(self.apply_furigana_to_selection)
            
        # Alt+0-9 Shortcuts
        alt_texts = self.global_config.get("shortcuts_alt", [""] * 10)
        
        if hasattr(self, 'alt_shortcuts'):
            for sc in self.alt_shortcuts:
                sc.deleteLater()
                
        if hasattr(self, 'shortcut_toolbar'):
            self.shortcut_toolbar.clear()
                
        self.alt_shortcuts = []
        for i, text in enumerate(alt_texts):
            if text:
                sc = QShortcut(QKeySequence(f"Alt+{i}"), self)
                sc.activated.connect(lambda txt=text: self.insert_shortcut_text(txt))
                self.alt_shortcuts.append(sc)
                
                # Add to toolbar
                if hasattr(self, 'shortcut_toolbar'):
                    btn_widget = QWidget()
                    vbox = QVBoxLayout(btn_widget)
                    vbox.setContentsMargins(5, 0, 5, 0)
                    vbox.setSpacing(0)
                    
                    lbl_key = QLabel(f"Alt+{i}")
                    lbl_key.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    lbl_key.setStyleSheet("color: gray; font-size: 10px;")
                    
                    btn_insert = QPushButton(text)
                    btn_insert.clicked.connect(lambda checked, txt=text: self.insert_shortcut_text(txt))
                    
                    vbox.addWidget(lbl_key)
                    vbox.addWidget(btn_insert)
                    self.shortcut_toolbar.addWidget(btn_widget)
                
                
    def get_text(self, key):
        return text_from_config(self.global_config, key)
        
    def switch_language(self, lang_code):
        self.global_config["ui_lang"] = lang_code
        self.config_manager.save()
        self.retranslate_ui()
        
    def retranslate_ui(self):
        # Update Menu titles
        self.edit_menu.setTitle(self.get_text("menu_edit"))
        self.tools_menu.setTitle(self.get_text("menu_tools"))
        self.menu_export.setTitle(self.get_text("menu_export"))
        self.lang_menu.setTitle(self.get_text("menu_lang"))
        
        # Update Actions
        self.act_find.setText(self.get_text("act_find"))
        self.act_undo_global.setText(self.get_text("act_undo_global"))
        self.act_split.setText(self.get_text("act_split"))
        self.act_exp_img.setText(self.get_text("act_exp_img"))
        self.act_merge.setText(self.get_text("act_merge"))
        self.act_similarity.setText(self.get_text("act_similarity"))
        self.act_headword_compare.setText(self.get_text("act_headword_compare"))
        self.act_report_review.setText(self.get_text("act_report_review"))
        
        self.act_exp_slice.setText(self.get_text("act_exp_slice"))
        self.act_exp_ocr_curr.setText(self.get_text("act_exp_ocr_curr"))
        self.act_exp_ocr_all.setText(self.get_text("act_exp_ocr_all"))
        self.act_exp_md_all.setText(self.get_text("act_exp_md_all"))
        self.act_exp_md_img_all.setText(self.get_text("act_exp_md_img_all"))
        
        self.act_exp_l_json.setText(self.get_text("act_exp_l_json"))
        self.act_exp_r_json.setText(self.get_text("act_exp_r_json"))
        self.act_exp_l_mdx.setText(self.get_text("act_exp_l_mdx"))
        self.act_exp_r_mdx.setText(self.get_text("act_exp_r_mdx"))
        
        self.act_exp_l_json_img.setText(self.get_text("act_exp_l_json_img"))
        self.act_exp_r_json_img.setText(self.get_text("act_exp_r_json_img"))
        self.act_exp_l_mdx_img.setText(self.get_text("act_exp_l_mdx_img"))
        self.act_exp_r_mdx_img.setText(self.get_text("act_exp_r_mdx_img"))
        
        self.action_force_recreate.setText(self.get_text("act_force_recreate"))
        
        # Update Toolbar
        self.lbl_project.setText(self.get_text("lbl_project"))
        self.btn_manage.setText(self.get_text("btn_manage"))
        self.lbl_page.setText(self.get_text("lbl_page"))
        self.lbl_source.setText(self.get_text("lbl_source"))
        self.cb_word_wrap.setText(self.get_text("cb_wrap"))
        self.lbl_engine.setText(self.get_text("lbl_engine"))
        self.lbl_ocr_model.setText(self.get_text("lbl_model"))
        self.btn_ocr_cur.setText(self.get_text("btn_ocr_cur"))
        self.btn_batch.setText(self.get_text("btn_ocr_batch"))
                
    def _get_focused_editor(self):
        """Return the last active text editor (edit_left or edit_right)."""
        editor = getattr(self, 'last_active_editor', None)
        if editor and isinstance(editor, QPlainTextEdit):
            return editor
        # Fallback: try either editor
        if hasattr(self, 'edit_left'):
            return self.edit_left
        return None
                
    def insert_shortcut_text(self, text):
        editor = self._get_focused_editor()
        if editor:
            editor.setFocus()
            cursor = editor.textCursor()
            cursor.insertText(text)
            editor.setTextCursor(cursor)
            
    def apply_furigana_to_selection(self):
        if not HAS_FURIGANA: return
        
        editor = self._get_focused_editor()
        if not editor: return
        
        editor.setFocus()
        cursor = editor.textCursor()
        if not cursor.hasSelection(): return
        
        sel_text = cursor.selectedText()
        if not sel_text.strip(): return
        
        # Process Furigana via extracted tool
        result_text = generate_furigana_string(
            sel_text,
            left_marker=self.global_config.get("furigana_left_marker", "["),
            right_marker=self.global_config.get("furigana_right_marker", "]"),
            kana_type=self.global_config.get("furigana_kana_type", "hiragana"),
            use_jmdict_split=self.global_config.get("furigana_use_jmdict_split", True),
        )
                
        cursor.insertText(result_text)
        editor.setTextCursor(cursor)

    def show_split_pdf_dialog(self):
        d = SplitPdfDialog(self)
        d.exec()
        
    def show_export_pdf_img_dialog(self):
        d = ExportPdfImageDialog(self)
        d.exec()
        
    def show_merge_text_dialog(self):
        d = MergeTextDialog(self)
        d.exec()

    def show_similarity_dialog(self):
        if not hasattr(self, "similarity_dialog") or self.similarity_dialog is None:
            self.similarity_dialog = SimilarityDialog(self)
            self.similarity_dialog.destroyed.connect(lambda: setattr(self, "similarity_dialog", None))
        self.similarity_dialog.show()
        self.similarity_dialog.raise_()
        self.similarity_dialog.activateWindow()

    def show_headword_compare_dialog(self):
        if not hasattr(self, "headword_compare_dialog") or self.headword_compare_dialog is None:
            self.headword_compare_dialog = HeadwordCompareDialog(self)
            self.headword_compare_dialog.destroyed.connect(lambda: setattr(self, "headword_compare_dialog", None))
        self.headword_compare_dialog.show()
        self.headword_compare_dialog.raise_()
        self.headword_compare_dialog.activateWindow()

    def show_report_review_dialog(self):
        if not hasattr(self, "report_review_dialog") or self.report_review_dialog is None:
            self.report_review_dialog = ReportReviewDialog(self)
            self.report_review_dialog.destroyed.connect(lambda: setattr(self, "report_review_dialog", None))
        self.report_review_dialog.show()
        self.report_review_dialog.raise_()
        self.report_review_dialog.activateWindow()

    def calculate_page_similarities(self):
        return calculate_page_similarities(self.pages_left, self.pages_right_text)

    def start_background_progress(self, label):
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setValue(0)
        self.statusBar().showMessage(f"{label}: 准备计算...")

    def update_background_progress(self, done, total, message):
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, max(total, 1))
        self.progress_bar.setValue(done)
        self.statusBar().showMessage(message)

    def finish_background_progress(self, message):
        self.progress_bar.setVisible(False)
        self.statusBar().showMessage(message, 5000)

    def init_ui(self):
        # --- 工具栏 ---
        toolbar = QToolBar()
        self.addToolBar(toolbar)
        
        # Project Controls
        self.lbl_project = QLabel("Project: ")
        toolbar.addWidget(self.lbl_project)
        self.combo_project = QComboBox()
        self.combo_project.setMinimumWidth(150)
        self.update_project_combo()
        self.combo_project.activated.connect(self.on_project_switched)
        toolbar.addWidget(self.combo_project)

        # Menu Bar
        self.menubar = self.menuBar()
        self.edit_menu = self.menubar.addMenu("Edit")
        
        self.act_find = QAction("Find and Replace", self)
        self.act_find.setShortcut("Ctrl+F")
        self.act_find.triggered.connect(self.show_find_replace)
        self.edit_menu.addAction(self.act_find)
        
        self.act_undo_global = QAction("Global Replace History", self)
        self.act_undo_global.triggered.connect(self.show_global_replace_history)
        self.edit_menu.addAction(self.act_undo_global)
        
        self.tools_menu = self.menubar.addMenu("Tools (实用工具)")
        self.act_split = QAction("拆分PDF (Split PDF)", self)
        self.act_split.triggered.connect(self.show_split_pdf_dialog)
        self.tools_menu.addAction(self.act_split)
        
        self.act_exp_img = QAction("导出PDF图片 (Export PDF Images)", self)
        self.act_exp_img.triggered.connect(self.show_export_pdf_img_dialog)
        self.tools_menu.addAction(self.act_exp_img)
        
        self.act_merge = QAction("合并文本文件 (Merge Texts)", self)
        self.act_merge.triggered.connect(self.show_merge_text_dialog)
        self.tools_menu.addAction(self.act_merge)
        

        self.tools_menu.addSeparator()
        self.act_similarity = QAction("相似度窗口", self)
        self.act_similarity.triggered.connect(self.show_similarity_dialog)
        self.tools_menu.addAction(self.act_similarity)
        self.act_headword_compare = QAction("词头对比窗口", self)
        self.act_headword_compare.triggered.connect(self.show_headword_compare_dialog)
        self.tools_menu.addAction(self.act_headword_compare)
        self.act_report_review = QAction("外部报告审阅", self)
        self.act_report_review.triggered.connect(self.show_report_review_dialog)
        self.tools_menu.addAction(self.act_report_review)

        self.btn_manage = QPushButton("Settings / Manage")
        self.btn_manage.clicked.connect(self.open_project_manager)
        toolbar.addWidget(self.btn_manage)
        
        toolbar.addSeparator()
        
        # 页码控制
        self.spin_page = QLineEdit()
        self.spin_page.setFixedWidth(50)
        self.spin_page.returnPressed.connect(self.jump_page)
        
        btn_prev = QPushButton("<"); btn_prev.setFixedWidth(30); btn_prev.clicked.connect(self.prev_page)
        btn_next = QPushButton(">"); btn_next.setFixedWidth(30); btn_next.clicked.connect(self.next_page)
        
        self.lbl_page = QLabel("页码: ")
        toolbar.addWidget(self.lbl_page)
        toolbar.addWidget(btn_prev)
        toolbar.addWidget(self.spin_page)
        toolbar.addWidget(btn_next)
        toolbar.addSeparator()
        
        # 数据源选择
        self.lbl_source = QLabel(" 右侧数据源: ")
        toolbar.addWidget(self.lbl_source)
        self.combo_source = QComboBox()
        self.combo_source.setMinimumWidth(220)
        self.combo_source.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.combo_source.setMinimumContentsLength(24)
        self.combo_source.addItem("Text File B", {"type": "text", "candidate_index": 0})
        self.combo_source.currentIndexChanged.connect(self.on_source_changed)
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

        self.cb_show_bboxes = QCheckBox("定位框")
        self.cb_show_bboxes.setChecked(True)
        self.cb_show_bboxes.setToolTip(
            "显示/隐藏图片上的 OCR 定位框\n"
            "蓝色：行/块级；橙色：词级；绿色：字符级"
        )
        # image_view 在主布局阶段创建，因此在信号触发时再解析它。
        self.cb_show_bboxes.toggled.connect(lambda visible: self.image_view.set_bboxes_visible(visible))
        toolbar.addWidget(self.cb_show_bboxes)
        
        toolbar.addSeparator()
        self.lbl_engine = QLabel(" OCR Engine: ")
        toolbar.addWidget(self.lbl_engine)
        self.combo_ocr_engine = QComboBox()
        self.combo_ocr_engine.currentIndexChanged.connect(self.on_ocr_engine_changed)
        toolbar.addWidget(self.combo_ocr_engine)

        # Model selector — only relevant for remote engine
        self.lbl_ocr_model = QLabel(" 模型: ")
        toolbar.addWidget(self.lbl_ocr_model)
        self.combo_ocr_model = QComboBox()
        self.combo_ocr_model.setMinimumWidth(180)
        self.combo_ocr_model.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.combo_ocr_model.setMinimumContentsLength(18)
        self.combo_ocr_model.currentIndexChanged.connect(self.on_ocr_model_changed)
        toolbar.addWidget(self.combo_ocr_model)
        
        self.btn_ocr_cur = QPushButton("OCR当前页面")
        self.btn_ocr_cur.clicked.connect(self.run_current_ocr_unified)
        toolbar.addWidget(self.btn_ocr_cur)
        
        self.btn_batch = QPushButton("OCR所有缺失页面")
        self.btn_batch.clicked.connect(self.run_batch_ocr)
        toolbar.addWidget(self.btn_batch)
        self.refresh_ocr_engine_combo()

        # Export Menu (Moved to Menu Bar)
        self.menu_export = self.menubar.addMenu("Export (导出)")
        
        self.act_exp_slice = QAction("导出当前页面切图", self)
        self.act_exp_slice.triggered.connect(self.exporter.export_slices)
        self.menu_export.addAction(self.act_exp_slice)
        
        self.act_exp_ocr_curr = QAction("导出当前页面OCR文本", self)
        self.act_exp_ocr_curr.triggered.connect(self.exporter.export_ocr_dict_current)
        self.menu_export.addAction(self.act_exp_ocr_curr)
        
        self.menu_export.addSeparator()
        
        self.act_exp_ocr_all = QAction("导出所有页面OCR文本", self)
        self.act_exp_ocr_all.triggered.connect(self.exporter.export_all_ocr_txt)
        self.menu_export.addAction(self.act_exp_ocr_all)
        
        # New Markdown Exports
        self.act_exp_md_all = QAction("导出所有为Markdown", self)
        self.act_exp_md_all.triggered.connect(lambda: self.exporter.export_all_markdown(with_images=False))
        self.menu_export.addAction(self.act_exp_md_all)
        
        self.act_exp_md_img_all = QAction("导出所有为Markdown+图片", self)
        self.act_exp_md_img_all.triggered.connect(lambda: self.exporter.export_all_markdown(with_images=True))
        self.menu_export.addAction(self.act_exp_md_img_all)
        
        self.menu_export.addSeparator()
        
        self.act_exp_l_json = QAction("导出左侧文本(.json)", self)
        self.act_exp_l_json.triggered.connect(lambda: self.exporter.export_parsed("left", "json"))
        self.menu_export.addAction(self.act_exp_l_json)
        
        self.act_exp_r_json = QAction("导出右侧文本(.json)", self)
        self.act_exp_r_json.triggered.connect(lambda: self.exporter.export_parsed("right", "json"))
        self.menu_export.addAction(self.act_exp_r_json)
        
        self.act_exp_l_mdx = QAction("导出左侧文本(.mdx.txt)", self)
        self.act_exp_l_mdx.triggered.connect(lambda: self.exporter.export_parsed("left", "mdx"))
        self.menu_export.addAction(self.act_exp_l_mdx)
        
        self.act_exp_r_mdx = QAction("导出右侧文本(.mdx.txt)", self)
        self.act_exp_r_mdx.triggered.connect(lambda: self.exporter.export_parsed("right", "mdx"))
        self.menu_export.addAction(self.act_exp_r_mdx)
        
        self.menu_export.addSeparator()
        
        self.act_exp_l_json_img = QAction("导出左侧文本(.json)+图片", self)
        self.act_exp_l_json_img.triggered.connect(lambda: self.exporter.export_parsed_with_images("left", "json"))
        self.menu_export.addAction(self.act_exp_l_json_img)
        
        self.act_exp_r_json_img = QAction("导出右侧文本(.json)+图片", self)
        self.act_exp_r_json_img.triggered.connect(lambda: self.exporter.export_parsed_with_images("right", "json"))
        self.menu_export.addAction(self.act_exp_r_json_img)
        
        self.act_exp_l_mdx_img = QAction("导出左侧文本(.mdx.txt)+图片", self)
        self.act_exp_l_mdx_img.triggered.connect(lambda: self.exporter.export_parsed_with_images("left", "mdx"))
        self.menu_export.addAction(self.act_exp_l_mdx_img)
        
        self.act_exp_r_mdx_img = QAction("导出右侧文本(.mdx.txt)+图片", self)
        self.act_exp_r_mdx_img.triggered.connect(lambda: self.exporter.export_parsed_with_images("right", "mdx"))
        self.menu_export.addAction(self.act_exp_r_mdx_img)
        
        self.menu_export.addSeparator()
        self.action_force_recreate = QAction("强制重新生成图片", self, checkable=True)
        self.action_force_recreate.setChecked(False)
        self.menu_export.addAction(self.action_force_recreate)
        
        self.lang_menu = self.menubar.addMenu("Language")
        from PyQt6.QtGui import QActionGroup
        self.lang_group = QActionGroup(self)
        self.lang_group.setExclusive(True)
        
        self.act_lang_zh = QAction("中文", self, checkable=True)
        self.act_lang_zh.triggered.connect(lambda: self.switch_language("zh"))
        self.lang_group.addAction(self.act_lang_zh)
        self.lang_menu.addAction(self.act_lang_zh)
        
        self.act_lang_en = QAction("English", self, checkable=True)
        self.act_lang_en.triggered.connect(lambda: self.switch_language("en"))
        self.lang_group.addAction(self.act_lang_en)
        self.lang_menu.addAction(self.act_lang_en)
        
        curr_lang = self.global_config.get("ui_lang", "zh")
        if curr_lang == "zh": self.act_lang_zh.setChecked(True)
        else: self.act_lang_en.setChecked(True)
        
        self.retranslate_ui()
        
        # Shortcut Toolbar creation
        self.shortcut_toolbar = QToolBar("Shortcuts")
        self.addToolBarBreak()
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.shortcut_toolbar)
        
        # Setup shortcuts after toolbar is created to populate the buttons
        self.setup_shortcuts()

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
        
        # 2.1 正则配置区
        regex_layout = QHBoxLayout()
        
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
        self.edit_left = DiffTextEdit("left")
        
        left_box.addWidget(self.header_left)
        left_box.addWidget(self.edit_left)
        
        # --- Right Side Container ---
        right_container_widget = QWidget() # Rename to avoid conflict with right_container (outer)
        right_box = QVBoxLayout(right_container_widget)
        right_box.setContentsMargins(0,0,0,0)
        right_box.setSpacing(0)
        
        self.header_right = FileHeaderWidget(self, "right")
        self.edit_right = DiffTextEdit("right")
        
        right_box.addWidget(self.header_right)
        right_box.addWidget(self.edit_right)
        
        # 初始化 Highlighters
        self.highlighter_left = DiffSyntaxHighlighter(self.edit_left.document())
        self.highlighter_right = DiffSyntaxHighlighter(self.edit_right.document())
        
        # 绑定信号
        self.edit_left.textChanged.connect(self.on_text_changed_left)
        self.edit_right.textChanged.connect(self.on_text_changed_right)
        
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
        self.edit_left.cursorPositionChanged.connect(self.on_cursor_left)
        self.edit_right.cursorPositionChanged.connect(self.on_cursor_right)
        
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
        self.right_text_candidates = self.get_right_text_candidates()
        self.current_right_candidate_index = min(
            int(self.project_config.get("active_right_text_candidate", 0) or 0),
            max(0, len(self.right_text_candidates) - 1),
        )
        self.right_candidate_pages = {
            idx: read_text_to_pages(candidate.get("path", ""))
            for idx, candidate in enumerate(self.right_text_candidates)
        }
        self.pages_right_text = self.right_candidate_pages.get(self.current_right_candidate_index, {})
        self.refresh_right_candidate_combo()
        
        # 2. 加载 PDF
        self.doc = None
        if self.project_config['pdf_path'] and os.path.exists(self.project_config['pdf_path']):
            try:
                self.doc = fitz.open(self.project_config['pdf_path'])
            except:
                self.doc = None
        
        # 3. Update Headers
        self.header_left.set_path(self.project_config.get('text_path_left', ''))
        self.header_right.set_path(self.get_current_right_text_path())

        self.dirty_pages_left.clear()
        self.dirty_pages_right.clear()
        self.load_current_page()

 
    def get_right_text_candidates(self):
        candidates = []
        primary_path = self.project_config.get("text_path_right", "")
        candidates.append({
            "label": "Text B",
            "path": primary_path,
        })
        for candidate in self.project_config.get("right_text_candidates", []):
            if isinstance(candidate, dict):
                path = candidate.get("path", "")
                label = candidate.get("label") or os.path.basename(path) or "Candidate"
            else:
                path = str(candidate)
                label = os.path.basename(path) or "Candidate"
            if path and path != primary_path:
                candidates.append({"label": label, "path": path})
        return candidates

    def get_current_right_text_path(self):
        candidates = getattr(self, "right_text_candidates", None) or self.get_right_text_candidates()
        idx = getattr(self, "current_right_candidate_index", 0)
        if 0 <= idx < len(candidates):
            return candidates[idx].get("path", "")
        return self.project_config.get("text_path_right", "")

    def is_text_source_selected(self):
        data = self.combo_source.currentData() if hasattr(self, "combo_source") else None
        return isinstance(data, dict) and data.get("type") == "text"

    @staticmethod
    def ocr_source_identity(source_data):
        """跨页识别同一 OCR 引擎/模型，忽略结果文件名中的页码。"""
        if not isinstance(source_data, dict) or source_data.get("type") != "ocr":
            return None
        filename = os.path.splitext(os.path.basename(source_data.get("path", "")))[0]
        suffix = re.sub(r"^(?:page_)?\d+(?:_|$)", "", filename, count=1, flags=re.I)
        return (
            canonical_engine_id(source_data.get("engine_id", "")),
            suffix.lower(),
            bool(source_data.get("legacy")),
        )

    @staticmethod
    def source_data_matches(left, right):
        if not isinstance(left, dict) or not isinstance(right, dict):
            return left == right
        if left.get("type") != right.get("type"):
            return False
        if left.get("type") == "text":
            return left.get("candidate_index", 0) == right.get("candidate_index", 0)
        return left.get("path") == right.get("path")

    def restore_source_selection(self, source_data):
        self.combo_source.blockSignals(True)
        try:
            for index in range(self.combo_source.count()):
                if self.source_data_matches(self.combo_source.itemData(index), source_data):
                    self.combo_source.setCurrentIndex(index)
                    break
        finally:
            self.combo_source.blockSignals(False)

    def on_source_changed(self, _index):
        """切换右侧数据源前处理当前文本的未保存修改。"""
        if getattr(self, '_is_loading', False):
            return
        new_source = self.combo_source.currentData()
        old_source = getattr(self, 'last_loaded_source_data', None)
        if self.source_data_matches(old_source, new_source):
            return

        self.save_current_page_data()
        if isinstance(old_source, dict) and old_source.get('type') == 'text':
            old_candidate = int(old_source.get('candidate_index', 0) or 0)
            if self.dirty_pages_right.get(old_candidate):
                reply = QMessageBox.question(
                    self,
                    "保存右侧文本",
                    f"{self.get_right_candidate_label(old_candidate)} 有未保存的修改，切换前是否保存？",
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No
                    | QMessageBox.StandardButton.Cancel,
                )
                if reply == QMessageBox.StandardButton.Cancel:
                    self.restore_source_selection(old_source)
                    return
                if reply == QMessageBox.StandardButton.Yes:
                    if not self.save_right_data(old_candidate, force=True):
                        self.restore_source_selection(old_source)
                        return
                else:
                    path = self.right_text_candidates[old_candidate].get('path', '')
                    self.right_candidate_pages[old_candidate] = read_text_to_pages(path)
                    self.dirty_pages_right.setdefault(old_candidate, set()).clear()
                    if old_candidate == getattr(self, 'current_right_candidate_index', 0):
                        self.pages_right_text = self.right_candidate_pages[old_candidate]
                    # 防止 load_current_page 再把编辑器里的旧内容写回缓存。
                    self.last_loaded_source = "Discarded Text File B"

        self.load_current_page()

    def select_text_source(self, candidate_index=None):
        candidate_index = getattr(self, "current_right_candidate_index", 0) if candidate_index is None else candidate_index
        for i in range(self.combo_source.count()):
            data = self.combo_source.itemData(i)
            if isinstance(data, dict) and data.get("type") == "text" and data.get("candidate_index") == candidate_index:
                self.combo_source.setCurrentIndex(i)
                return

    def get_right_candidate_label(self, idx):
        candidates = getattr(self, "right_text_candidates", [])
        if 0 <= idx < len(candidates):
            return candidates[idx].get("label") or os.path.basename(candidates[idx].get("path", "")) or f"Text B {idx + 1}"
        return f"Text B {idx + 1}"

    def set_current_right_text_path(self, path):
        idx = getattr(self, "current_right_candidate_index", 0)
        candidates = getattr(self, "right_text_candidates", [])
        if idx <= 0 or idx >= len(candidates):
            idx = 0
            self.project_config['text_path_right'] = path
        else:
            candidates[idx]["path"] = path
            configured = self.project_config.setdefault("right_text_candidates", [])
            config_idx = idx - 1
            while len(configured) <= config_idx:
                configured.append({})
            if not isinstance(configured[config_idx], dict):
                configured[config_idx] = {"path": str(configured[config_idx])}
            configured[config_idx]["path"] = path
            if not configured[config_idx].get("label"):
                configured[config_idx]["label"] = os.path.basename(path)

        self.current_right_candidate_index = idx
        self.project_config["active_right_text_candidate"] = idx
        self.right_text_candidates = self.get_right_text_candidates()
        self.right_candidate_pages[idx] = read_text_to_pages(path)
        self.pages_right_text = self.right_candidate_pages.get(idx, {})
        self.header_right.set_path(path)
        self.config_manager.save()
        self.refresh_ocr_source_options(self.current_loaded_page or self.project_config.get('start_page', 1))
        self.select_text_source(idx)
        self.load_current_page()

    def current_right_dirty_pages(self):
        idx = getattr(self, "current_right_candidate_index", 0)
        return self.dirty_pages_right.setdefault(idx, set())

    def has_dirty_right_pages(self):
        return any(bool(pages) for pages in self.dirty_pages_right.values())

    def refresh_right_candidate_combo(self):
        return

    def refresh_ocr_source_options(self, page_num):
        current_text = self.combo_source.currentText() if hasattr(self, "combo_source") else "Text File B"
        current_path = None
        current_ocr_identity = None
        current_candidate = getattr(self, "current_right_candidate_index", 0)
        current_data = self.combo_source.currentData() if hasattr(self, "combo_source") else None
        if isinstance(current_data, dict):
            if current_data.get("type") == "ocr":
                current_path = current_data.get("path")
                current_ocr_identity = self.ocr_source_identity(current_data)
            elif current_data.get("type") == "text":
                current_candidate = current_data.get("candidate_index", current_candidate)

        real_page_num = page_num + self.project_config.get('page_offset', 0)
        results = sort_ocr_results_by_priority(
            discover_ocr_results(self.project_config.get('ocr_json_path'), real_page_num),
            self.global_config,
        )

        self.combo_source.blockSignals(True)
        self.combo_source.clear()
        for idx, candidate in enumerate(getattr(self, "right_text_candidates", [])):
            path = candidate.get("path", "")
            path_name = os.path.basename(path)
            label = candidate.get("label") or path_name or f"Text B {idx + 1}"
            display = f"Text B: {path_name or label}" if idx == 0 and (path_name or label != "Text B") else "Text B"
            if idx > 0:
                display = f"Text B: {label}"
            self.combo_source.addItem(display, {"type": "text", "candidate_index": idx})
            self.combo_source.setItemData(self.combo_source.count() - 1, path, Qt.ItemDataRole.ToolTipRole)
        for idx, info in enumerate(results):
            label = "OCR Results" if idx == 0 and info.get("legacy") else info.get("label", "OCR")
            if label in [self.combo_source.itemText(i) for i in range(self.combo_source.count())]:
                label = f"{label} ({os.path.basename(info.get('path', ''))})"
            ocr_info = dict(info)
            ocr_info["type"] = "ocr"
            self.combo_source.addItem(label, ocr_info)
            self.combo_source.setItemData(self.combo_source.count() - 1, info.get("path", ""), Qt.ItemDataRole.ToolTipRole)

        target_idx = 0
        if current_path:
            for i in range(self.combo_source.count()):
                data = self.combo_source.itemData(i)
                if isinstance(data, dict) and data.get("type") == "ocr" and data.get("path") == current_path:
                    target_idx = i
                    break
            else:
                # 页码变化后路径会变化；若新页存在同一 OCR 来源则保持选择。
                for i in range(self.combo_source.count()):
                    data = self.combo_source.itemData(i)
                    if self.ocr_source_identity(data) == current_ocr_identity:
                        target_idx = i
                        break
        elif isinstance(current_data, dict) and current_data.get("type") == "text":
            for i in range(self.combo_source.count()):
                data = self.combo_source.itemData(i)
                if isinstance(data, dict) and data.get("type") == "text" and data.get("candidate_index") == current_candidate:
                    target_idx = i
                    break
        elif current_text != "Text File B":
            for i in range(self.combo_source.count()):
                if self.combo_source.itemText(i) == current_text:
                    target_idx = i
                    break
            else:
                if self.combo_source.count() > 1:
                    target_idx = 1

        self.combo_source.setCurrentIndex(target_idx)
        self.combo_source.blockSignals(False)

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

            self.refresh_ocr_source_options(page_num)
        
            # 1. Load OCR Data
            current_source_data = self.combo_source.currentData()
            if isinstance(current_source_data, dict) and current_source_data.get("type") == "text":
                candidate_idx = int(current_source_data.get("candidate_index", 0) or 0)
                if candidate_idx != getattr(self, "current_right_candidate_index", 0):
                    self.current_right_candidate_index = candidate_idx
                    self.project_config["active_right_text_candidate"] = candidate_idx
                    self.pages_right_text = self.right_candidate_pages.get(candidate_idx, {})
                    self.header_right.set_path(self.get_current_right_text_path())
                    self.config_manager.save()
            ocr_result_info = current_source_data if isinstance(current_source_data, dict) and current_source_data.get("type") == "ocr" else None
            ocr_data = self.load_ocr_json(page_num, ocr_result_info)
            self.current_ocr_data = ocr_data # Store for highlighting
            
            # 2. Load Image (High Res). Always refresh the canvas; no image means blank preview.
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
                    sub_items = []
                    bbox_coordinate_type = None
                    if isinstance(item, dict):
                        text = item.get('text', '')
                        bbox = item.get('bbox', [])
                        sub_items = item.get('sub_items', [])
                        bbox_coordinate_type = item.get('bbox_coordinate_type')
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
                        'bbox': bbox,
                        'sub_items': sub_items,
                        'bbox_coordinate_type': bbox_coordinate_type,
                    })
                    current_idx += length
            
            source_data = self.combo_source.currentData()
            is_ocr_mode = isinstance(source_data, dict) and source_data.get("type") == "ocr"
            if not is_ocr_mode and isinstance(source_data, dict) and source_data.get("type") == "text":
                self.last_loaded_right_candidate_index = int(source_data.get("candidate_index", 0) or 0)
            
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
            self._show_similarity_after_load = not getattr(self, "_suppress_next_load_similarity_status", False)
            self._suppress_next_load_similarity_status = False
            self.deferred_run_diff()
            
            # Record last loaded source
            self.last_loaded_source = "OCR Results" if is_ocr_mode else "Text File B"
            self.last_loaded_source_data = dict(source_data) if isinstance(source_data, dict) else source_data

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
            if self.is_text_source_selected():
                text_r = self.pages_right_text.get(p, "")
                self.edit_right.setPlainText(text_r)
                
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
            image_path = get_page_image_path(img_dir, real_page_num)
            if image_path:
                return QPixmap(image_path)
        return None

    def load_ocr_json(self, page_num, result_info=None):
        """加载 PaddleOCR 格式 JSON"""
        if result_info and result_info.get("path"):
            f_path = result_info["path"]
            engine_id = result_info.get("engine_id", PADDLE_ENGINE_ID)
        else:
            path = self.project_config['ocr_json_path']
            real_page_num = page_num + self.project_config.get('page_offset', 0)
            discovered = sort_ocr_results_by_priority(discover_ocr_results(path, real_page_num), self.global_config)
            if discovered:
                result_info = discovered[0]
                f_path = result_info["path"]
                engine_id = result_info.get("engine_id", PADDLE_ENGINE_ID)
            else:
                f_path = os.path.join(path, f"page_{real_page_num}.json")
                engine_id = PADDLE_ENGINE_ID
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
                    
                    return normalize_ocr_result(data, engine_id, self.global_config)
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
        
        need_ocr_map = (self.is_text_source_selected() and bool(self.ocr_text_full))
        
        self.diff_worker = DiffWorker(text_l, text_r, self.ocr_text_full, need_ocr_map)
        self.diff_worker.result_ready.connect(self.on_diff_finished)
        self.diff_worker.finished.connect(self.on_diff_thread_finished)
        self.diff_worker.start()

    def on_diff_thread_finished(self):
        self._is_updating_diff = False
        self.diff_worker = None

    def on_diff_finished(self, opcodes, ocr_opcodes):
        text_l = self.edit_left.toPlainText()
        text_r = self.edit_right.toPlainText()
        
        # Set Data
        self.edit_left.set_diff_data(opcodes, text_r)
        self.edit_right.set_diff_data(opcodes, text_l)
        
        # Highlight
        self.edit_left.blockSignals(True)
        self.edit_right.blockSignals(True)
        self.highlighter_left.set_diff_data(opcodes, is_left=True)
        self.highlighter_right.set_diff_data(opcodes, is_left=False)
        self.edit_left.blockSignals(False)
        self.edit_right.blockSignals(False)
        
        # Regex
        # Regex
        self.highlighter_left.set_regex(self.project_config.get("regex_left"), self.project_config.get("regex_group_left", 0))
        self.highlighter_right.set_regex(self.project_config.get("regex_right"), self.project_config.get("regex_group_right", 0))
        
        # OCR Mapping
        if ocr_opcodes:
            self.ocr_diff_opcodes = ocr_opcodes
        else:
            # If not calculated, maybe we use main diff if right is OCR
            if not self.is_text_source_selected():
                 self.ocr_diff_opcodes = opcodes



        if getattr(self, "_show_similarity_after_load", False):
            self._show_similarity_after_load = False
            page = self.current_loaded_page if self.current_loaded_page is not None else self.spin_page.text()
            ratio = text_similarity(text_l, text_r)
            self.statusBar().showMessage(f"Page {page} Similarity: {ratio * 100:.2f}%")

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
        if self.dirty_pages_left or self.has_dirty_right_pages():
            msg = "Unsaved changes in:\n"
            if self.dirty_pages_left: msg += "- Left Text\n"
            for idx, pages in sorted(self.dirty_pages_right.items()):
                if pages:
                    msg += f"- Right Text: {self.get_right_candidate_label(idx)}\n"
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
                if self.has_dirty_right_pages(): self.save_all_dirty_right_data()
                return True
        return True

    def mark_page_dirty(self, page_num, is_left):
        try: p = int(page_num)
        except: return
        
        if is_left:
            self.dirty_pages_left.add(p)
        else:
            self.current_right_dirty_pages().add(p)
            
    def update_memory_cache(self):
        """Update memory dicts from editors"""
        try:
            page_num = self.current_loaded_page
            # Logic handled in save_current_page_data mostly, but for live updates:
            self.pages_left[page_num] = self.edit_left.toPlainText()
            if self.is_text_source_selected():
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
                self.current_right_dirty_pages().add(p)
            except: pass
            self.update_memory_cache()
        self.deferred_run_diff()

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
        p = self.current_loaded_page
        
        # Left
        current_left = self.edit_left.toPlainText()
        saved_left = self.pages_left.get(p, "")
        if current_left != saved_left:
            self.pages_left[p] = current_left
            self.mark_page_dirty(p, True)
            
        # Right (Check Last Loaded Source!)
        if hasattr(self, 'last_loaded_source') and self.last_loaded_source == "Text File B":
            candidate_idx = getattr(self, "last_loaded_right_candidate_index", getattr(self, "current_right_candidate_index", 0))
            pages = self.right_candidate_pages.setdefault(candidate_idx, {})
            current_right = self.edit_right.toPlainText()
            saved_right = pages.get(p, "")
            if current_right != saved_right:
                 pages[p] = current_right
                 if candidate_idx == getattr(self, "current_right_candidate_index", 0):
                     self.pages_right_text = pages
                 self.dirty_pages_right.setdefault(candidate_idx, set()).add(p)
        elif not hasattr(self, 'last_loaded_source'):
            # Fallback for initialization or if variable missing
             if self.is_text_source_selected(): # This was the buggy line if switching!
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
        # 当前侧高亮由光标处理函数设置，这里保留它并同时高亮映射侧。
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
        is_ocr_mode = not self.is_text_source_selected()
        if editor == self.edit_right and is_ocr_mode:
            self._handle_right_editor_ocr_scroll(editor, idx)
            return

        # 如果是 Left Editor (或者 Right Editor 非 OCR 模式)
        # 使用 Diff Mapping 映射到 OCR Index
        
        target_ocr_idx = -1
        
        # 1. 先统一映射到左侧文本索引，再由左侧映射到 OCR 索引。
        src_py_idx = to_py_pos(editor.toPlainText(), idx)
        left_py_idx = src_py_idx
        if editor == self.edit_right:
            left_py_idx = -1
            for tag, i1, i2, j1, j2 in getattr(self.edit_left, 'diff_opcodes', []):
                if j1 <= src_py_idx <= j2:
                    left_py_idx = i1 + (src_py_idx - j1) if tag == 'equal' else i1
                    left_py_idx = min(left_py_idx, i2)
                    break

        if left_py_idx >= 0:
            for tag, i1, i2, j1, j2 in getattr(self, 'ocr_diff_opcodes', []):
                if i1 <= left_py_idx <= i2:
                    target_ocr_idx = j1 + (left_py_idx - i1) if tag == 'equal' else j1
                    target_ocr_idx = min(target_ocr_idx, j2)
                    break
        
        # 2. Find BBox for target_ocr_idx
        if target_ocr_idx >= 0:
            entries = self.find_ocr_bbox_entries_for_index(target_ocr_idx)
            if entries:
                self.image_view.set_highlight_bboxes(
                    [(x1, y1, x2 - x1, y2 - y1, level) for level, (x1, y1, x2, y2) in entries]
                )
                return

    def normalize_display_bbox(self, bbox, coordinate_type=None):
        return self.image_view.normalize_bbox_for_scene(bbox, coordinate_type)

    def find_ocr_bbox_for_index(self, ocr_idx):
        bboxes = self.find_ocr_bboxes_for_index(ocr_idx)
        return bboxes[0] if bboxes else None

    def find_ocr_bboxes_for_index(self, ocr_idx):
        return [bbox for _level, bbox in self.find_ocr_bbox_entries_for_index(ocr_idx)]

    def find_ocr_bbox_entries_for_index(self, ocr_idx):
        if not hasattr(self, 'ocr_char_map'):
            return []
        for mapping in self.ocr_char_map:
            start = mapping.get('start_index', 0)
            end = mapping.get('end_index', 0)
            if start <= ocr_idx <= end + 1:
                local_idx = max(0, min(ocr_idx - start, max(0, end - start)))
                # 同一字符可能同时落在 line/word/char 中，优先使用最细粒度。
                priority = {'char': 0, 'word': 1, 'line': 2}
                sub_items = sorted(
                    mapping.get('sub_items') or [],
                    key=lambda sub: priority.get(sub.get('level'), 3),
                )
                matches = []
                for sub in sub_items:
                    sub_start = sub.get('start', 0)
                    sub_end = sub.get('end', sub_start)
                    if sub_start <= local_idx < sub_end:
                        bbox = self.normalize_display_bbox(
                            sub.get('bbox'),
                            sub.get('bbox_coordinate_type') or mapping.get('bbox_coordinate_type'),
                        )
                        if bbox:
                            level = sub.get('level') or 'block'
                            matches.append((priority.get(level, 3), level, bbox))
                if matches:
                    # 最细框用于滚动定位，同时保留 word/line 等所属层级框。
                    return [(level, bbox) for _priority, level, bbox in sorted(matches, key=lambda item: item[0])]
                block_bbox = self.normalize_display_bbox(mapping.get('bbox'), mapping.get('bbox_coordinate_type'))
                return [('block', block_bbox)] if block_bbox else []
        return []

    def _handle_right_editor_ocr_scroll(self, editor, idx):
        # New Logic: Char based mapping using to_py_pos and ocr_char_map
        text = editor.toPlainText()
        py_idx = to_py_pos(text, idx)
        
        if not hasattr(self, 'ocr_char_map'): return
        
        entries = self.find_ocr_bbox_entries_for_index(py_idx)
        if entries:
            self.image_view.set_highlight_bboxes(
                [(x1, y1, x2 - x1, y2 - y1, level) for level, (x1, y1, x2, y2) in entries]
            )
            return

    def on_cursor_left(self):
        if self._is_syncing_cursor: return
        self._is_syncing_cursor = True
        try:
            idx = self.edit_left.textCursor().position()
            self.edit_left.highlight_line_at_index(idx)
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
            self.edit_right.highlight_line_at_index(idx)
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

    def goto_page(self, page_num):
        try:
            page_num = int(page_num)
        except (TypeError, ValueError):
            return
        if str(page_num) != self.spin_page.text():
            self.spin_page.setText(str(page_num))
            self.jump_page()
            QApplication.processEvents()

    def goto_headword(self, row, preferred_side="left"):
        if not row:
            return

        side = preferred_side
        span = row.get(f"{side}_span")
        if not span:
            side = "right" if side == "left" else "left"
            span = row.get(f"{side}_span")
        if not span:
            return

        if side == "right" and not self.is_text_source_selected():
            self.select_text_source()
            QApplication.processEvents()
        self.goto_page(row.get("page"))

        editor = self.edit_left if side == "left" else self.edit_right
        text = editor.toPlainText()
        start_py, end_py = span
        start_qt = to_qt_pos(text, start_py)
        end_qt = to_qt_pos(text, end_py)
        cursor = editor.textCursor()
        try:
            cursor.setPosition(start_qt)
            cursor.setPosition(end_qt, QTextCursor.MoveMode.KeepAnchor)
            editor.setTextCursor(cursor)
            editor.ensureCursorVisible()
            editor.setFocus()
        except Exception:
            pass

    def goto_report_issue(self, row):
        if not row:
            return

        side = row.get("side", "left")
        if side == "right" and not self.is_text_source_selected():
            self.select_text_source()
            QApplication.processEvents()
        candidates = list(row.get("search_candidates") or [])
        data = row.get("data") or {}
        is_example_report = (
            row.get("source_file") == "example_match_report.tsv"
            or bool(data.get("json_example"))
        )
        for value in (
            data.get("search_text"),
            data.get("context"),
            data.get("line_context"),
            data.get("txt_line"),
            data.get("old_txt_example"),
            data.get("new_txt_example"),
            data.get("txt_preview"),
        ):
            value = str(value or "").strip()
            if value and value not in candidates:
                candidates.append(value)
        if is_example_report:
            example_candidates = []
            for value in (
                data.get("best_txt_candidate"),
                data.get("search_text"),
                data.get("old_txt_example"),
                data.get("new_txt_example"),
            ):
                value = str(value or "").strip()
                if not value:
                    continue
                japanese = value.split("/", 1)[0].strip()
                if japanese and japanese not in example_candidates:
                    example_candidates.append(japanese)
                if value not in example_candidates:
                    example_candidates.append(value)
            candidates = example_candidates + [
                value for value in candidates
                if value not in example_candidates and value != row.get("headword")
            ]
        if row.get("headword") and row["headword"] not in candidates:
            candidates.append(row["headword"])

        page = row.get("page")
        if page is None:
            pages = self.pages_right_text if side == "right" else self.pages_left
            for candidate in candidates:
                candidate = str(candidate or "").strip()
                if not candidate:
                    continue
                for candidate_page, page_text in pages.items():
                    if candidate in page_text:
                        page = candidate_page
                        break
                if page is not None:
                    break
        if page is None:
            self.statusBar().showMessage("报告项目没有页码，且未能在文本中找到候选内容。", 5000)
            return

        self.goto_page(page)
        editor = self.edit_right if side == "right" else self.edit_left
        text = editor.toPlainText()
        match_span = None
        for candidate in candidates:
            candidate = str(candidate or "").strip()
            if not candidate:
                continue
            position = text.find(candidate)
            if position >= 0:
                match_span = (position, position + len(candidate))
                break

        if match_span is None:
            def normalize(value):
                value = re.sub(r"\{[^{}]*\}", "", value or "")
                return re.sub(r"[\s・･▶>【】\[\]（）()]+", "", value).casefold()

            targets = [normalize(value) for value in candidates if normalize(str(value or ""))]
            best = (0.0, None)
            offset = 0
            for line in text.splitlines(True):
                normalized_line = normalize(line)
                for target in targets:
                    if target in normalized_line or normalized_line in target:
                        score = min(len(target), len(normalized_line)) / max(len(target), len(normalized_line), 1)
                    else:
                        score = difflib.SequenceMatcher(None, target, normalized_line, autojunk=False).ratio()
                    if score > best[0]:
                        best = (score, (offset, offset + len(line.rstrip("\r\n"))))
                offset += len(line)
            if best[0] >= 0.42:
                match_span = best[1]

        if match_span:
            start_qt = to_qt_pos(text, match_span[0])
            end_qt = to_qt_pos(text, match_span[1])
            cursor = editor.textCursor()
            cursor.setPosition(start_qt)
            cursor.setPosition(end_qt, QTextCursor.MoveMode.KeepAnchor)
            editor.setTextCursor(cursor)
            editor.ensureCursorVisible()
            editor.setFocus()
            self.statusBar().showMessage(
                f"已定位报告项目：第 {page} 页 {row.get('headword', '')}",
                5000,
            )
        else:
            editor.setFocus()
            self.statusBar().showMessage(
                f"已跳到第 {page} 页，但未找到报告中的词头或候选文本。",
                5000,
            )

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

    def save_right_data(self, candidate_index=None, force=False):
        if not force and not self.is_text_source_selected():
            QMessageBox.warning(self, "Error", "Right side is not a text file.")
            return False

        if candidate_index is None:
            candidate_index = getattr(self, "current_right_candidate_index", 0)
        pages = self.right_candidate_pages.get(candidate_index, self.pages_right_text)
        candidates = getattr(self, "right_text_candidates", [])
        path = candidates[candidate_index].get("path", "") if 0 <= candidate_index < len(candidates) else ""
        if not path:
             path, _ = QFileDialog.getSaveFileName(self, "Save Right", "", "Text (*.txt)")
             if path:
                 if candidate_index == 0:
                     self.project_config['text_path_right'] = path
                 else:
                     self.right_text_candidates[candidate_index]["path"] = path
                     if "right_text_candidates" in self.project_config and candidate_index - 1 < len(self.project_config["right_text_candidates"]):
                         self.project_config["right_text_candidates"][candidate_index - 1]["path"] = path
        
        if path:
            write_pages_to_file(pages, path)
            self.dirty_pages_right.get(candidate_index, set()).clear()
            QMessageBox.information(self, "保存", f"Right data saved to {path}")
            self.config_manager.save()
            return True
        return False

    def save_all_dirty_right_data(self):
        for idx, pages in list(self.dirty_pages_right.items()):
            if pages:
                self.save_right_data(idx, force=True)

    def run_batch_ocr(self):
        """批量 OCR / Cancel"""
        # Toggle Logic: Cancel
        if hasattr(self, 'ocr_thread') and self.ocr_thread and self.ocr_thread.isRunning():
            self.ocr_thread.stop()
            self.btn_batch.setText("Stopping...")
            self.btn_batch.setEnabled(False)
            return

        # Check prereqs based on engine
        engine = canonical_engine_id(self.global_config.get("ocr_engine", "remote"))
        if self.combo_ocr_engine.currentData() is None:
            QMessageBox.warning(self, "Config", "No configured OCR engine is available.")
            return
        
        if engine == PADDLE_ENGINE_ID:
            token = self.global_config.get("ocr_api_token")
            if not token:
                QMessageBox.warning(self, "Config", "Missing Token for Remote OCR (set in Settings)")
                return
        elif engine == "textin":
            cfg = self.global_config.get("ocr_engines", {}).get("textin", {})
            if not cfg.get("app_id") or not cfg.get("secret_code"):
                QMessageBox.warning(self, "Config", "Missing Textin App ID or Secret Code")
                return
        elif engine == "quark":
            cfg = self.global_config.get("ocr_engines", {}).get("quark", {})
            if not cfg.get("client_id") or not cfg.get("client_secret"):
                QMessageBox.warning(self, "Config", "Missing Quark Client ID or Client Secret")
                return
        elif engine == "mineru":
            cfg = self.global_config.get("ocr_engines", {}).get("mineru", {})
            if not cfg.get("token"):
                QMessageBox.warning(self, "Config", "Missing MinerU Token")
                return
        elif engine == "local":
            if not any(e['id'] == 'local' for e in get_available_engines()):
                QMessageBox.warning(self, "Config", "Local OCR module missing")
                return

            
        start = self.project_config.get("start_page", 1)
        end = self.project_config.get("end_page", 100)
        
        missing_pages = []
        save_dir = self.project_config.get("ocr_json_path", "ocr_results")
        from ocr.ocr_engines import get_result_path, get_legacy_result_paths
        
        for p in range(start, end + 1):
            real_page_num = p + self.project_config.get("page_offset", 0)
            engine_result_path = get_result_path(save_dir, real_page_num, engine, self.global_config)
            legacy_paths = get_legacy_result_paths(save_dir, real_page_num) if engine == PADDLE_ENGINE_ID else []
            if not os.path.exists(engine_result_path) and not any(os.path.exists(path) for path in legacy_paths):
                missing_pages.append(p)
                
        if not missing_pages:
            QMessageBox.information(self, "Info", "No missing OCR pages found.")
            return

        ocr_pages = []
        skipped_pages = []
        for p in missing_pages:
            real_page_num = p + self.project_config.get("page_offset", 0)
            if get_page_image(self.doc, self.project_config.get('image_dir'), real_page_num):
                ocr_pages.append(p)
            else:
                skipped_pages.append(p)

        if skipped_pages:
            self.statusBar().showMessage(f"Skipped {len(skipped_pages)} pages without images.")

        if not ocr_pages:
            QMessageBox.information(self, "Info", "No page images found. OCR task was not started.")
            return
            
        # Direct Start with Cancel Option
        self.btn_batch.setText("Cancel OCR")
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, len(ocr_pages))
        self.progress_bar.setValue(0)
        
        self.start_ocr_thread('batch', ocr_pages)

    def start_ocr_thread(self, mode, pages):
        # OCRWorker(mode, pages, project_config, global_config, engine, pdf_path)
        engine = canonical_engine_id(self.global_config.get("ocr_engine", "remote"))
        
        # Ensure pdf path is passed properly or handled in thread
        pdf_path = self.project_config.get('pdf_path')
        
        worker = OCRWorker(mode, pages, self.project_config, self.global_config, engine)
        worker.project_name = self.project_config.get("name") # Tag with project name
        worker.progress.connect(self.on_ocr_progress)
        worker.page_done.connect(self.on_ocr_page_done)
        # 显式捕获 worker，避免页面切换后依赖 QObject.sender() 丢失任务上下文。
        worker.finished.connect(
            lambda success, msg, completed_worker=worker:
                self.on_ocr_finished(success, msg, completed_worker)
        )
        
        self.ocr_thread = worker
        self.ocr_thread.start()

    def on_ocr_progress(self, msg):
        worker = self.sender()
        if not worker: return
        
        # Display global progress with project context
        proj_name = getattr(worker, 'project_name', 'Unknown')
        self.statusBar().showMessage(f"[{proj_name}] {msg}")

    def on_ocr_page_done(self, done: int, total: int):
        """Slot for accurate page-level progress bar updates."""
        self.progress_bar.setValue(done)

    def on_ocr_finished(self, success, msg, worker=None):
        worker = worker or self.sender() or getattr(self, 'ocr_thread', None)
        if not worker:
            return

        is_current_project = (getattr(worker, 'project_name', None) == self.project_config.get("name"))
        is_active_worker = getattr(self, 'ocr_thread', None) is worker

        # OCR 状态属于任务而不是启动任务时所在的页面，切页后也必须复位。
        if is_active_worker:
            if hasattr(self, 'btn_batch'):
                self.btn_batch.setText("OCR所有缺失页面")
                self.btn_batch.setEnabled(True)
            self.progress_bar.setVisible(False)
            self.ocr_thread = None

        if worker.mode == 'single':
            QApplication.restoreOverrideCursor()

        if not is_current_project:
            print(f"Background OCR finished for {getattr(worker, 'project_name', 'Unknown')}")
            return

        self.statusBar().showMessage(msg, 5000)
        if success:
            if worker.mode == 'single':
                # 只刷新当前正在浏览的页面，不强制跳回任务启动页。
                self._suppress_next_load_similarity_status = True
                try:
                    current_page = int(self.spin_page.text())
                    self.refresh_ocr_source_options(current_page)
                except Exception:
                    pass
                self.load_current_page()
            else:
                QMessageBox.information(self, "Batch Done", msg)
        elif "Program interrupted" not in msg: # Don't error on manual stop
            QMessageBox.critical(self, "OCR Failed", msg)


            


    def on_ocr_engine_changed(self):
        raw_engine = self.combo_ocr_engine.currentData()
        if not raw_engine:
            self.refresh_ocr_model_combo(None)
            return
        engine = canonical_engine_id(raw_engine)
        self.global_config["ocr_engine"] = engine
        self.config_manager.save()
        self.refresh_ocr_model_combo(engine)

    def on_ocr_model_changed(self):
        model = self.combo_ocr_model.currentData() or self.combo_ocr_model.currentText()
        raw_engine = self.combo_ocr_engine.currentData()
        if not raw_engine:
            return
        engine = canonical_engine_id(raw_engine)
        if engine == PADDLE_ENGINE_ID:
            self.global_config["ocr_api_model"] = model
            refresh_remote_engine_label(model)
            idx = self.combo_ocr_engine.findData(PADDLE_ENGINE_ID)
            if idx >= 0:
                self.combo_ocr_engine.setItemText(idx, "PaddleOCR")
        elif engine == "mineru":
            self.global_config.setdefault("ocr_engines", {}).setdefault("mineru", {})["model_version"] = model
        elif engine in ("textin", "quark", "chrome_lens"):
            self.global_config.setdefault("ocr_engines", {}).setdefault(engine, {})["model"] = model
        self.config_manager.save()

    def refresh_ocr_engine_combo(self):
        if not hasattr(self, "combo_ocr_engine"):
            return
        current_engine = canonical_engine_id(self.global_config.get("ocr_engine", "remote"))
        self.combo_ocr_engine.blockSignals(True)
        self.combo_ocr_engine.clear()
        available_engines = get_available_engines(self.global_config)
        for engine in available_engines:
            self.combo_ocr_engine.addItem(engine['label'], engine['id'])
        if not available_engines:
            self.combo_ocr_engine.addItem("未配置 OCR 引擎", None)
        idx = self.combo_ocr_engine.findData(current_engine)
        if idx < 0:
            idx = 0
            if available_engines:
                self.global_config["ocr_engine"] = self.combo_ocr_engine.itemData(0)
                self.config_manager.save()
        self.combo_ocr_engine.setCurrentIndex(idx)
        self.combo_ocr_engine.blockSignals(False)
        self.btn_ocr_cur.setEnabled(bool(available_engines))
        self.btn_batch.setEnabled(bool(available_engines))
        self.refresh_ocr_model_combo(self.combo_ocr_engine.currentData())

    def refresh_ocr_model_combo(self, engine):
        engine = canonical_engine_id(engine)
        self.combo_ocr_model.blockSignals(True)
        self.combo_ocr_model.clear()

        if engine == PADDLE_ENGINE_ID:
            saved = self.global_config.get("ocr_api_model", V2_MODELS[0])
            for model in V2_MODELS:
                self.combo_ocr_model.addItem(model, model)
                self.combo_ocr_model.setItemData(self.combo_ocr_model.count() - 1, model, Qt.ItemDataRole.ToolTipRole)
            idx = self.combo_ocr_model.findData(saved)
        elif engine == "mineru":
            models = ["vlm", "pipeline"]
            saved = self.global_config.get("ocr_engines", {}).get("mineru", {}).get("model_version", "vlm")
            for model in models:
                self.combo_ocr_model.addItem(model, model)
                self.combo_ocr_model.setItemData(self.combo_ocr_model.count() - 1, model, Qt.ItemDataRole.ToolTipRole)
            idx = self.combo_ocr_model.findData(saved)
        elif engine == "textin":
            saved = self.global_config.get("ocr_engines", {}).get("textin", {}).get("model", "默认")
            self.combo_ocr_model.addItem("默认", "默认")
            self.combo_ocr_model.setItemData(0, "默认", Qt.ItemDataRole.ToolTipRole)
            idx = self.combo_ocr_model.findData(saved)
        elif engine == "quark":
            saved = self.global_config.get("ocr_engines", {}).get("quark", {}).get("model", "RecognizeGeneralDocument")
            self.combo_ocr_model.addItem("RecognizeGeneralDocument", "RecognizeGeneralDocument")
            self.combo_ocr_model.setItemData(0, "RecognizeGeneralDocument", Qt.ItemDataRole.ToolTipRole)
            idx = self.combo_ocr_model.findData(saved)
        elif engine == "chrome_lens":
            saved = self.global_config.get("ocr_engines", {}).get("chrome_lens", {}).get("model", "默认")
            self.combo_ocr_model.addItem("默认", "默认")
            self.combo_ocr_model.setItemData(0, "默认", Qt.ItemDataRole.ToolTipRole)
            idx = self.combo_ocr_model.findData(saved)
        else:
            self.combo_ocr_model.addItem("默认", "默认")
            self.combo_ocr_model.setItemData(0, "默认", Qt.ItemDataRole.ToolTipRole)
            idx = 0

        if idx is None or idx < 0:
            idx = 0
        self.combo_ocr_model.setCurrentIndex(idx)
        self.combo_ocr_model.blockSignals(False)

    def run_current_ocr_unified(self):
        """Unified entry point for Single Page OCR"""
        try:
            page_num = int(self.spin_page.text())
        except: return
        if self.combo_ocr_engine.currentData() is None:
            QMessageBox.warning(self, "Config", "No configured OCR engine is available.")
            return

        real_page_num = page_num + self.project_config.get("page_offset", 0)
        doc = self.doc
        should_close_doc = False
        if not doc:
            pdf_path = self.project_config.get('pdf_path')
            if pdf_path and os.path.exists(pdf_path):
                try:
                    doc = fitz.open(pdf_path)
                    should_close_doc = True
                except Exception:
                    doc = None
        try:
            if not get_page_image(
                doc,
                self.project_config.get('image_dir'),
                real_page_num,
            ):
                QMessageBox.warning(self, "OCR", f"No image found for page {page_num}.")
                return
        finally:
            if should_close_doc and doc:
                doc.close()
        
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
        dlg.settings_changed.connect(self.on_settings_changed)
        dlg.exec()
        
        # After close: Config might have changed
        self.global_config = self.config_manager.get_global()
        self.project_config = self.config_manager.get_active_project()

        self.update_project_combo()
        self.setup_shortcuts()
        self.reload_all_data()

    def on_settings_changed(self):
        self.global_config = self.config_manager.get_global()
        self.refresh_ocr_engine_combo()

    def on_editor_focus(self):
        self.last_active_editor = self.sender()
        
    def on_image_bbox_click(self, ocr_idx, local_offset=0):
        """Handle Ctrl+Click on Image BBox"""
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
        start_py_idx = data['start_index'] + max(
            0,
            min(int(local_offset or 0), data['end_index'] - data['start_index']),
        )

        # 图片反向定位文本时也保留当前细粒度矩形框。
        entries = self.find_ocr_bbox_entries_for_index(start_py_idx)
        if entries:
            self.image_view.set_highlight_bboxes(
                [
                    (x1, y1, x2 - x1, y2 - y1, level)
                    for level, (x1, y1, x2, y2) in entries
                ],
                ensure_visible=False,
            )
        
        # Determine target
        target = self.last_active_editor
        if not target: target = self.edit_left # Default
        
        target_pos = -1
        
        if target == self.edit_right and not self.is_text_source_selected():
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
                # 图片反向定位时两侧都显示醒目的行高亮，避免只靠细光标辨认。
                target.highlight_line_at_index(target_pos)
                self.request_highlight_other(target, target_pos)
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

    def capture_global_state(self, description):
        import copy
        return {
            'time': time.time(),
            'desc': description,
            'left': copy.deepcopy(self.pages_left),
            'right': copy.deepcopy(self.pages_right_text),
            'right_candidates': copy.deepcopy(getattr(self, 'right_candidate_pages', {})),
            'active_right_candidate': getattr(self, 'current_right_candidate_index', 0),
        }

    @staticmethod
    def global_states_equal(left, right):
        if not left or not right:
            return False
        return (
            left.get('left') == right.get('left')
            and left.get('right_candidates', left.get('right'))
            == right.get('right_candidates', right.get('right'))
        )

    def push_global_undo(self, description="Global Action"):
        """开始一次全局操作，并记录操作前的实际状态。"""
        before = self.capture_global_state(f"{description} 前")
        if self.global_history_index < len(self.global_undo_stack) - 1:
            del self.global_undo_stack[self.global_history_index + 1:]
        current = (
            self.global_undo_stack[self.global_history_index]
            if 0 <= self.global_history_index < len(self.global_undo_stack)
            else None
        )
        if not self.global_states_equal(current, before):
            self.global_undo_stack.append(before)
            self.global_history_index = len(self.global_undo_stack) - 1
        self._pending_global_description = description

    def finalize_global_action(self, changed=True):
        """记录全局操作后的状态，使历史支持撤销、重做和任意恢复。"""
        description = self._pending_global_description
        self._pending_global_description = None
        if not changed or not description:
            return
        self.global_undo_stack.append(self.capture_global_state(description))
        self.global_history_index = len(self.global_undo_stack) - 1
        if len(self.global_undo_stack) > 30:
            overflow = len(self.global_undo_stack) - 30
            del self.global_undo_stack[:overflow]
            self.global_history_index -= overflow
        if self.global_history_dialog and self.global_history_dialog.isVisible():
            self.global_history_dialog.refresh()

    def apply_global_snapshot(self, snapshot):
        import copy
        self.pages_left = copy.deepcopy(snapshot.get('left', {}))
        if snapshot.get('right_candidates') is not None:
            self.right_candidate_pages = copy.deepcopy(snapshot['right_candidates'])
            active = int(snapshot.get('active_right_candidate', self.current_right_candidate_index) or 0)
            self.current_right_candidate_index = min(active, max(0, len(self.right_text_candidates) - 1))
            self.pages_right_text = self.right_candidate_pages.get(self.current_right_candidate_index, {})
        else:
            self.pages_right_text = copy.deepcopy(snapshot.get('right', {}))
            self.right_candidate_pages[self.current_right_candidate_index] = self.pages_right_text

        self.dirty_pages_left.update(self.pages_left.keys())
        for candidate, pages in self.right_candidate_pages.items():
            self.dirty_pages_right.setdefault(candidate, set()).update(pages.keys())
        self.force_ui_reload()

    def restore_global_history_index(self, index, confirm=True):
        if not (0 <= index < len(self.global_undo_stack)):
            return False
        if index == self.global_history_index:
            return True
        if confirm:
            reply = QMessageBox.warning(
                self,
                "恢复全局替换状态",
                "恢复会覆盖当前内存中的文本状态，但不会立即写入文件。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return False
        self.save_current_page_data()
        self.apply_global_snapshot(self.global_undo_stack[index])
        self.global_history_index = index
        self.statusBar().showMessage(
            f"已恢复：{self.global_undo_stack[index].get('desc', '全局状态')}", 5000
        )
        return True

    def undo_global(self):
        if self.global_history_index <= 0:
            QMessageBox.information(self, "撤销", "没有可撤销的全局替换。")
            return False
        return self.restore_global_history_index(self.global_history_index - 1)

    def redo_global(self):
        if self.global_history_index < 0 or self.global_history_index >= len(self.global_undo_stack) - 1:
            QMessageBox.information(self, "重做", "没有可重做的全局替换。")
            return False
        return self.restore_global_history_index(self.global_history_index + 1)

    def show_global_replace_history(self):
        if not self.global_history_dialog:
            self.global_history_dialog = GlobalReplaceHistoryDialog(self)
        self.global_history_dialog.refresh()
        self.global_history_dialog.show()
        self.global_history_dialog.raise_()
        self.global_history_dialog.activateWindow()

# ==========================================
# 6. Utility Functions Dialogs
# ==========================================


# (Extracted to tools/pdf_tools.py and tools/text_tools.py)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    
    # 全局字体设置，防止显示过小
    font = QFont("Microsoft YaHei", 10)
    app.setFont(font)
    
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
