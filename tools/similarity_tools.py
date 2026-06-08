import difflib
import re

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)


def text_similarity(left_text: str, right_text: str) -> float:
    if not left_text and not right_text:
        return 1.0
    return difflib.SequenceMatcher(None, left_text or "", right_text or "", autojunk=False).ratio()


def calculate_page_similarities(pages_left: dict[int, str], pages_right: dict[int, str]) -> list[dict]:
    rows = []
    for page in sorted(set(pages_left.keys()) | set(pages_right.keys())):
        left_text = pages_left.get(page, "")
        right_text = pages_right.get(page, "")
        ratio = text_similarity(left_text, right_text)
        if not left_text and not right_text:
            status = "两侧为空"
        elif not left_text:
            status = "左侧为空"
        elif not right_text:
            status = "右侧为空"
        elif ratio >= 1.0:
            status = "完全相同"
        else:
            status = "有差异"
        rows.append({
            "page": page,
            "ratio": ratio,
            "left_len": len(left_text),
            "right_len": len(right_text),
            "status": status,
        })
    return rows


def similarity_background(ratio: float) -> QColor | None:
    if ratio < 0.60:
        return QColor("#ffdede")
    if ratio < 0.70:
        return QColor("#fff4bf")
    return None


def parse_page_ranges(range_text: str) -> set[int] | None:
    text = (range_text or "").strip()
    if not text:
        return None

    pages = set()
    for part in re.split(r"[,，\s]+", text):
        if not part:
            continue
        match = re.fullmatch(r"(\d+)(?:\s*[-－—]\s*(\d+))?", part)
        if not match:
            raise ValueError(part)
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start > end:
            start, end = end, start
        pages.update(range(start, end + 1))
    return pages


class SimilarityWorker(QThread):
    progress = pyqtSignal(int, int, str)
    result_ready = pyqtSignal(list)

    def __init__(self, pages_left: dict[int, str], pages_right: dict[int, str]):
        super().__init__()
        self.pages_left = dict(pages_left)
        self.pages_right = dict(pages_right)

    def run(self):
        pages = sorted(set(self.pages_left.keys()) | set(self.pages_right.keys()))
        total = len(pages)
        rows = []
        for done, page in enumerate(pages, start=1):
            if self.isInterruptionRequested():
                break
            left_text = self.pages_left.get(page, "")
            right_text = self.pages_right.get(page, "")
            ratio = text_similarity(left_text, right_text)
            if not left_text and not right_text:
                status = "两侧为空"
            elif not left_text:
                status = "左侧为空"
            elif not right_text:
                status = "右侧为空"
            elif ratio >= 1.0:
                status = "完全相同"
            else:
                status = "有差异"
            rows.append({
                "page": page,
                "ratio": ratio,
                "left_len": len(left_text),
                "right_len": len(right_text),
                "status": status,
            })
            self.progress.emit(done, total, f"相似度计算: {done}/{total}")
        self.result_ready.emit(rows)


