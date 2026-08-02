from __future__ import annotations

import copy
import hashlib
import os
import re
import threading
import unicodedata

from PyQt6.QtCore import QThread, pyqtSignal

from ocr.ocr_utils import BBoxMerger, TextToBBoxMapper
from tools.workspace_views import ImageCoordinateMapper

from .models import CropSegment, OcrMatchResult


class OcrMatchCache:
    """Session cache keyed by source text and the OCR files used by an entry."""

    def __init__(self):
        self._items = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(item, ocr_dir, page_offset):
        digest = hashlib.sha1()
        digest.update(str(item.metadata.get("text") or "").encode("utf-8", errors="replace"))
        digest.update(str(int(page_offset or 0)).encode("ascii"))
        for page in item.metadata.get("pages") or ():
            real_page = int(page) + int(page_offset or 0)
            for filename in (f"page_{real_page}.json", f"{real_page}.json"):
                path = os.path.join(ocr_dir, filename)
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                digest.update(os.path.abspath(path).encode("utf-8", errors="replace"))
                digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode("ascii"))
                break
        return item.item_id, digest.hexdigest()

    def get(self, key):
        with self._lock:
            return self._items.get(key)

    def put(self, key, result):
        with self._lock:
            self._items[key] = result

    def clear(self):
        with self._lock:
            self._items.clear()


class PreloadedOcrMapper:
    """Read-only mapper for OCR blocks normalized on the GUI thread."""

    def __init__(self, pages):
        self.pages = {
            int(page): tuple(dict(block) for block in blocks)
            for page, blocks in (pages or {}).items()
        }

    def load_page_data(self, page_num):
        return self.pages.get(int(page_num), ())

    def get_page_text_map(self, page_num):
        blocks = list(self.load_page_data(page_num))
        full_text = ""
        char_map = []
        for index, block in enumerate(blocks):
            text = str(block.get("text") or "")
            full_text += text
            char_map.extend([index] * len(text))
        return blocks, full_text, char_map

    find_bboxes = TextToBBoxMapper.find_bboxes

class LoadedOcrMapper(TextToBBoxMapper):
    """Text mapper backed by an explicitly selected normalized OCR result."""

    def __init__(self, page_loader, page_size_loader):
        self.page_loader = page_loader
        self.page_size_loader = page_size_loader
        self.cache = {}

    def load_page_data(self, page_num):
        if page_num in self.cache:
            return self.cache[page_num]
        width, height = self.page_size_loader(page_num)
        blocks = []
        for item in self.page_loader(page_num) or ():
            if not isinstance(item, dict):
                continue
            bbox = ImageCoordinateMapper.to_pixels(
                item.get("bbox"),
                item.get("bbox_coordinate_type"),
                width,
                height,
            )
            text = str(item.get("text") or "")
            if not text or not bbox:
                continue
            blocks.append({
                "text": text,
                "bbox": list(bbox),
                "block_label": item.get("block_label") or "text",
            })
        self.cache[page_num] = blocks
        return blocks

MATCH_CACHE = OcrMatchCache()


def match_entry(item, mapper, merger=None):
    text = str(item.metadata.get("text") or "")
    pages = tuple(int(page) for page in item.metadata.get("pages") or ())
    if not text.strip():
        return OcrMatchResult(item.item_id, status="unmatched", reason="词条正文为空")
    if not pages:
        return OcrMatchResult(item.item_id, status="unmatched", reason="词条没有关联页码")
    raw = mapper.find_bboxes(text, pages)
    if not raw:
        return OcrMatchResult(
            item.item_id,
            status="unmatched",
            reason="OCR 数据中未找到可用的 bbox",
        )
    merged = (merger or BBoxMerger()).merge(raw)
    orientation = (
        "vertical"
        if any(box.get("label") == "vertical_text" for box in raw)
        else "horizontal"
    )
    segments = tuple(
        CropSegment(
            f"{item.item_id}:{index}",
            int(box["page"]),
            (
                float(box["x"]),
                float(box["y"]),
                float(box["x"] + box["w"]),
                float(box["y"] + box["h"]),
            ),
            order=index,
            label="text",
            origin="ocr_auto",
        )
        for index, box in enumerate(merged)
    )
    return OcrMatchResult(
        item.item_id,
        segments,
        status="matched",
        confidence=1.0,
        reason=f"找到 {len(raw)} 个 OCR 框，合并为 {len(segments)} 个片段",
        orientation=orientation,
    )


