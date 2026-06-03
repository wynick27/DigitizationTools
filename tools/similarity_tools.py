import difflib

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
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
        filter_layout = QHBoxLayout()
        filter_layout.addWidget(QLabel("过滤:"))
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(["全部", "低于 90%", "低于 80%", "低于 60%", "完全相同", "有差异"])
        self.filter_combo.currentIndexChanged.connect(self.apply_filter)
        filter_layout.addWidget(self.filter_combo)

        self.filter_text = QLineEdit()
        self.filter_text.setPlaceholderText("页码或状态过滤")
        self.filter_text.textChanged.connect(self.apply_filter)
        filter_layout.addWidget(self.filter_text)

        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self.refresh)
        filter_layout.addWidget(self.btn_refresh)
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

    def apply_filter(self):
        mode = self.filter_combo.currentText()
        needle = self.filter_text.text().strip().lower()
        visible_rows = []
        for row in self.rows:
            ratio = row["ratio"]
            if mode == "低于 90%" and ratio >= 0.90:
                continue
            if mode == "低于 80%" and ratio >= 0.80:
                continue
            if mode == "低于 60%" and ratio >= 0.60:
                continue
            if mode == "完全相同" and ratio < 1.0:
                continue
            if mode == "有差异" and ratio >= 1.0:
                continue
            if needle and needle not in f'{row["page"]} {row["status"]}'.lower():
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
        self.status_label.setText(f"显示 {len(visible_rows)} / {len(self.rows)} 页")

    def on_double_click(self, index):
        if not index.isValid():
            return
        item = self.table.item(index.row(), 0)
        if item:
            self.mainwindow.goto_page(item.data(Qt.ItemDataRole.UserRole))
