import difflib
import re

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)


KIND_ALL = "全部"
KIND_EQUAL = "相同词头"
KIND_DIFF = "不同词头"
KIND_LEFT_ONLY = "左侧孤立"
KIND_RIGHT_ONLY = "右侧孤立"
KIND_PAIRED = "非孤立"


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


def extract_page_headwords(text: str, regex_text: str, group_id: int) -> list[dict]:
    if not regex_text:
        return []
    try:
        regex = re.compile(regex_text)
    except re.error:
        return []

    headwords = []
    offset = 0
    for line in (text or "").splitlines(True):
        match = regex.search(line)
        if match:
            try:
                headword = match.group(group_id)
                start = match.start(group_id)
                end = match.end(group_id)
            except IndexError:
                headword = match.group(0)
                start = match.start(0)
                end = match.end(0)
            headwords.append({
                "text": headword,
                "start": offset + start,
                "end": offset + end,
            })
        offset += len(line)
    return headwords


def calculate_headword_comparison(
    pages_left: dict[int, str],
    pages_right: dict[int, str],
    left_regex: str,
    left_group: int,
    right_regex: str,
    right_group: int,
) -> list[dict]:
    rows = []
    for page in sorted(set(pages_left.keys()) | set(pages_right.keys())):
        left_items = extract_page_headwords(pages_left.get(page, ""), left_regex, left_group)
        right_items = extract_page_headwords(pages_right.get(page, ""), right_regex, right_group)
        matcher = difflib.SequenceMatcher(
            None,
            [item["text"] for item in left_items],
            [item["text"] for item in right_items],
            autojunk=False,
        )

        index = 1
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for left_idx, right_idx in zip(range(i1, i2), range(j1, j2)):
                    rows.append(_make_row(page, KIND_EQUAL, index, left_items[left_idx], right_items[right_idx]))
                    index += 1
                continue

            if tag == "delete":
                for left_idx in range(i1, i2):
                    rows.append(_make_row(page, KIND_LEFT_ONLY, index, left_items[left_idx], None))
                    index += 1
                continue

            if tag == "insert":
                for right_idx in range(j1, j2):
                    rows.append(_make_row(page, KIND_RIGHT_ONLY, index, None, right_items[right_idx]))
                    index += 1
                continue

            left_range = list(range(i1, i2))
            right_range = list(range(j1, j2))
            paired_count = min(len(left_range), len(right_range))
            for k in range(paired_count):
                rows.append(_make_row(
                    page,
                    KIND_DIFF,
                    index,
                    left_items[left_range[k]],
                    right_items[right_range[k]],
                ))
                index += 1
            for left_idx in left_range[paired_count:]:
                rows.append(_make_row(page, KIND_LEFT_ONLY, index, left_items[left_idx], None))
                index += 1
            for right_idx in right_range[paired_count:]:
                rows.append(_make_row(page, KIND_RIGHT_ONLY, index, None, right_items[right_idx]))
                index += 1

    return rows


def _make_row(page, kind, index, left_item, right_item):
    return {
        "page": page,
        "kind": kind,
        "left": left_item["text"] if left_item else "",
        "right": right_item["text"] if right_item else "",
        "left_span": (left_item["start"], left_item["end"]) if left_item else None,
        "right_span": (right_item["start"], right_item["end"]) if right_item else None,
        "index": index,
    }


def headword_background(kind: str) -> QColor | None:
    if kind == KIND_DIFF:
        return QColor("#fff4bf")
    if kind == KIND_LEFT_ONLY:
        return QColor("#ffdede")
    if kind == KIND_RIGHT_ONLY:
        return QColor("#dff0ff")
    return None


class HeadwordCompareWorker(QThread):
    progress = pyqtSignal(int, int, str)
    result_ready = pyqtSignal(list)

    def __init__(
        self,
        pages_left: dict[int, str],
        pages_right: dict[int, str],
        left_regex: str,
        left_group: int,
        right_regex: str,
        right_group: int,
    ):
        super().__init__()
        self.pages_left = dict(pages_left)
        self.pages_right = dict(pages_right)
        self.left_regex = left_regex
        self.left_group = left_group
        self.right_regex = right_regex
        self.right_group = right_group

    def run(self):
        pages = sorted(set(self.pages_left.keys()) | set(self.pages_right.keys()))
        total = len(pages)
        rows = []
        for done, page in enumerate(pages, start=1):
            if self.isInterruptionRequested():
                break
            rows.extend(calculate_headword_comparison(
                {page: self.pages_left.get(page, "")},
                {page: self.pages_right.get(page, "")},
                self.left_regex,
                self.left_group,
                self.right_regex,
                self.right_group,
            ))
            self.progress.emit(done, total, f"词头对比: {done}/{total}")
        self.result_ready.emit(rows)


