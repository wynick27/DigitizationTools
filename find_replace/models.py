from PyQt6.QtWidgets import (
    QStyle, QStyledItemDelegate
)
from PyQt6.QtCore import Qt, QAbstractTableModel, QSize, QRect, QPoint
from PyQt6.QtGui import (
    QColor, QTextDocument, QAbstractTextDocumentLayout
)

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

