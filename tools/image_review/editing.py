from __future__ import annotations

from PyQt6 import sip

from PyQt6.QtCore import QEvent, QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QBrush, QPen, QUndoCommand
from PyQt6.QtWidgets import QGraphicsItem, QGraphicsRectItem, QGraphicsView

from tools.workspace_views import ImageEditExtension


class GeometryCommand(QUndoCommand):
    def __init__(self, extension, segment_id, old_bbox, new_bbox):
        super().__init__("调整图片区域")
        self.extension = extension
        self.segment_id = str(segment_id)
        self.old_bbox = tuple(old_bbox)
        self.new_bbox = tuple(new_bbox)

    def undo(self):
        self.extension.apply_geometry(self.segment_id, self.old_bbox, True)

    def redo(self):
        self.extension.apply_geometry(self.segment_id, self.new_bbox, True)


class EditableRectItem(QGraphicsRectItem):
    HANDLE_SIZE = 9.0
    MIN_SIZE = 4.0

    def __init__(self, extension, segment_id, bbox):
        super().__init__()
        self.extension = extension
        self.segment_id = str(segment_id)
        self.resize_handle = None
        self.press_bbox = None
        self.press_scene_pos = QPointF()
        self._applying_bbox = False
        self.display_muted = False
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        pen = QPen(QColor("#00875f"))
        pen.setWidth(2)
        pen.setCosmetic(True)
        self.setPen(pen)
        self.setBrush(QBrush(QColor(0, 163, 108, 30)))
        self.setZValue(30)
        self.set_mute_state(False)
        self.set_bbox(bbox)

    def set_mute_state(self, muted):
        self.display_muted = bool(muted)
        color = QColor("#7b8794") if self.display_muted else QColor("#00875f")
        fill = QColor(123, 135, 148, 18) if self.display_muted else QColor(0, 163, 108, 30)
        pen = QPen(color)
        pen.setWidth(1 if self.display_muted else 2)
        pen.setCosmetic(True)
        self.setPen(pen)
        self.setBrush(QBrush(fill))
        self.update()

    def bbox(self):
        rect = self.rect()
        position = self.pos()
        return (
            position.x() + rect.left(),
            position.y() + rect.top(),
            position.x() + rect.right(),
            position.y() + rect.bottom(),
        )

    def set_bbox(self, bbox):
        x1, y1, x2, y2 = (float(value) for value in bbox)
        self._applying_bbox = True
        self.setPos(x1, y1)
        self.setRect(0, 0, max(self.MIN_SIZE, x2 - x1), max(self.MIN_SIZE, y2 - y1))
        self._applying_bbox = False
        self.update()

    def handle_rects(self):
        rect = self.rect()
        size = self.HANDLE_SIZE / max(0.1, self.extension.canvas.transform().m11())
        half = size / 2
        x1, xc, x2 = rect.left(), rect.center().x(), rect.right()
        y1, yc, y2 = rect.top(), rect.center().y(), rect.bottom()
        return {
            "nw": QRectF(x1 - half, y1 - half, size, size),
            "n": QRectF(xc - half, y1 - half, size, size),
            "ne": QRectF(x2 - half, y1 - half, size, size),
            "e": QRectF(x2 - half, yc - half, size, size),
            "se": QRectF(x2 - half, y2 - half, size, size),
            "s": QRectF(xc - half, y2 - half, size, size),
            "sw": QRectF(x1 - half, y2 - half, size, size),
            "w": QRectF(x1 - half, yc - half, size, size),
        }

    def boundingRect(self):
        margin = self.HANDLE_SIZE / max(0.1, self.extension.canvas.transform().m11())
        return self.rect().adjusted(-margin, -margin, margin, margin)

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        if not self.isSelected():
            return
        painter.save()
        painter.setPen(QPen(QColor("white"), 1))
        painter.setBrush(QBrush(QColor("#007a52")))
        for rect in self.handle_rects().values():
            painter.drawRect(rect)
        painter.restore()

    def mousePressEvent(self, event):
        self.extension.select_segment(self.segment_id)
        self.press_bbox = self.bbox()
        self.press_scene_pos = event.scenePos()
        self.resize_handle = next(
            (name for name, rect in self.handle_rects().items() if rect.contains(event.pos())),
            None,
        )
        if self.resize_handle:
            self.setSelected(True)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not self.resize_handle or not self.press_bbox:
            super().mouseMoveEvent(event)
            self.extension.preview_geometry(self.segment_id, self.bbox())
            return
        x1, y1, x2, y2 = self.press_bbox
        delta = event.scenePos() - self.press_scene_pos
        if "w" in self.resize_handle:
            x1 += delta.x()
        if "e" in self.resize_handle:
            x2 += delta.x()
        if "n" in self.resize_handle:
            y1 += delta.y()
        if "s" in self.resize_handle:
            y2 += delta.y()
        scene = self.extension.canvas.sceneRect()
        x1 = max(scene.left(), min(x1, x2 - self.MIN_SIZE))
        y1 = max(scene.top(), min(y1, y2 - self.MIN_SIZE))
        x2 = min(scene.right(), max(x2, x1 + self.MIN_SIZE))
        y2 = min(scene.bottom(), max(y2, y1 + self.MIN_SIZE))
        self.set_bbox((x1, y1, x2, y2))
        self.extension.preview_geometry(self.segment_id, self.bbox())
        event.accept()

    def mouseReleaseEvent(self, event):
        if self.resize_handle:
            event.accept()
        else:
            super().mouseReleaseEvent(event)
        old_bbox = self.press_bbox
        new_bbox = self.bbox()
        self.resize_handle = None
        self.press_bbox = None
        if old_bbox and any(abs(a - b) > 0.01 for a, b in zip(old_bbox, new_bbox)):
            self.extension.commit_geometry(self.segment_id, old_bbox, new_bbox)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            selected = bool(value)
            self.setZValue(60 if selected else 30)
            if selected:
                self.extension._selected_segment_id = self.segment_id
                self.extension.selection_changed.emit(self.segment_id)
            elif self.extension._selected_segment_id == self.segment_id:
                self.extension._selected_segment_id = ""
        if (
            change == QGraphicsItem.GraphicsItemChange.ItemPositionChange
            and not self._applying_bbox
            and self.scene()
        ):
            scene = self.extension.canvas.sceneRect()
            rect = self.rect()
            return QPointF(
                max(scene.left(), min(value.x(), scene.right() - rect.width())),
                max(scene.top(), min(value.y(), scene.bottom() - rect.height())),
            )
        return super().itemChange(change, value)