class SimilarityDialog(QDialog):
    def __init__(self, mainwindow):
        super().__init__(mainwindow)
        self.mainwindow = mainwindow
        self.rows = []
        self.worker = None
        self.setWindowTitle("相似度窗口")
        self.resize(520, 620)
        self.init_ui()
        self.refresh()

    def init_ui(self):
        layout = QVBoxLayout(self)
        filter_layout = QGridLayout()

        filter_layout.addWidget(QLabel("相似度:"), 0, 0)
        self.min_similarity_slider = QSlider(Qt.Orientation.Horizontal)
        self.min_similarity_slider.setRange(0, 100)
        self.min_similarity_slider.setValue(0)
        self.min_similarity_slider.valueChanged.connect(self.on_min_similarity_changed)
        filter_layout.addWidget(self.min_similarity_slider, 0, 1)
        self.min_similarity_label = QLabel("0%")
        filter_layout.addWidget(self.min_similarity_label, 0, 2)

        filter_layout.addWidget(QLabel("到:"), 0, 3)
        self.max_similarity_slider = QSlider(Qt.Orientation.Horizontal)
        self.max_similarity_slider.setRange(0, 100)
        self.max_similarity_slider.setValue(100)
        self.max_similarity_slider.valueChanged.connect(self.on_max_similarity_changed)
        filter_layout.addWidget(self.max_similarity_slider, 0, 4)
        self.max_similarity_label = QLabel("100%")
        filter_layout.addWidget(self.max_similarity_label, 0, 5)

        filter_layout.addWidget(QLabel("页码:"), 1, 0)
        self.page_filter = QLineEdit()
        self.page_filter.setPlaceholderText("全部，或 1-10,20-30")
        self.page_filter.textChanged.connect(self.apply_filter)
        filter_layout.addWidget(self.page_filter, 1, 1, 1, 2)

        filter_layout.addWidget(QLabel("状态:"), 1, 3)
        self.status_filter = QComboBox()
        self.status_filter.addItems(["全部", "完全相同", "有差异", "左侧为空", "右侧为空", "两侧为空"])
        self.status_filter.currentIndexChanged.connect(self.apply_filter)
        filter_layout.addWidget(self.status_filter, 1, 4)

        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self.refresh)
        filter_layout.addWidget(self.btn_refresh, 1, 5)
        layout.addLayout(filter_layout)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["页码", "相似度", "左字数", "右字数", "状态"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.doubleClicked.connect(self.on_double_click)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table)

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

    def refresh(self):
        if self.worker and self.worker.isRunning():
            return
        self.mainwindow.save_current_page_data()
        self.btn_refresh.setEnabled(False)
        self.status_label.setText("正在后台计算...")
        self.mainwindow.start_background_progress("相似度计算")
        self.worker = SimilarityWorker(
            self.mainwindow.pages_left,
            self.mainwindow.pages_right_text,
        )
        self.worker.progress.connect(self.mainwindow.update_background_progress)
        self.worker.progress.connect(lambda done, total, msg: self.status_label.setText(msg))
        self.worker.result_ready.connect(self.on_result_ready)
        self.worker.finished.connect(self.on_worker_finished)
        self.worker.start()

    def on_result_ready(self, rows):
        self.rows = rows
        self.apply_filter()

    def on_worker_finished(self):
        self.btn_refresh.setEnabled(True)
        self.mainwindow.finish_background_progress("相似度计算完成")
        self.worker = None

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.requestInterruption()
            self.worker.wait(1000)
        super().closeEvent(event)

    def on_min_similarity_changed(self, value):
        if value > self.max_similarity_slider.value():
            self.max_similarity_slider.setValue(value)
        self.update_similarity_labels()
        self.apply_filter()

    def on_max_similarity_changed(self, value):
        if value < self.min_similarity_slider.value():
            self.min_similarity_slider.setValue(value)
        self.update_similarity_labels()
        self.apply_filter()

    def update_similarity_labels(self):
        self.min_similarity_label.setText(f"{self.min_similarity_slider.value()}%")
        self.max_similarity_label.setText(f"{self.max_similarity_slider.value()}%")

    def apply_filter(self):
        min_ratio = self.min_similarity_slider.value() / 100
        max_ratio = self.max_similarity_slider.value() / 100
        status = self.status_filter.currentText()
        try:
            selected_pages = parse_page_ranges(self.page_filter.text())
            page_filter_error = None
        except ValueError as e:
            selected_pages = set()
            page_filter_error = f"页码范围格式错误: {e}"

        visible_rows = []
        for row in self.rows:
            ratio = row["ratio"]
            if ratio < min_ratio or ratio > max_ratio:
                continue
            if selected_pages is not None and row["page"] not in selected_pages:
                continue
            if status != "全部" and row["status"] != status:
                continue
            visible_rows.append(row)

        self.table.setRowCount(len(visible_rows))
        for r, row in enumerate(visible_rows):
            background = similarity_background(row["ratio"])
            page_item = QTableWidgetItem(str(row["page"]))
            page_item.setData(Qt.ItemDataRole.UserRole, row["page"])
            items = [
                page_item,
                QTableWidgetItem(f'{row["ratio"] * 100:.2f}%'),
                QTableWidgetItem(str(row["left_len"])),
                QTableWidgetItem(str(row["right_len"])),
                QTableWidgetItem(row["status"]),
            ]
            for col, item in enumerate(items):
                if background:
                    item.setBackground(background)
                self.table.setItem(r, col, item)
        if page_filter_error:
            self.status_label.setText(page_filter_error)
        else:
            self.status_label.setText(f"显示 {len(visible_rows)} / {len(self.rows)} 页")

    def on_double_click(self, index):
        if not index.isValid():
            return
        item = self.table.item(index.row(), 0)
        if item:
            self.mainwindow.goto_page(item.data(Qt.ItemDataRole.UserRole))
