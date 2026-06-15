import json
from pathlib import Path

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableView,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from tools.report_data import (
    load_report_file,
    parse_page_ranges,
    write_tool_report,
)


class ReportTableModel(QAbstractTableModel):
    HEADERS = ("页码", "行号", "问题类型", "词头", "摘要", "来源")
    KEYS = ("page", "line", "issue_type", "headword", "summary", "source_file")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = list(rows)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return row
        if role == Qt.ItemDataRole.DisplayRole:
            value = row.get(self.KEYS[index.column()])
            return "" if value is None else value
        if role == Qt.ItemDataRole.ToolTipRole and index.column() in {3, 4}:
            return str(row.get(self.KEYS[index.column()]) or "")
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return super().headerData(section, orientation, role)

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        key = self.KEYS[column]

        def sort_key(row):
            value = row.get(key)
            if value is None:
                return (1, "")
            if isinstance(value, int):
                return (0, value)
            return (0, str(value).casefold())

        self.layoutAboutToBeChanged.emit()
        self.rows.sort(key=sort_key, reverse=order == Qt.SortOrder.DescendingOrder)
        self.layoutChanged.emit()


class ReportReviewDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.main_window = parent
        self.rows = []
        self.filtered_rows = []
        self.setWindowTitle("外部报告审阅")
        self.resize(1180, 760)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)

        actions = QHBoxLayout()
        self.btn_import = QPushButton("导入报告")
        self.btn_import.clicked.connect(self.import_reports)
        actions.addWidget(self.btn_import)
        self.btn_export = QPushButton("导出当前筛选")
        self.btn_export.clicked.connect(self.export_filtered_report)
        actions.addWidget(self.btn_export)
        self.btn_clear = QPushButton("清空")
        self.btn_clear.clicked.connect(self.clear_reports)
        actions.addWidget(self.btn_clear)
        actions.addStretch()
        self.count_label = QLabel("0 项")
        actions.addWidget(self.count_label)
        root.addLayout(actions)

        filters = QHBoxLayout()
        filters.addWidget(QLabel("来源"))
        self.source_filter = QComboBox()
        self.source_filter.currentIndexChanged.connect(self.apply_filters)
        filters.addWidget(self.source_filter, 1)
        filters.addWidget(QLabel("问题类型"))
        self.type_filter = QComboBox()
        self.type_filter.currentIndexChanged.connect(self.apply_filters)
        filters.addWidget(self.type_filter, 1)
        filters.addWidget(QLabel("页码"))
        self.page_filter = QLineEdit()
        self.page_filter.setPlaceholderText("如 1-20, 35")
        self.page_filter.textChanged.connect(self.apply_filters)
        filters.addWidget(self.page_filter)
        filters.addWidget(QLabel("关键词"))
        self.text_filter = QLineEdit()
        self.text_filter.setPlaceholderText("词头或报告内容")
        self.text_filter.textChanged.connect(self.apply_filters)
        filters.addWidget(self.text_filter, 2)
        root.addLayout(filters)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableView()
        self.table_model = ReportTableModel(self.table)
        self.table.setModel(self.table_model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.doubleClicked.connect(self.locate_current_row)
        self.table.selectionModel().selectionChanged.connect(self.show_current_detail)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        splitter.addWidget(self.table)

        detail_widget = QWidget()
        detail_layout = QVBoxLayout(detail_widget)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.addWidget(QLabel("原始报告字段"))
        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        detail_layout.addWidget(self.detail)
        splitter.addWidget(detail_widget)
        splitter.setSizes((530, 180))
        root.addWidget(splitter)

    def import_reports(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "导入外部报告",
            "",
            "报告文件 (*.tsv *.csv *.json *.jsonl);;所有文件 (*)",
        )
        if paths:
            self.load_files(paths)

    def load_files(self, paths):
        loaded = []
        errors = []
        for path in paths:
            try:
                loaded.extend(load_report_file(path))
            except Exception as exc:
                errors.append(f"{Path(path).name}: {exc}")
        self.rows.extend(loaded)
        self._refresh_filter_options()
        self.apply_filters()
        if errors:
            QMessageBox.warning(self, "部分报告导入失败", "\n".join(errors))

    def _refresh_filter_options(self):
        selected_source = self.source_filter.currentText()
        selected_type = self.type_filter.currentText()
        self.source_filter.blockSignals(True)
        self.type_filter.blockSignals(True)
        self.source_filter.clear()
        self.source_filter.addItem("全部")
        self.source_filter.addItems(sorted({row["source_file"] for row in self.rows if row["source_file"]}))
        self.type_filter.clear()
        self.type_filter.addItem("全部")
        self.type_filter.addItems(sorted({row["issue_type"] for row in self.rows}))
        source_index = self.source_filter.findText(selected_source)
        type_index = self.type_filter.findText(selected_type)
        self.source_filter.setCurrentIndex(max(0, source_index))
        self.type_filter.setCurrentIndex(max(0, type_index))
        self.source_filter.blockSignals(False)
        self.type_filter.blockSignals(False)

    def apply_filters(self):
        source = self.source_filter.currentText()
        issue_type = self.type_filter.currentText()
        needle = self.text_filter.text().strip().casefold()
        try:
            pages = parse_page_ranges(self.page_filter.text())
            self.page_filter.setStyleSheet("")
        except ValueError:
            self.page_filter.setStyleSheet("border: 1px solid #c62828;")
            return

        filtered = []
        for row in self.rows:
            if source and source != "全部" and row["source_file"] != source:
                continue
            if issue_type and issue_type != "全部" and row["issue_type"] != issue_type:
                continue
            if pages is not None and row["page"] not in pages:
                continue
            if needle:
                haystack = " ".join(
                    [
                        row["headword"],
                        row["summary"],
                        json.dumps(row["data"], ensure_ascii=False),
                    ]
                ).casefold()
                if needle not in haystack:
                    continue
            filtered.append(row)
        self.filtered_rows = filtered
        self._populate_table()

    def _populate_table(self):
        self.table_model.set_rows(self.filtered_rows)
        self.count_label.setText(f"{len(self.filtered_rows)} / {len(self.rows)} 项")
        self.detail.clear()

    def _selected_row(self):
        selected = self.table.selectionModel().selectedRows()
        if not selected:
            return None
        return self.table_model.rows[selected[0].row()]

    def show_current_detail(self):
        row = self._selected_row()
        if not row:
            self.detail.clear()
            return
        detail = {
            "page": row["page"],
            "line": row["line"],
            "issue_type": row["issue_type"],
            "headword": row["headword"],
            "source_file": row["source_file"],
            "source_row": row["source_row"],
            "data": row["data"],
        }
        self.detail.setPlainText(json.dumps(detail, ensure_ascii=False, indent=2))

    def locate_current_row(self, _index=None):
        row = self._selected_row()
        if row and self.main_window and hasattr(self.main_window, "goto_report_issue"):
            self.main_window.goto_report_issue(row)

    def export_filtered_report(self):
        if not self.filtered_rows:
            QMessageBox.information(self, "导出报告", "当前筛选没有可导出的项目。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出工具报告",
            "digitization_tools_report.json",
            "JSON (*.json)",
        )
        if path:
            if not path.lower().endswith(".json"):
                path += ".json"
            write_tool_report(path, self.filtered_rows)
            QMessageBox.information(self, "导出报告", f"已导出 {len(self.filtered_rows)} 项。")

    def clear_reports(self):
        self.rows.clear()
        self.filtered_rows.clear()
        self._refresh_filter_options()
        self._populate_table()