class AdvancedImageEditExtension(ImageEditExtension):
    selection_changed = pyqtSignal(str)
    geometry_preview = pyqtSignal(str, object)

    def __init__(self, canvas, owner_key, parent=None):
        super().__init__(canvas, owner_key, parent)
        self.mode = "select"
        self.items = {}
        self.on_change = None
        self.on_new = None
        self.on_delete = None
        self.draw_start = None
        self.draw_item = None
        self.applying_command = False
        self._selected_segment_id = ""

    @staticmethod
    def _item_alive(item):
        return item is not None and not sip.isdeleted(item)

    def _purge_deleted_items(self):
        deleted = [
            segment_id for segment_id, item in self.items.items()
            if not self._item_alive(item)
        ]
        for segment_id in deleted:
            self.items.pop(segment_id, None)
        self.overlay_items[:] = [
            item for item in self.overlay_items if self._item_alive(item)
        ]
        if self._selected_segment_id not in self.items:
            self._selected_segment_id = ""
    def activate(self):
        super().activate()
        self.canvas.installEventFilter(self)
        self.canvas.viewport().installEventFilter(self)
        self.set_mode("select")

    def deactivate(self):
        self.canvas.removeEventFilter(self)
        self.canvas.viewport().removeEventFilter(self)
        self.clear_segments()
        super().deactivate()

    def set_mode(self, mode):
        self._purge_deleted_items()
        self.mode = mode if mode in {"pan", "select", "draw"} else "select"
        self.canvas.setDragMode(
            QGraphicsView.DragMode.ScrollHandDrag
            if self.mode == "pan"
            else QGraphicsView.DragMode.NoDrag
        )
        for item in self.items.values():
            item.setEnabled(self.mode == "select")

    def set_segments(self, segments, on_change=None, on_new=None, on_delete=None):
        self._purge_deleted_items()
        self.on_change = on_change
        self.on_new = on_new
        self.on_delete = on_delete
        incoming = {
            str(getattr(segment, "segment_id", None) or segment["id"]): segment
            for segment in segments
        }
        changed_ids = set(incoming) != set(self.items)
        for segment_id in tuple(self.items):
            if segment_id not in incoming:
                item = self.items.pop(segment_id)
                if self._item_alive(item):
                    self.canvas.scene.removeItem(item)
                if item in self.overlay_items:
                    self.overlay_items.remove(item)
        for segment_id, segment in incoming.items():
            bbox = getattr(segment, "bbox", None) or segment["bbox"]
            item = self.items.get(segment_id)
            if item is None:
                item = EditableRectItem(self, segment_id, bbox)
                self.canvas.scene.addItem(item)
                self.overlay_items.append(item)
                self.items[segment_id] = item
            elif not item.press_bbox:
                item.set_bbox(bbox)
        if changed_ids:
            self.undo_stack.clear()
        if self._selected_segment_id not in self.items:
            self._selected_segment_id = ""

    def clear_segments(self):
        self._purge_deleted_items()
        for item in list(self.overlay_items):
            if self._item_alive(item):
                self.canvas.scene.removeItem(item)
        self.overlay_items.clear()
        self.items.clear()
        self.draw_item = None
        self.draw_start = None
        self._selected_segment_id = ""

    def selected_segment_id(self):
        self._purge_deleted_items()
        if self._selected_segment_id in self.items:
            return self._selected_segment_id
        for segment_id, item in self.items.items():
            if item.isSelected():
                self._selected_segment_id = segment_id
                return segment_id
        return ""

    def select_segment(self, segment_id):
        self._purge_deleted_items()
        segment_id = str(segment_id or "")
        if segment_id not in self.items:
            return False
        for current_id, item in self.items.items():
            item.setSelected(current_id == segment_id)
        self._selected_segment_id = segment_id
        return True

    def delete_selected(self):
        self._purge_deleted_items()
        segment_id = self.selected_segment_id()
        if not segment_id:
            return False
        item = self.items.pop(segment_id, None)
        if item is not None:
            if self._item_alive(item):
                self.canvas.scene.removeItem(item)
            if item in self.overlay_items:
                self.overlay_items.remove(item)
        self._selected_segment_id = ""
        if self.on_delete:
            self.on_delete(segment_id)
        return True

    def nudge_selected(self, dx, dy):
        self._purge_deleted_items()
        segment_id = self.selected_segment_id()
        item = self.items.get(segment_id)
        if item is None:
            return
        old_bbox = item.bbox()
        x1, y1, x2, y2 = old_bbox
        self.commit_geometry(segment_id, old_bbox, (x1 + dx, y1 + dy, x2 + dx, y2 + dy))

    def commit_geometry(self, segment_id, old_bbox, new_bbox):
        if not self.applying_command:
            self.undo_stack.push(GeometryCommand(self, segment_id, old_bbox, new_bbox))

    def preview_geometry(self, segment_id, bbox):
        if self.applying_command:
            return
        if self.on_change:
            self.on_change(segment_id, tuple(bbox), False)
        self.geometry_preview.emit(segment_id, tuple(bbox))

    def apply_geometry(self, segment_id, bbox, final):
        self._purge_deleted_items()
        item = self.items.get(str(segment_id))
        if item is None:
            return
        self.applying_command = True
        try:
            item.set_bbox(bbox)
            if self.on_change:
                self.on_change(str(segment_id), tuple(bbox), bool(final))
        finally:
            self.applying_command = False

    def eventFilter(self, watched, event):
        if not self.active or watched not in (self.canvas, self.canvas.viewport()):
            return super().eventFilter(watched, event)
        if event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
                self.delete_selected()
                return True
            step = 10 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
            directions = {
                Qt.Key.Key_Left: (-step, 0),
                Qt.Key.Key_Right: (step, 0),
                Qt.Key.Key_Up: (0, -step),
                Qt.Key.Key_Down: (0, step),
            }
            if event.key() in directions:
                self.nudge_selected(*directions[event.key()])
                return True
        if self.mode != "draw":
            return super().eventFilter(watched, event)
        if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            self.draw_start = self.canvas.mapToScene(event.position().toPoint())
            self.draw_item = QGraphicsRectItem()
            pen = QPen(QColor("#00875f"))
            pen.setWidth(2)
            pen.setCosmetic(True)
            self.draw_item.setPen(pen)
            self.draw_item.setBrush(QBrush(QColor(0, 163, 108, 24)))
            self.draw_item.setZValue(31)
            self.canvas.scene.addItem(self.draw_item)
            self.overlay_items.append(self.draw_item)
            return True
        if event.type() == QEvent.Type.MouseMove and self.draw_start and self.draw_item:
            current = self.canvas.mapToScene(event.position().toPoint())
            self.draw_item.setRect(QRectF(self.draw_start, current).normalized())
            return True
        if event.type() == QEvent.Type.MouseButtonRelease and self.draw_start and self.draw_item:
            rect = self.draw_item.rect().normalized().intersected(self.canvas.sceneRect())
            try:
                self.canvas.scene.removeItem(self.draw_item)
            except RuntimeError:
                pass
            if self.draw_item in self.overlay_items:
                self.overlay_items.remove(self.draw_item)
            self.draw_item = None
            self.draw_start = None
            if rect.width() >= EditableRectItem.MIN_SIZE and rect.height() >= EditableRectItem.MIN_SIZE:
                if self.on_new:
                    self.on_new((rect.left(), rect.top(), rect.right(), rect.bottom()))
            return True
        return super().eventFilter(watched, event)