def _match_text(value):
    value = unicodedata.normalize("NFC", str(value or "")).casefold()
    return re.sub(r"\s+", "", value)


def _text_ngrams(value):
    size = 2 if len(value) < 8 else 3
    if len(value) <= size:
        return frozenset((value,)) if value else frozenset()
    return frozenset(value[index:index + size] for index in range(len(value) - size + 1))


def _coverage_score(entry_text, entry_grams, headword, block_text):
    block_text = _match_text(block_text)
    if not entry_text or not block_text:
        return 0.0
    headword_bonus = 0.1 if headword and headword in block_text else 0.0
    if block_text in entry_text:
        return min(1.0, 0.9 + headword_bonus)
    if entry_text in block_text:
        ratio = len(entry_text) / max(1, len(block_text))
        return min(1.0, 0.65 + ratio * 0.25 + headword_bonus)
    block_grams = _text_ngrams(block_text)
    if not block_grams:
        return 0.0
    overlap = len(block_grams.intersection(entry_grams)) / len(block_grams)
    length_balance = min(len(block_text), len(entry_text)) / max(
        len(block_text), len(entry_text)
    )
    return min(1.0, overlap * 0.82 + length_balance * 0.08 + headword_bonus)

def _overlap_ratio(bbox, reserved):
    x1, y1, x2, y2 = (float(value) for value in bbox)
    area = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if area <= 0:
        return 0.0
    rx1, ry1, rx2, ry2 = (float(value) for value in reserved)
    overlap = max(0.0, min(x2, rx2) - max(x1, rx1)) * max(
        0.0, min(y2, ry2) - max(y1, ry1)
    )
    return overlap / area


def match_entries_global(
    items, mapper, merger=None, reserved_by_page=None, threshold=0.34,
    page_filter=None, cancelled=None,
):
    """Assign each OCR block once using precomputed visible-text n-grams."""
    items = list(items)
    merger = merger or BBoxMerger()
    reserved_by_page = {
        int(page): tuple(boxes) for page, boxes in (reserved_by_page or {}).items()
    }
    assignments = {item.item_id: [] for item in items}
    scores = {item.item_id: [] for item in items}
    pages = (
        [int(page_filter)]
        if page_filter is not None
        else sorted({
            int(page)
            for item in items
            for page in (item.metadata.get("pages") or ())
            if int(page) > 0
        })
    )
    for page in pages:
        if cancelled is not None and cancelled():
            return {}
        page_items = [
            item for item in items if page in tuple(item.metadata.get("pages") or ())
        ]
        page_items.sort(key=lambda item: (item.sequence, item.label, item.item_id))
        prepared = []
        for order, item in enumerate(page_items):
            entry_text = _match_text(
                item.metadata.get("match_text")
                or item.metadata.get("text")
                or ""
            )
            prepared.append((
                order,
                item,
                entry_text,
                _text_ngrams(entry_text),
                _match_text(item.metadata.get("headword") or item.label),
            ))

        for block_index, block in enumerate(mapper.load_page_data(page) or ()):
            if cancelled is not None and cancelled():
                return {}
            bbox = block.get("bbox")
            if not bbox or any(
                _overlap_ratio(bbox, reserved) >= 0.5
                for reserved in reserved_by_page.get(page, ())
            ):
                continue
            block_text = str(block.get("text") or "")
            ranked = [
                (
                    _coverage_score(entry_text, entry_grams, headword, block_text),
                    -order,
                    item,
                )
                for order, item, entry_text, entry_grams, headword in prepared
            ]
            if not ranked:
                continue
            score, _tie, owner = max(ranked, key=lambda value: value[:2])
            if score < float(threshold):
                continue
            assignments[owner.item_id].append({
                "bbox": list(bbox),
                "page": page,
                "label": block.get("block_label") or "text",
                "sort_key": (page, block_index),
            })
            scores[owner.item_id].append(score)

    results = {}
    for item in items:
        raw = assignments[item.item_id]
        merged = merger.merge(raw)
        orientation = (
            "vertical"
            if any(box.get("label") == "vertical_text" for box in raw)
            else "horizontal"
        )
        segments = tuple(
            CropSegment(
                f"{item.item_id}:ocr:{index}",
                int(box["page"]),
                (
                    float(box["x"]), float(box["y"]),
                    float(box["x"] + box["w"]),
                    float(box["y"] + box["h"]),
                ),
                order=index,
                label="text",
                origin="ocr_auto",
            )
            for index, box in enumerate(merged)
        )
        confidence = (
            sum(scores[item.item_id]) / len(scores[item.item_id])
            if scores[item.item_id] else 0.0
        )
        results[item.item_id] = OcrMatchResult(
            item.item_id,
            segments,
            status="matched" if segments else "unmatched",
            confidence=confidence,
            reason=(
                f"独占分配 {len(raw)} 个 OCR 框，合并为 {len(segments)} 个片段"
                if segments else "本页 OCR 框未达到该词条的匹配阈值"
            ),
            orientation=orientation,
        )
    return results