class HeadwordCompareDialog(QDialog):
    def __init__(self, mainwindow):
        super().__init__(mainwindow)
        self.mainwindow = mainwindow
        self.rows = []
        self.worker = None
        self.setWindowTitle("词头对比窗口")
        self.resize(920, 680)
        self.init_ui()
        self.refresh()

    def init_ui(self):
        layout = QVBoxLayout(self)
        regex_group = QGroupBox("词头正则表达式")
        regex_layout = QFormLayout(regex_group)

        left_row = QHBoxLayout()
        self.regex_left = QLineEdit(self.mainwindow.project_config.get("regex_left", ""))
        self.group_left = QSpinBox()
        self.group_left.setRange(0, 99)
        self.group_left.setValue(self.mainwindow.project_config.get("regex_group_left", 0))
        left_row.addWidget(self.regex_left)
        left_row.addWidget(QLabel("组:"))
        left_row.addWidget(self.group_left)
        regex_layout.addRow("左侧:", left_row)

        right_row = QHBoxLayout()
        self.regex_right = QLineEdit(self.mainwindow.project_config.get("regex_right", ""))
        self.group_right = QSpinBox()
        self.group_right.setRange(0, 99)
        self.group_right.setValue(self.mainwindow.project_config.get("regex_group_right", 0))
        right_row.addWidget(self.regex_right)
        right_row.addWidget(QLabel("组:"))
        right_row.addWidget(self.group_right)
        regex_layout.addRow("右侧:", right_row)
        layout.addWidget(regex_group)

        filter_layout = QHBoxLayout()
        filter_layout.addWidget(QLabel("页码:"))
        self.page_filter = QLineEdit()
        self.page_filter.setPlaceholderText("全部，或 1-4,6-8,12")
        self.page_filter.textChanged.connect(self.apply_filter)
        filter_layout.addWidget(self.page_filter)

        filter_layout.addWidget(QLabel("筛选:"))
        self.kind_filter = QComboBox()
        self.kind_filter.addItems([
            KIND_ALL,
            KIND_EQUAL,
            KIND_DIFF,
            KIND_LEFT_ONLY,
            KIND_RIGHT_ONLY,
            KIND_PAIRED,
        ])
        self.kind_filter.currentIndexChanged.connect(self.apply_filter)
        filter_layout.addWidget(self.kind_filter)

        self.text_filter = QLineEdit()
        self.text_filter.setPlaceholderText("词头过滤")
        self.text_filter.textChanged.connect(self.apply_filter)
        filter_layout.addWidget(self.text_filter)

        self.btn_refresh = QPushButton("计算")
        self.btn_refresh.clicked.connect(self.refresh)
        filter_layout.addWidget(self.btn_refresh)
        layout.addLayout(filter_layout)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["页码", "类型", "左侧词头", "右侧词头", "序号"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.doubleClicked.connect(self.on_double_click)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table)

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

    def refresh(self):
        if self.worker and self.worker.isRunning():
            return
        self.mainwindow.save_current_page_data()
        self.btn_refresh.setEnabled(False)
        self.status_label.setText("正在后台计算...")
        self.mainwindow.start_background_progress("词头对比")
        self.worker = HeadwordCompareWorker(
            self.mainwindow.pages_left,
            self.mainwindow.pages_right_text,
            self.regex_left.text(),
            self.group_left.value(),
            self.regex_right.text(),
            self.group_right.value(),
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
        self.mainwindow.finish_background_progress("词头对比完成")
        self.worker = None

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.requestInterruption()
            self.worker.wait(1000)
        super().closeEvent(event)

    def apply_filter(self):
        try:
            selected_pages = parse_page_ranges(self.page_filter.text())
            page_filter_error = None
        except ValueError as e:
            selected_pages = set()
            page_filter_error = f"页码范围格式错误: {e}"

        kind = self.kind_filter.currentText()
        needle = self.text_filter.text().strip().lower()
        visible_rows = []
        for row in self.rows:
            if selected_pages is not None and row["page"] not in selected_pages:
                continue
            if kind == KIND_PAIRED:
                if not row["left_span"] or not row["right_span"]:
                    continue
            elif kind != KIND_ALL and row["kind"] != kind:
                continue
            if needle and needle not in f'{row["left"]} {row["right"]}'.lower():
                continue
            visible_rows.append(row)

        self.table.setRowCount(len(visible_rows))
        for r, row in enumerate(visible_rows):
            background = headword_background(row["kind"])
            page_item = QTableWidgetItem(str(row["page"]))
            page_item.setData(Qt.ItemDataRole.UserRole, row)
            items = [
                page_item,
                QTableWidgetItem(row["kind"]),
                QTableWidgetItem(row["left"]),
                QTableWidgetItem(row["right"]),
                QTableWidgetItem(str(row["index"])),
            ]
            for col, item in enumerate(items):
                if background:
                    item.setBackground(background)
                self.table.setItem(r, col, item)
        if page_filter_error:
            self.status_label.setText(page_filter_error)
        else:
            self.status_label.setText(f"显示 {len(visible_rows)} / {len(self.rows)} 条")

    def on_double_click(self, index):
        if not index.isValid():
            return
        item = self.table.item(index.row(), 0)
        if item:
            row = item.data(Qt.ItemDataRole.UserRole)
            preferred_side = "right" if index.column() == 3 else "left"
            self.mainwindow.goto_headword(row, preferred_side)
