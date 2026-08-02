from __future__ import annotations

import base64
import html
import json
import os
import tempfile
from pathlib import Path

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
from PyQt6.QtGui import QColor, QImage, QPainter

from .models import ApplyReport, NamingPolicy, ReviewItem, ReviewMode


class OverrideStore:
    VERSION = 1

    def __init__(self, path):
        self.path = str(path or "")
        self.source_fingerprint = {}
        self.items = {}
        self.legacy_mode = False

    def load(self):
        self.items = {}
        self.legacy_mode = False
        if not self.path or not os.path.isfile(self.path):
            return self.items
        try:
            payload = json.loads(Path(self.path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self.items
        if isinstance(payload, dict) and payload.get("version") == self.VERSION:
            self.source_fingerprint = payload.get("source_fingerprint") or {}
            self.items = payload.get("items") or {}
            return self.items
        if isinstance(payload, dict):
            # Compatibility with serve_image_audit.py's filename-keyed format.
            self.legacy_mode = True
            for filename, value in payload.items():
                if not isinstance(value, dict):
                    continue
                self.items[str(filename)] = {
                    "original_name": str(filename),
                    "page": int(value.get("page") or value.get("pdf_page") or 0),
                    "legacy_box": value.get("box"),
                    "legacy": dict(value),
                    "status": "ignored" if value.get("action") == "ignore" else "applied",
                }
        return self.items

    def apply_to(self, items):
        by_name = {item.original_name: item for item in items if item.original_name}
        for item in items:
            override = self.items.get(item.item_id)
            if isinstance(override, dict):
                item.apply_override(override)
        for key, override in self.items.items():
            if not isinstance(override, dict) or not override.get("legacy"):
                continue
            item = by_name.get(key)
            if item is None:
                continue
            legacy = override["legacy"]
            item.metadata["legacy_key"] = key
            box = legacy.get("box")
            if box and len(box) == 4 and item.page > 0:
                item.metadata["legacy_normalized_box"] = list(box)
            for field in (
                "entry_id", "headword", "caption", "action", "confidence", "reason"
            ):
                if field in legacy:
                    item.metadata[field] = legacy[field]
            if "image_order" in legacy or "order" in legacy:
                item.metadata["image_order"] = int(
                    legacy.get("image_order", legacy.get("order", 0)) or 0
                )
            item.label = str(item.metadata.get("headword") or item.original_name)
            item.status = override.get("status", item.status)
        return items

    def save(self, items, source_fingerprint=None):
        if not self.path:
            raise ValueError("未配置覆盖记录路径")
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        if self.legacy_mode:
            payload = {
                key: dict(value.get("legacy") or {})
                for key, value in self.items.items()
                if isinstance(value, dict) and value.get("legacy")
            }
            for item in items:
                old_key = str(item.metadata.get("legacy_key") or "")
                filename = item.output_name or item.original_name
                if old_key and old_key != filename:
                    payload.pop(old_key, None)
                first = item.ordered_segments()[0] if item.segments else None
                payload[filename] = {
                    "entry_id": str(item.metadata.get("entry_id") or ""),
                    "headword": str(item.metadata.get("headword") or item.label),
                    "pdf_page": int(
                        (first.source_page if first and first.source_page is not None else item.page) or 0
                    ),
                    "box": list(item.metadata.get("legacy_box") or ()),
                    "caption": str(item.metadata.get("caption") or ""),
                    "action": str(item.metadata.get("action") or "attach"),
                    "image_order": int(item.metadata.get("image_order") or 0),
                }
                item.metadata["legacy_key"] = filename
            _atomic_write_text(self.path, json.dumps(payload, ensure_ascii=False, indent=2))
            self.load()
            return
        merged = dict(self.items)
        for item in items:
            merged[item.item_id] = item.to_override()
        payload = {
            "version": self.VERSION,
            "source_fingerprint": source_fingerprint or self.source_fingerprint,
            "items": merged,
        }
        _atomic_write_text(self.path, json.dumps(payload, ensure_ascii=False, indent=2))
        self.items = merged


def _atomic_write_text(path, text):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".image-review-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def image_to_data_url(image, fmt="PNG"):
    if image is None or image.isNull():
        return ""
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, fmt)
    return f"data:image/{fmt.lower()};base64,{base64.b64encode(bytes(data)).decode('ascii')}"


class ImageReviewService:
    def __init__(self, page_image_loader, project_config=None):
        self.page_image_loader = page_image_loader
        self.project_config = project_config or {}

    def compose(self, item):
        crops = []
        for segment in item.ordered_segments():
            if not segment.valid:
                continue
            image = self.page_image_loader(segment.page)
            if image is None or image.isNull():
                continue
            x1, y1, x2, y2 = segment.bbox
            x1 = max(0, min(image.width() - 1, round(x1)))
            y1 = max(0, min(image.height() - 1, round(y1)))
            x2 = max(x1 + 1, min(image.width(), round(x2)))
            y2 = max(y1 + 1, min(image.height(), round(y2)))
            crop = image.copy(x1, y1, x2 - x1, y2 - y1)
            if not crop.isNull():
                crops.append(crop)
        if not crops:
            return QImage()
        if len(crops) == 1:
            return crops[0]
        padding = 10
        vertical_text = item.metadata.get("orientation") == "vertical"
        if vertical_text:
            width = sum(crop.width() for crop in crops) + padding * (len(crops) - 1)
            height = max(crop.height() for crop in crops)
        else:
            width = max(crop.width() for crop in crops)
            height = sum(crop.height() for crop in crops) + padding * (len(crops) - 1)
        result = QImage(width + 20, height + 20, QImage.Format.Format_RGB888)
        result.fill(QColor("white"))
        painter = QPainter(result)
        if vertical_text:
            x = result.width() - 10
            for crop in crops:
                x -= crop.width()
                painter.drawImage(x, 10, crop)
                x -= padding
        else:
            y = 10
            for crop in crops:
                painter.drawImage(10, y, crop)
                y += crop.height() + padding
        painter.end()
        return result

    def output_name(self, item):
        extension = os.path.splitext(item.original_name)[1].lower() or ".jpg"
        if extension not in {".jpg", ".jpeg", ".png", ".webp"}:
            extension = ".jpg"
        if item.naming_policy == NamingPolicy.KEEP and item.original_name:
            return item.original_name
        if item.naming_policy == NamingPolicy.PAGE_BBOX and item.segments:
            x1, y1, x2, y2 = item.ordered_segments()[0].bbox
            suffix = f"_multi{len(item.segments)}" if len(item.segments) > 1 else ""
            return (
                f"page_{item.page}_{round(x1)}_{round(y1)}_"
                f"{round(x2)}_{round(y2)}{suffix}{extension}"
            )
        return f"page_{item.page}_{max(1, item.sequence):03d}{extension}"

    def preview_html(self, item, source_text=""):
        image = self.compose(item)
        image_url = image_to_data_url(image)
        if item.mode == ReviewMode.MARKDOWN_IMAGES:
            context = source_text
            context_start = 0
            if item.context_span and source_text:
                context_start, end = item.context_span
                context = source_text[context_start:end]
            token = "IMAGEPREVIEWCURRENTCROPZXQ"
            markup_start = item.metadata.get("markup_start")
            markup_end = item.metadata.get("markup_end")
            if markup_start is not None and markup_end is not None:
                relative_start = int(markup_start) - context_start
                relative_end = int(markup_end) - context_start
                if 0 <= relative_start < relative_end <= len(context):
                    context = context[:relative_start] + token + context[relative_end:]
            from tools.markup_support import build_markup_projection

            projection = build_markup_projection(context, "markdown")
            content = projection.rendered_html
            preview_image = (
                f"<figure style='margin:12px 0'><img src='{image_url}' "
                "style='max-width:100%;height:auto'></figure>"
                if image_url else "<p style='color:#b42318'>当前裁切区域无法生成图片</p>"
            )
            content = content.replace(token, preview_image)
            error = ""
            if projection.errors:
                error = "<p style='color:#b42318'>" + "<br>".join(
                    html.escape(value.message) for value in projection.errors
                ) + "</p>"
            return error + content
        source = str(item.metadata.get("text") or "")
        side = str(item.metadata.get("side") or "left")
        mode = str(self.project_config.get(f"markup_mode_{side}") or "plain")
        if mode in {"markdown", "html"}:
            from tools.markup_support import build_markup_projection

            projection = build_markup_projection(source, mode)
            body = projection.rendered_html
            errors = "<br>".join(html.escape(error.display()) for error in projection.errors)
            error_html = f"<p style='color:#b42318'>{errors}</p>" if errors else ""
        else:
            body = html.escape(source).replace("\n", "<br>")
            error_html = ""
        title = html.escape(item.label)
        image_html = f"<img src='{image_url}' style='max-width:100%;height:auto'>" if image_url else ""
        return (
            "<article style='font-family:sans-serif;line-height:1.55;padding:12px'>"
            f"<h2>{title}</h2>{error_html}<div>{body}</div>"
            f"<figure>{image_html}</figure></article>"
        )

    def entry_preview_html(self, item, image_items=()):
        """Render the same text/image order consumed by dictionary export."""
        base = self.preview_html(item)
        figures = []
        ordered = sorted(
            (image for image in image_items if not image.ignored),
            key=lambda image: (
                int(image.metadata.get("image_order") or 0),
                image.page,
                image.sequence,
            ),
        )
        for image_item in ordered:
            local_path = str(image_item.metadata.get("local_path") or "")
            image = QImage(local_path) if local_path and os.path.isfile(local_path) else QImage()
            if image.isNull() and image_item.segments:
                image = self.compose(image_item)
            image_url = image_to_data_url(image)
            if not image_url:
                continue
            caption = html.escape(str(image_item.metadata.get("caption") or ""))
            caption_html = f"<figcaption>{caption}</figcaption>" if caption else ""
            figures.append(
                "<figure style='margin:14px 0'>"
                f"<img src='{image_url}' style='max-width:100%;height:auto'>"
                f"{caption_html}</figure>"
            )
        if not figures:
            return base
        gallery = (
            "<section style='border-top:1px solid #d9dde3;margin-top:18px;padding-top:12px'>"
            "<h3 style='font-size:15px'>词条图片</h3>"
            + "".join(figures)
            + "</section>"
        )
        marker = "</article>"
        return base.replace(marker, gallery + marker) if marker in base else base + gallery


    def apply(self, items, store, source_text="", catalog_items=None):
        report = ApplyReport()
        changed = [item for item in items if item is not None and item.dirty]
        if not changed:
            report.skipped.extend(item.item_id for item in items if item is not None)
            return report, source_text

        prepared = []
        patches = []
        for item in changed:
            try:
                write_image = bool(item.geometry_dirty or not item.metadata_dirty)
                filename = item.output_name or item.original_name
                replacement_ref = item.original_ref
                target_path = ""
                if write_image:
                    if not item.segments or any(not segment.valid for segment in item.segments):
                        raise ValueError("没有有效裁切框")
                    image = self.compose(item)
                    if image.isNull():
                        raise ValueError("无法从原页生成裁切图")
                    filename = self.output_name(item)
                    target_path, replacement_ref = self._target_for(item, filename)
                    target_path = self._resolve_collision(item, target_path)
                    if os.path.basename(target_path) != filename:
                        filename = os.path.basename(target_path)
                        if item.mode == ReviewMode.MARKDOWN_IMAGES:
                            source_dir = os.path.dirname(os.path.abspath(item.source_path))
                            replacement_ref = os.path.relpath(target_path, source_dir).replace("\\", "/")
                        else:
                            replacement_ref = filename
                    if item.mode == ReviewMode.MARKDOWN_IMAGES and replacement_ref != item.original_ref:
                        if not item.source_span:
                            raise ValueError("无法定位 Markdown 图片引用")
                        start, end = item.source_span
                        if source_text[start:end] != item.original_ref:
                            raise ValueError("Markdown 图片引用已变化，请重新加载")
                        patches.append((start, end, replacement_ref, item))
                    self._atomic_save_image(image, target_path)
                prepared.append((item, filename, target_path, replacement_ref))
            except Exception as error:
                report.errors[item.item_id] = str(error)

        updated_text = source_text
        for start, end, replacement, _item in sorted(patches, reverse=True):
            updated_text = updated_text[:start] + replacement + updated_text[end:]
        if patches:
            markdown_paths = {
                item.source_path for item, _filename, _target, _replacement in prepared
                if item.mode == ReviewMode.MARKDOWN_IMAGES and item.source_path
            }
            if len(markdown_paths) != 1:
                message = "一次应用只能更新一个 Markdown 数据源"
                for _start, _end, _replacement, item in patches:
                    report.errors[item.item_id] = message
                return report, source_text
            try:
                _atomic_write_text(next(iter(markdown_paths)), updated_text)
            except Exception as error:
                for _start, _end, _replacement, item in patches:
                    report.errors[item.item_id] = f"Markdown 写入失败: {error}"
                return report, source_text

        span_items = list(catalog_items or items)
        for start, end, replacement, item in sorted(patches, reverse=True):
            delta = len(replacement) - (end - start)
            item.original_ref = replacement
            for catalog_item in span_items:
                if catalog_item is item:
                    catalog_item.source_span = (start, start + len(replacement))
                elif catalog_item.source_span:
                    left, right = catalog_item.source_span
                    if left >= end:
                        catalog_item.source_span = (left + delta, right + delta)
                if catalog_item.context_span:
                    left, right = catalog_item.context_span
                    if left >= end:
                        catalog_item.context_span = (left + delta, right + delta)
                    elif right > start:
                        catalog_item.context_span = (left, right + delta)
                markup_start = catalog_item.metadata.get("markup_start")
                markup_end = catalog_item.metadata.get("markup_end")
                if markup_start is None or markup_end is None:
                    continue
                if catalog_item is item:
                    catalog_item.metadata["markup_end"] = int(markup_end) + delta
                elif int(markup_start) >= end:
                    catalog_item.metadata["markup_start"] = int(markup_start) + delta
                    catalog_item.metadata["markup_end"] = int(markup_end) + delta

        successful = []
        for item, filename, target_path, _replacement_ref in prepared:
            if item.item_id in report.errors:
                continue
            if target_path:
                item.output_name = filename
                if item.mode == ReviewMode.MARKDOWN_IMAGES:
                    item.original_name = filename
                    item.metadata["local_path"] = target_path
                    first = item.ordered_segments()[0] if item.segments else None
                    page_image = self.page_image_loader(first.page) if first else QImage()
                    if first and page_image is not None and not page_image.isNull():
                        x1, y1, x2, y2 = first.bbox
                        item.metadata["legacy_box"] = [
                            x1 / page_image.width(),
                            y1 / page_image.height(),
                            (x2 - x1) / page_image.width(),
                            (y2 - y1) / page_image.height(),
                        ]
                report.output_paths[item.item_id] = target_path
            report.applied.append(item.item_id)
            successful.append(item)

        if successful:
            try:
                store.save(successful, self._source_fingerprint(successful[0].source_path))
            except Exception as error:
                for item in successful:
                    report.errors[item.item_id] = f"覆盖记录保存失败: {error}"
                    item.status = "persistence_error"
                return report, updated_text
            for item in successful:
                item.dirty = False
                item.geometry_dirty = False
                item.metadata_dirty = False
                item.status = "applied"
        return report, updated_text

    def _target_for(self, item, filename):
        if item.mode == ReviewMode.MARKDOWN_IMAGES:
            source_dir = os.path.dirname(os.path.abspath(item.source_path))
            local_path = str(item.metadata.get("local_path") or "")
            if item.naming_policy == NamingPolicy.KEEP and local_path:
                return local_path, item.original_ref
            original_dir = os.path.dirname(item.original_ref.replace("\\", "/"))
            relative_dir = original_dir if original_dir and not original_dir.startswith(("http:", "https:")) else "imgs"
            target_dir = os.path.normpath(os.path.join(source_dir, relative_dir))
            target_path = os.path.join(target_dir, filename)
            replacement = os.path.relpath(target_path, source_dir).replace("\\", "/")
            return target_path, replacement
        export_dir = self.project_config.get("export_dir")
        if not export_dir:
            export_dir = os.path.dirname(os.path.abspath(item.source_path)) if item.source_path else os.getcwd()
        return os.path.join(export_dir, "output_slices", filename), filename

    @staticmethod
    def _resolve_collision(item, target_path):
        target_path = os.path.abspath(target_path)
        current_path = os.path.abspath(str(item.metadata.get("local_path") or "")) if item.metadata.get("local_path") else ""
        if not os.path.exists(target_path) or target_path == current_path:
            return target_path
        if item.output_name and os.path.basename(target_path) == item.output_name:
            return target_path
        stem, extension = os.path.splitext(target_path)
        suffix = 2
        candidate = f"{stem}_{suffix}{extension}"
        while os.path.exists(candidate):
            suffix += 1
            candidate = f"{stem}_{suffix}{extension}"
        return candidate
    @staticmethod
    def _atomic_save_image(image, target_path):
        directory = os.path.dirname(os.path.abspath(target_path))
        os.makedirs(directory, exist_ok=True)
        extension = os.path.splitext(target_path)[1] or ".jpg"
        fd, temp_path = tempfile.mkstemp(prefix=".crop-", suffix=extension, dir=directory)
        os.close(fd)
        try:
            if not image.save(temp_path, quality=96):
                raise OSError(f"图片保存失败: {target_path}")
            os.replace(temp_path, target_path)
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    @staticmethod
    def _source_fingerprint(path):
        if not path or not os.path.isfile(path):
            return {}
        stat = os.stat(path)
        return {
            "path": os.path.abspath(path),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
