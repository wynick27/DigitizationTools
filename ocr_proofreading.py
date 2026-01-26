import sys
import os
import json
import re
import fitz  # PyMuPDF
import difflib
import requests
import base64

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                             QSplitter, QTextEdit, QLabel, QToolBar, QFileDialog, 
                             QMessageBox, QLineEdit, QPushButton, QComboBox, QCheckBox,
                             QGraphicsView, QGraphicsScene, QGraphicsPixmapItem, QGraphicsRectItem)
from PyQt6.QtGui import (QAction, QColor, QFont, QImage, QPixmap, QPen, QTextCursor, 
                         QTextCharFormat, QCursor, QTextFormat)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QEvent

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

DEFAULT_CONFIG = {
    "pdf_path": "",
    "image_dir": "",
    "start_page": 1,
    "end_page": 1,
    "page_offset": 0,
    "text_path_left": "",
    "text_path_right": "", # 第二版本文本
    "ocr_json_path": "",       # OCR 数据目录
    "regex_left": r"^\*\*(.*?)\*\*",
    "regex_right": r"^([a-zA-Z]*?)",
    "use_pdf_render": False,
    "ocr_api_url": "", # Remote OCR URL
    "ocr_api_token": "", # Remote OCR Token
}

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
# 1. 自定义编辑器 (支持 Diff 交互)
# ==========================================

class DiffTextEdit(QTextEdit):
    """
    支持 Ctrl+Hover 高亮和 Ctrl+Click 应用补丁的文本框
    """
    # 信号：点击了某个 Diff 块，请求应用到另一侧 (self_index_range, target_text)
    apply_patch_signal = pyqtSignal(tuple, str)
    # 信号：Alt+Click 将本侧内容推送到另一侧 (target_range, my_content)
    push_patch_signal = pyqtSignal(tuple, str)
    
    def __init__(self, side="left"):
        super().__init__()
        self.side = side # 'left' or 'right'
        self.diff_opcodes = [] # 存储 difflib 的 opcodes
        self.other_text_content = "" # 另一侧的完整文本，用于提取
        self.setFont(QFont("Consolas", 11))
        
        # 启用鼠标追踪以支持 Hover
        self.setMouseTracking(True)
        self._hovering_diff = False

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
        selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
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
        idx = cursor.position()
        
        # 遍历 opcodes 查找当前索引是否在差异区间内
        for tag, i1, i2, j1, j2 in self.diff_opcodes:
            if tag == 'equal': continue
            
            # 判断是在左侧还是右侧
            if self.side == 'left':
                # 左侧关注 i1, i2
                # 对于 insert (左侧为空)，范围是 i1==i2，鼠标很难点中，需要容错？
                # 这里主要处理 replace/delete
                if i1 <= idx <= i2:
                    return (tag, i1, i2, j1, j2)
            else:
                # 右侧关注 j1, j2
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
        if pixmap:
            self.scene.addPixmap(pixmap)
            self.setSceneRect(0, 0, pixmap.width(), pixmap.height())
            if ocr_data:
                self.draw_bboxes(ocr_data)
        self.scale_factor = 1.0
        self.resetTransform()
        
    def draw_bboxes(self, ocr_data):
        pen = QPen(QColor(255, 0, 0, 200))
        pen.setWidth(2)
        
        for item in ocr_data:
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
            self.scene.addItem(rect)

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
                pass
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
        filename, _ = QFileDialog.getOpenFileName(self, "Open File", "", "Text Files (*.txt);;All Files (*)")
        if filename:
            self.set_path(filename)
            # Update config and reload
            if self.side == "left":
                self.main_window.config['text_path_left'] = filename
            else:
                self.main_window.config['text_path_right'] = filename
            self.main_window.save_config()
            self.main_window.reload_all_data()

    def save_file(self):
        if self.side == "left":
            self.main_window.save_left_data()
        else:
            # Maybe save right data?
            QMessageBox.information(self, "Info", "Saving right side not fully implemented yet (depends on source type).")


