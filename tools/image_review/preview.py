from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
)


class ImagePreviewView(QGraphicsView):
    """Pan/zoom viewer that always fits the complete image on first display."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._pixmap_item = QGraphicsPixmapItem()
        self._scene.addItem(self._pixmap_item)
        self._fit_mode = True
        self._right_wheel_scroll = False
        self.setBackgroundBrush(Qt.GlobalColor.lightGray)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)

    def set_image(self, image):
        if image is None or image.isNull():
            self.clear_image()
            return
        self._pixmap_item.setPixmap(QPixmap.fromImage(image))
        self._scene.setSceneRect(self._pixmap_item.boundingRect())
        self._fit_mode = True
        self.fit_image()

    def has_image(self):
        return not self._pixmap_item.pixmap().isNull()

    def clear_image(self):
        self._pixmap_item.setPixmap(QPixmap())
        self._scene.setSceneRect(0, 0, 1, 1)
        self.resetTransform()

    def fit_image(self):
        if self._pixmap_item.pixmap().isNull():
            return
        self._fit_mode = True
        self.resetTransform()
        self.fitInView(
            self._pixmap_item.boundingRect(),
            Qt.AspectRatioMode.KeepAspectRatio,
        )

    def actual_size(self):
        self._fit_mode = False
        self.resetTransform()
        self.centerOn(self._pixmap_item)

    def zoom_by(self, factor):
        if self._pixmap_item.pixmap().isNull():
            return
        self._fit_mode = False
        current = self.transform().m11()
        target = max(0.05, min(20.0, current * float(factor)))
        self.scale(target / max(current, 0.0001), target / max(current, 0.0001))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self._right_wheel_scroll = True
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self._right_wheel_scroll = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        right_down = bool(
            QApplication.mouseButtons() & Qt.MouseButton.RightButton
        )
        if self._right_wheel_scroll or right_down:
            pixel = event.pixelDelta()
            angle = event.angleDelta()
            delta = pixel.y() or pixel.x() or angle.y() or angle.x()
            bar = self.horizontalScrollBar()
            if pixel.isNull():
                distance = int(
                    (delta / 120.0) * max(40, bar.pageStep() // 8)
                )
            else:
                distance = int(delta)
            bar.setValue(bar.value() - distance)
            event.accept()
            return
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom_by(1.15 if event.angleDelta().y() > 0 else 1 / 1.15)
            event.accept()
            return
        super().wheelEvent(event)

    def focusOutEvent(self, event):
        self._right_wheel_scroll = False
        super().focusOutEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._fit_mode:
            self.fit_image()