class OcrMatchWorker(QThread):
    matched = pyqtSignal(int, str, object)
    progress = pyqtSignal(int, int)
    completed = pyqtSignal(int, bool)

    def __init__(
        self,
        request_id,
        items,
        ocr_dir,
        page_offset=0,
        preferred_page=0,
        parent=None,
        page_blocks=None,
        source_fingerprint="",
    ):
        super().__init__(parent)
        self.request_id = int(request_id)
        self.items = list(items)
        self.ocr_dir = str(ocr_dir or "")
        self.page_offset = int(page_offset or 0)
        self.preferred_page = int(preferred_page or 0)
        self.page_blocks = dict(page_blocks or {})
        self.source_fingerprint = str(source_fingerprint or "")
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        mapper = (
            PreloadedOcrMapper(self.page_blocks)
            if self.page_blocks
            else TextToBBoxMapper(self.ocr_dir, self.page_offset)
        )
        pending = []
        reserved = {}
        for item in self.items:
            protected = item.segments and item.status in {
                "applied", "manual", "modified"
            }
            if protected:
                for segment in item.segments:
                    reserved.setdefault(segment.page, []).append(segment.bbox)
            else:
                pending.append(item)
        if self._cancelled or self.isInterruptionRequested():
            self.completed.emit(self.request_id, True)
            return
        reserved_key = tuple(
            (int(page), tuple(
                tuple(round(float(value), 2) for value in bbox)
                for bbox in boxes
            ))
            for page, boxes in sorted(reserved.items())
        )
        batch_key = (
            "global-v4",
            self.source_fingerprint,
            self.preferred_page,
            tuple(
                MATCH_CACHE.key(item, self.ocr_dir, self.page_offset)
                for item in pending
            ),
            reserved_key,
        )
        results = MATCH_CACHE.get(batch_key)
        if results is None:
            results = match_entries_global(
                pending,
                mapper,
                BBoxMerger(),
                reserved_by_page=reserved,
                page_filter=self.preferred_page or None,
                cancelled=lambda: (
                    self._cancelled or self.isInterruptionRequested()
                ),
            )
            if self._cancelled or self.isInterruptionRequested():
                self.completed.emit(self.request_id, True)
                return
            MATCH_CACHE.put(batch_key, results)
        total = len(pending)
        for index, item in enumerate(pending, 1):
            if self._cancelled or self.isInterruptionRequested():
                self.completed.emit(self.request_id, True)
                return
            self.matched.emit(
                self.request_id, item.item_id,
                copy.deepcopy(results[item.item_id]),
            )
            self.progress.emit(index, total)
        self.completed.emit(self.request_id, False)

