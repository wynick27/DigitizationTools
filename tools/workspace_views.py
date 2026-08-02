from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QUndoStack


IMAGE_NAVIGATION = "image_navigation"
IMAGE_LOCATION = "image_location"
IMAGE_EDITING = "image_editing"


class ImageCoordinateMapper:
    """Convert persisted OCR coordinates to canonical original-image pixels."""

    @staticmethod
    def to_pixels(bbox, coordinate_type, width, height):
        if not bbox or len(bbox) != 4:
            return None
        x1, y1, x2, y2 = (float(value) for value in bbox)
        if width <= 0 or height <= 0:
            return [x1, y1, x2, y2]
        if coordinate_type == "mineru_page_1000":
            return [
                x1 * width / 1000.0,
                y1 * height / 1000.0,
                x2 * width / 1000.0,
                y2 * height / 1000.0,
            ]
        if coordinate_type == "normalized" or max(map(abs, (x1, y1, x2, y2))) <= 1.5:
            return [x1 * width, y1 * height, x2 * width, y2 * height]
        return [x1, y1, x2, y2]

    @staticmethod
    def from_pixels(bbox, coordinate_type, width, height):
        if not bbox or len(bbox) != 4:
            return None
        x1, y1, x2, y2 = (float(value) for value in bbox)
        if width <= 0 or height <= 0:
            return [x1, y1, x2, y2]
        if coordinate_type == "mineru_page_1000":
            return [
                x1 * 1000.0 / width,
                y1 * 1000.0 / height,
                x2 * 1000.0 / width,
                y2 * 1000.0 / height,
            ]
        if coordinate_type == "normalized":
            return [x1 / width, y1 / height, x2 / width, y2 / height]
        return [x1, y1, x2, y2]

@dataclass
class WorkspaceViewSpec:
    key: str
    label: str
    activate: Callable[[], bool | None]
    deactivate: Callable[[], bool | None]
    capabilities: frozenset[str] = field(default_factory=frozenset)


class ImageEditExtension(QObject):
    """Optional image-editing state. Read-only views never instantiate this."""

    def __init__(self, canvas, owner_key, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self.owner_key = owner_key
        self.undo_stack = QUndoStack(self)
        self.overlay_items = []
        self.active = False

    def activate(self):
        self.active = True

    def deactivate(self):
        self.active = False
        for item in self.overlay_items:
            try:
                self.canvas.scene.removeItem(item)
            except RuntimeError:
                pass
        self.overlay_items.clear()
        self.undo_stack.clear()


class ImageEditExtensionFactory:
    def create(self, canvas, owner_key, parent=None):
        from tools.image_review.editing import AdvancedImageEditExtension

        return AdvancedImageEditExtension(canvas, owner_key, parent)


class WorkspaceViewManager(QObject):
    view_changed = pyqtSignal(str)
    activation_failed = pyqtSignal(str, str)

    def __init__(self, image_canvas, image_extension_factory=None, parent=None):
        super().__init__(parent)
        self.image_canvas = image_canvas
        self.image_extension_factory = image_extension_factory or ImageEditExtensionFactory()
        self.specs = {}
        self.current_key = None
        self.image_edit_extension = None
        self.is_transitioning = False

    def register(self, spec: WorkspaceViewSpec):
        if spec.key in self.specs:
            raise ValueError(f"重复的工作视图: {spec.key}")
        self.specs[spec.key] = spec

    def activate(self, key):
        if key == self.current_key:
            return True
        target = self.specs.get(key)
        if target is None:
            raise KeyError(key)
        if self.is_transitioning:
            return False

        old_key = self.current_key
        old = self.specs.get(old_key)
        self.is_transitioning = True
        try:
            if old is not None and old.deactivate() is False:
                self.activation_failed.emit(key, "当前视图拒绝退出")
                return False
            self._release_image_extension()

            if IMAGE_EDITING in target.capabilities:
                self.image_edit_extension = self.image_extension_factory.create(
                    self.image_canvas, target.key, self
                )
                self.image_edit_extension.activate()

            if target.activate() is False:
                self._release_image_extension()
                if old is not None:
                    if IMAGE_EDITING in old.capabilities:
                        self.image_edit_extension = self.image_extension_factory.create(
                            self.image_canvas, old.key, self
                        )
                        self.image_edit_extension.activate()
                    old.activate()
                self.activation_failed.emit(key, "目标视图初始化失败")
                return False

            self.current_key = key
            self.view_changed.emit(key)
            return True
        finally:
            self.is_transitioning = False

    def _release_image_extension(self):
        extension = self.image_edit_extension
        self.image_edit_extension = None
        if extension is not None:
            extension.deactivate()
            extension.deleteLater()

    def shutdown(self):
        current = self.specs.get(self.current_key)
        if current is not None and current.deactivate() is False:
            return False
        self._release_image_extension()
        self.current_key = None
        return True