# ==========================================
# 3. 主窗口
# ==========================================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("OCR 校对工具 v4 (PyQt6 Refactor)")
        self.resize(1600, 900)
        
        self.config = DEFAULT_CONFIG.copy()
        self.load_config()
        
        # 数据缓存
        self.pages_left = {}  # {page_num: text}
        self.pages_right_text = {} # {page_num: text} (Data Source 2)
        self.current_ocr_data = [] 
        
        self.doc = None # PDF Document
        
        # 初始化界面
        self.init_ui()
        
        # 加载数据
        self.reload_all_data()
        
    def load_config(self):
        if os.path.exists("config.json"):
            try:
                with open("config.json", "r", encoding='utf-8') as f:
                    self.config.update(json.load(f))
            except: pass
            
    def save_config(self):
        with open("config.json", "w", encoding='utf-8') as f:
            json.dump(self.config, f, indent=4)

    def init_ui(self):
        # --- 工具栏 ---
        toolbar = QToolBar()
        self.addToolBar(toolbar)
        
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
        
        # 本地 OCR 按钮
        if HAS_LOCAL_OCR:
            btn_ocr = QPushButton("运行本地OCR")
            btn_ocr.clicked.connect(self.run_local_ocr)
            toolbar.addWidget(btn_ocr)
            
        # 远程 OCR 按钮 (仅当配置存在时)
        if self.config.get("ocr_api_url") and self.config.get("ocr_api_token"):
            btn_remote_ocr = QPushButton("运行远程OCR")
            btn_remote_ocr.clicked.connect(self.run_remote_ocr)
            toolbar.addWidget(btn_remote_ocr)
        
        toolbar.addSeparator()
        
        # 保存按钮
        btn_save = QPushButton("保存左侧")
        btn_save.clicked.connect(self.save_left_data)
        toolbar.addWidget(btn_save)
        
        btn_export = QPushButton("导出切图")
        btn_export.clicked.connect(self.export_slices)
        toolbar.addWidget(btn_export)

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
        self.regex_input_left.setText(self.config.get("regex_left", ""))
        self.regex_input_left.editingFinished.connect(self.on_regex_changed)
        
        self.regex_input_right = QLineEdit()
        self.regex_input_right.setPlaceholderText("右侧词头正则")
        self.regex_input_right.setText(self.config.get("regex_right", ""))
        self.regex_input_right.editingFinished.connect(self.on_regex_changed)
        
        regex_layout.addWidget(QLabel("L正则:"))
        regex_layout.addWidget(self.regex_input_left)
        regex_layout.addWidget(QLabel("R正则:"))
        regex_layout.addWidget(self.regex_input_right)
        
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
        
        # 绑定信号
        self.edit_left.textChanged.connect(self.on_text_changed)
        self.edit_right.textChanged.connect(self.on_text_changed)
        
        # 绑定 Patch 信号
        self.edit_left.apply_patch_signal.connect(lambda r, t: self.apply_patch(self.edit_left, r, t))
        self.edit_right.apply_patch_signal.connect(lambda r, t: self.apply_patch(self.edit_right, r, t))

        # 绑定 Push Patch 信号 (Alt+Click) : 源自 Left -> 改 Right
        self.edit_left.push_patch_signal.connect(lambda r, t: self.apply_patch(self.edit_right, r, t))
        self.edit_right.push_patch_signal.connect(lambda r, t: self.apply_patch(self.edit_left, r, t))
        
        # 绑定滚动同步 (屏蔽默认)
        # self.edit_left.verticalScrollBar().valueChanged.connect(self.sync_scroll_to_right)
        # self.edit_right.verticalScrollBar().valueChanged.connect(self.sync_scroll_to_left)
        # 使用自定义的滚动监听，因为需要判断是否由用户触发
        self.edit_left.verticalScrollBar().valueChanged.connect(lambda v: self.on_scroll(self.edit_left, self.edit_right))
        self.edit_right.verticalScrollBar().valueChanged.connect(lambda v: self.on_scroll(self.edit_right, self.edit_left))
        
        # 绑定光标移动 (高亮对齐 & 自动滚动)
        self.edit_left.cursorPositionChanged.connect(self.on_cursor_left)
        self.edit_right.cursorPositionChanged.connect(self.on_cursor_right)
        
        # 标记是否正在编程滚动，防止死循环
        self._is_program_scrolling = False

        text_splitter.addWidget(left_container)
        text_splitter.addWidget(right_container_widget)
        right_layout.addWidget(text_splitter)
        
        splitter.addWidget(right_container)
        splitter.setSizes([600, 1000]) # 初始比例

    # ================= 逻辑处理 =================

    def reload_all_data(self):
        # 1. 加载文本
        self.pages_left = read_text_to_pages(self.config['text_path_left'])
        self.pages_right_text = read_text_to_pages(self.config['text_path_right'])
        
        # 2. 加载 PDF
        if self.config['pdf_path'] and os.path.exists(self.config['pdf_path']):
            try:
                self.doc = fitz.open(self.config['pdf_path'])
            except:
                self.doc = None
        
        # 3. Update Headers
        self.header_left.set_path(self.config.get('text_path_left', ''))
        self.header_right.set_path(self.config.get('text_path_right', ''))

        self.load_current_page()

    def load_current_page(self):
        page_num = self.config.get('start_page', 1)
        try:
            page_num = int(self.spin_page.text())
        except:
            pass
        
        # 界面同步
        self.spin_page.setText(str(page_num))
        
        # 1. 显示图片 (如果有)
        pix = self.get_page_pixmap(page_num)
        
        # 2. 准备 OCR 数据 (Always Load if valid)
        ocr_data = self.load_ocr_json(page_num)
        self.current_ocr_data = ocr_data
        
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
        self.image_view.load_content(pix, ocr_data if ocr_data else [])
        
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

        # 避免触发 textChanged 导致死循环
        self.edit_left.blockSignals(True)
        self.edit_right.blockSignals(True)
        
        self.edit_left.setPlainText(left_text)
        self.edit_right.setPlainText(right_text)
        
        self.edit_left.blockSignals(False)
        self.edit_right.blockSignals(False)
        
        # 4. 执行对比
        self.run_diff()
        
        # 5. 计算 OCR 对齐 (Diff: Left <-> OCR_Full)
        # 如果当前不是 OCR 模式，我们需要额外的 Diff 数据来做映射
        if not is_ocr_mode and self.ocr_text_full:
            self.run_ocr_mapping_diff(left_text)
        else:
            self.ocr_diff_opcodes = self.edit_left.diff_opcodes # 复用主 Diff (如果右侧就是OCR)

    def get_page_pixmap(self, page_num):
        """获取图片：优先 PDF，其次图片目录"""
        idx = page_num + self.config['page_offset'] - 1
        
        # PDF
        if self.doc and 0 <= idx < len(self.doc):
            try:
                page = self.doc[idx]
                pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
                # PyMuPDF pixmap -> QImage
                img_format = QImage.Format.Format_RGB888
                if pix.alpha: img_format = QImage.Format.Format_RGBA8888
                
                img = QImage(pix.samples, pix.width, pix.height, pix.stride, img_format)
                return QPixmap.fromImage(img)
            except Exception as e:
                print(e)
        
        # Image Dir
        img_dir = self.config['image_dir']
        if img_dir and os.path.exists(img_dir):
            # 尝试 page_1.jpg 或 1.jpg
            names = [f"page_{idx}", f"{idx}"]
            exts = [".jpg", ".png", ".jpeg"]
            for n in names:
                for e in exts:
                    p = os.path.join(img_dir, n + e)
                    if os.path.exists(p):
                        return QPixmap(p)
        return None

    def load_ocr_json(self, page_num):
        """加载 PaddleOCR 格式 JSON"""
        path = self.config['ocr_json_path']
        f_path = os.path.join(path, f"page_{page_num}.json")
        if not os.path.exists(f_path):
            # 尝试直接数字
            f_path = os.path.join(path, f"{page_num}.json")
        
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
                            if b.get('block_label') in ['text','vertical_text']:
                                res.append({
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

    def run_diff(self):
        text_l = self.edit_left.toPlainText()
        text_r = self.edit_right.toPlainText()
        
        matcher = difflib.SequenceMatcher(None, text_l, text_r, autojunk=False)
        opcodes = matcher.get_opcodes()
        
        # 将 diff 数据传递给编辑器，供交互使用
        self.edit_left.set_diff_data(opcodes, text_r)
        self.edit_right.set_diff_data(opcodes, text_l)
        
        # 渲染颜色
        self.highlight_editor(self.edit_left, opcodes, is_left=True)
        self.highlight_editor(self.edit_right, opcodes, is_left=False)
        
        # 渲染词头正则
        self.highlight_regex(self.edit_left, self.config.get("regex_left"))
        self.highlight_regex(self.edit_right, self.config.get("regex_right"))
        
        # 如果不在 OCR 模式，更新 OCR Mapping
        if self.combo_source.currentText() != "OCR Results" and self.ocr_text_full:
            self.run_ocr_mapping_diff(text_l)


    def highlight_editor(self, editor, opcodes, is_left):
        editor.blockSignals(True)
        
        # 清除旧格式 (保留字体)
        cursor = editor.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor("black"))
        fmt.setBackground(QColor("transparent"))
        cursor.setCharFormat(fmt)
        
        # 应用 Diff 格式
        for tag, i1, i2, j1, j2 in opcodes:
            if tag == 'equal': continue
            
            # 设置颜色：delete=红, insert=红, replace=红
            # 也可以区分颜色，这里按照要求统一标红
            color_fmt = QTextCharFormat()
            color_fmt.setForeground(QColor("red"))
            color_fmt.setBackground(QColor("#FFEEEE")) # 浅红背景
            
            start, end = (i1, i2) if is_left else (j1, j2)
            
            if start == end: 
                # 插入点，难以高亮字符，忽略视觉，但逻辑保留
                pass
            else:
                cursor.setPosition(start)
                cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
                cursor.mergeCharFormat(color_fmt)
                
        editor.blockSignals(False)

    def highlight_regex(self, editor, regex_str):
        if not regex_str: return
        try:
            regex = re.compile(regex_str, re.MULTILINE)
            text = editor.toPlainText()
            
            fmt = QTextCharFormat()
            fmt.setBackground(QColor("#E0F0FF")) # 浅蓝
            
            cursor = editor.textCursor()
            editor.blockSignals(True)
            for match in regex.finditer(text):
                cursor.setPosition(match.start())
                cursor.setPosition(match.end(), QTextCursor.MoveMode.KeepAnchor)
                cursor.mergeCharFormat(fmt)
            editor.blockSignals(False)
        except:
            pass

    # ================= 交互 =================

    def apply_patch(self, editor, rng, target_text):
        """应用 Diff 补丁：将 range 区间的内容替换为 target_text"""
        start, end = rng
        cursor = editor.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(target_text)
        # 插入后 textChanged 会触发，自动重新 diff

    def on_text_changed(self):
        # 实时保存到内存
        try:
            page_num = int(self.spin_page.text())
            self.pages_left[page_num] = self.edit_left.toPlainText()
            if self.combo_source.currentText() == "Text File B":
                self.pages_right_text[page_num] = self.edit_right.toPlainText()
        except: pass
        
        # 重新运行 diff (可以加 Timer 防抖)
        self.run_diff()

    def on_regex_changed(self):
        self.config["regex_left"] = self.regex_input_left.text()
        self.config["regex_right"] = self.regex_input_right.text()
        self.save_config()
        self.run_diff()

    # ================= 交互增强 (Sync & Highlight) =================

    def get_mapped_index(self, idx, is_left_source):
        """获取索引映射"""
        opcodes = self.edit_left.diff_opcodes
        mapped_idx = -1
        
        for tag, i1, i2, j1, j2 in opcodes:
            # src range, dst range
            s1, s2 = (i1, i2) if is_left_source else (j1, j2)
            d1, d2 = (j1, j2) if is_left_source else (i1, i2)
            
            if s1 <= idx <= s2:
                if tag == 'equal':
                    offset = idx - s1
                    mapped_idx = d1 + offset
                    if mapped_idx > d2: mapped_idx = d2
                else:
                    mapped_idx = d1
                break
        return mapped_idx

    def on_scroll(self, source, target):
        """基于内容的对齐滚动"""
        if self._is_program_scrolling: return
        
        # 获取 Source 视口最顶端的字符索引
        # cursorForPosition(0,0) 获取的是 visual line 的开始
        # 为了更准确，可以取一点 margin，比如 (5, 5)
        top_cursor = source.cursorForPosition(source.viewport().rect().topLeft())
        src_idx = top_cursor.position()
        
        is_left = (source == self.edit_left)
        
        # 映射到 Target
        dst_idx = self.get_mapped_index(src_idx, is_left)
        
        if dst_idx >= 0:
            self._is_program_scrolling = True
            
            # 计算目标位置的 Y 坐标
            # 方法：找到 dst_idx 所在的 block，获取其 bounding rect
            doc = target.document()
            block = doc.findBlock(dst_idx)
            layout = doc.documentLayout()
            
            # blockBoundingRect 返回的是相对于文档的坐标
            block_rect = layout.blockBoundingRect(block)
            
            # 也可以更精细：如果是 wrap 过的长行，blockBoundingRect 是整个 block 的
            # 我们只需要大致对齐 block 即可
            target_y = block_rect.y()
            
            target.verticalScrollBar().setValue(int(target_y))
            
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
             src_idx = idx
             
             # Map src_idx to ocr_idx
             for tag, i1, i2, j1, j2 in opcodes:
                 if i1 <= src_idx <= i2:
                     if tag == 'equal':
                         target_ocr_idx = j1 + (src_idx - i1)
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
                         # 假设是 x1,y1,x2,y2 (LayoutParser) 
                         # 或者 x,y,w,h (Paddle Dict)?
                         # 需要根据 load_ocr_json 的实际输出来定。
                         # 查看 load_ocr_json:
                         #   Paddle Dict: 'bbox': [x1, y1, x2, y2]
                         #   Paddle List: calculated [minx, miny, maxx, maxy]
                         # So it is consistently [x1, y1, x2, y2] in my code logic above (lines 535-544 modify it logic? Wait)
                         # line 535 logic: `bbox = [min(xs), min(ys), max(xs), max(ys)]` which is [x1, y1, x2, y2]
                         # line 532 logic: `bbox = item.get('bbox', [])`. standard layout parser is [x,y,w,h]? No usually [x1,y1,x2,y2]. 
                         # Let's assume [x1, y1, x2, y2]
                         
                         x, y = bbox[0], bbox[1]
                         w, h = bbox[2]-bbox[0], bbox[3]-bbox[1]
                         
                    self.image_view.set_highlight_bbox(x, y, w, h)
                    return

    def _handle_right_editor_ocr_scroll(self, editor, idx):
        # 原有的逻辑：按行号
        cursor = editor.textCursor()
        line_num = cursor.blockNumber() # 0-indexed
        if 0 <= line_num < len(self.current_ocr_data):
            # ... (Logic to find bbox from current_ocr_data list) ...
            # Reuse logic implicitly via creating a map first? 
            # Actually, let's just reuse the generic map logic if possible, 
            # BUT right editor in OCR mode is strictly line-synced.
            item = self.current_ocr_data[line_num]
            x, y, w, h = 0,0,0,0
            
            # ... Copy paste old logic ...
            if isinstance(item, dict) and 'bbox' in item:
                b = item['bbox']
                x, y, w, h = b[0], b[1], b[2]-b[0], b[3]-b[1]
            elif isinstance(item, list) and len(item) == 2:
                pts = item[0]
                # ...
                xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
                x, y = min(xs), min(ys)
                w, h = max(xs)-x, max(ys)-y
                
            self.image_view.set_highlight_bbox(x, y, w, h)

    def on_cursor_left(self):
        idx = self.edit_left.textCursor().position()
        self.request_highlight_other(self.edit_left, idx)
        # 增加：检查左侧光标对应的 BBox
        self.check_auto_scroll_bbox(self.edit_left, idx)

    def on_cursor_right(self):
        idx = self.edit_right.textCursor().position()
        self.request_highlight_other(self.edit_right, idx)
        self.check_auto_scroll_bbox(self.edit_right, idx)

    # ================= 功能 =================

    def prev_page(self):
        try:
            p = int(self.spin_page.text())
            self.spin_page.setText(str(p - 1))
            self.load_current_page()
        except: pass

    def next_page(self):
        try:
            p = int(self.spin_page.text())
            self.spin_page.setText(str(p + 1))
            self.load_current_page()
        except: pass
        
    def jump_page(self):
        self.load_current_page()

    def save_left_data(self):
        path = self.config['text_path_left']
        write_pages_to_file(self.pages_left, path)
        QMessageBox.information(self, "保存", f"左侧文本已保存至 {path}")

    def run_local_ocr(self):
        if not HAS_LOCAL_OCR: return
        page_num = int(self.spin_page.text())
        img_path = ""
        
        # 导出当前页面图片为临时文件供 OCR
        pix = self.get_page_pixmap(page_num)
        if not pix:
            QMessageBox.warning(self, "Error", "没有图片可供 OCR")
            return
            
        temp_img = "temp_ocr.jpg"
        pix.save(temp_img)
        
        try:
            self.statusBar().showMessage("OCR Running...")
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            
            ocr = PaddleOCRVL()
            result = ocr.predict(temp_img)
            
            # 转换为标准格式并保存
            # result 结构 [[[x,y]..], (text, conf)]
            data = result[0] if result else []
            
            # 保存到 json
            save_dir = self.config['ocr_json_path']
            if not os.path.exists(save_dir): os.makedirs(save_dir)
            json_path = os.path.join(save_dir, f"page_{page_num}.json")
            
            with open(json_path, "w", encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                
            QApplication.restoreOverrideCursor()
            self.statusBar().showMessage("OCR Done.")
            
            # 切换模式并刷新
            self.combo_source.setCurrentText("OCR Results")
            self.load_current_page()
            
        except Exception as e:
            QApplication.restoreOverrideCursor()
            print(e)
            QMessageBox.critical(self, "OCR Error", str(e))

            QMessageBox.critical(self, "OCR Error", str(e))

    def run_remote_ocr(self):
        """运行远程 OCR"""
        api_url = self.config.get("ocr_api_url")
        token = self.config.get("ocr_api_token")
        
        if not api_url or not token:
            QMessageBox.warning(self, "Config Error", "Please configure ocr_api_url and ocr_api_token in config.json")
            return

        page_num = self.spin_page.text()
        try:
            p_int = int(page_num)
        except: return
        
        # 1. Get Image
        pix = self.get_page_pixmap(p_int)
        if not pix:
            QMessageBox.warning(self, "Error", "No image found for this page.")
            return
            
        # 2. Save to buffer/base64
        # We can use QBuffer, but saving to temp file is easier to reuse logic or debug
        temp_img = "temp_remote_ocr.jpg"
        pix.save(temp_img)
        
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self.statusBar().showMessage(f"Running Remote OCR for page {page_num}...")
        
        try:
            # 3. Request
            with open(temp_img, "rb") as file:
                file_bytes = file.read()
                file_data = base64.b64encode(file_bytes).decode("ascii")

            headers = {
                "Authorization": f"token {token}",
                "Content-Type": "application/json"
            }

            payload = {
                "file": file_data,
                "fileType": 1,
                "useDocOrientationClassify": False,
                "useDocUnwarping": False,
                "useChartRecognition": False,
            }

            response = requests.post(api_url, json=payload, headers=headers)
            
            if response.status_code != 200:
                raise Exception(f"Remote Error: {response.text}")
                
            result = response.json().get("result")
            
            # 4. Save result
            save_dir = self.config['ocr_json_path']
            if not os.path.exists(save_dir) and save_dir: 
                try: os.makedirs(save_dir)
                except: pass
                
            if not save_dir:
                save_dir = "ocr_results" # default fallback
                if not os.path.exists(save_dir): os.makedirs(save_dir)
                
            json_path = os.path.join(save_dir, f"page_{page_num}.json")
            with open(json_path, "w", encoding='utf8') as json_file:
                json.dump(result, json_file, ensure_ascii=False, indent=2)
                
            QApplication.restoreOverrideCursor()
            self.statusBar().showMessage("Remote OCR Done.")
            QMessageBox.information(self, "Success", f"Remote OCR saved to {json_path}")
            
            # 5. Reload
            self.combo_source.setCurrentText("OCR Results")
            self.load_current_page()
            
        except Exception as e:
            QApplication.restoreOverrideCursor()
            print(e)
            QMessageBox.critical(self, "Remote OCR Error", str(e))

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

if __name__ == "__main__":
    app = QApplication(sys.argv)
    
    # 全局字体设置，防止显示过小
    font = QFont("Microsoft YaHei", 10)
    app.setFont(font)
    
    window = MainWindow()
    window.show()
    sys.exit(app.exec())