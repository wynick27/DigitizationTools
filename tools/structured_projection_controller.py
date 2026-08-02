"""Asynchronous controller for read-only structured projections in normal view."""

from __future__ import annotations

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from tools.comparison_sources import KIND_PAGED_TEXT, detect_source_kind
from tools.headword_extraction import extract_ocr_items
from tools.headword_matching import HeadwordExtractionCache
from tools.markup_support import build_markup_projection
from tools.page_projection import PageProjection, PageProjectionService
from tools.structured_sources import load_structured_source


class _ProjectionWorker(QThread):
    ready = pyqtSignal(int, object)
    failed = pyqtSignal(int, str)

    def __init__(self, request_id, paths, profiles, pages, page, ocr_items,
                 ocr_source_id, modes, sources=None, ocr_profiles=None,
                 adapters=None):
        super().__init__()
        self.request_id = request_id
        self.paths = dict(paths)
        self.profiles = dict(profiles)
        self.pages = {side: dict(value) for side, value in pages.items()}
        self.page = int(page)
        self.ocr_items = tuple(ocr_items or ())
        self.ocr_source_id = ocr_source_id
        self.modes = dict(modes)
        self.sources = dict(sources or {})
        self.adapters = dict(adapters or {})
        self.ocr_profiles = dict(ocr_profiles or self.profiles)

    def _structured(self, side):
        adapter = self.adapters.get(side)
        if adapter is not None:
            return adapter.structured
        path = self.paths.get(side, "")
        return bool(path and detect_source_kind(path) != KIND_PAGED_TEXT)

    def _load_source(self, side):
        if not self._structured(side):
            return None
        adapter = self.adapters.get(side)
        if adapter is not None:
            return adapter.ensure_loaded()
        source = self.sources.get(side)
        if source is None or source.path != self.paths[side] or source.profile.fingerprint() != self.profiles[side].fingerprint():
            source = load_structured_source(self.paths[side], self.profiles[side])
        return source

    @staticmethod
    def _direct_projection(page, anchor_items, source, mode):
        index = source.headword_key_index()
        pieces, entry_ids, matched, missing, reasons = [], [], [], [], []
        locations = []
        for anchor in anchor_items:
            candidates = {id(item): item for key in (anchor.key, *anchor.aliases) for item in index.get(key, ())}
            if len(candidates) != 1:
                missing.append(anchor.raw)
                if len(candidates) > 1:
                    reasons.append(f"{anchor.raw}: ambiguous structured key")
                continue
            item = next(iter(candidates.values()))
            body = source.get_body(item.order)
            projection = build_markup_projection(body, mode)
            if projection.errors:
                missing.append(anchor.raw)
                reasons.append(f"{anchor.raw}: {projection.errors[0].display()}")
                continue
            pieces.append(projection.visible_text)
            stable = str(item.metadata.get("stable_id", item.order))
            entry_ids.append(stable); matched.append(anchor.raw)
            locations.append({"entry_id": stable, "page": page, "span": anchor.span, "bbox": anchor.metadata.get("bbox")})
        return PageProjection(
            "\n".join(pieces), page, tuple(entry_ids), tuple(matched), tuple(missing),
            (), tuple(reasons), tuple(locations), True,
        )

    def run(self):
        try:
            structured = {side: self._structured(side) for side in ("left", "right")}
            if not any(structured.values()):
                self.ready.emit(self.request_id, {"active": False, "sources": {}})
                return
            sources = {side: self._load_source(side) for side in ("left", "right")}
            if self.isInterruptionRequested():
                return
            extraction_cache = HeadwordExtractionCache()
            projection_service = PageProjectionService()
            projections = {"left": None, "right": None}
            reasons = []
            for side in ("left", "right"):
                if not structured[side]:
                    continue
                other = "right" if side == "left" else "left"
                if not structured[other]:
                    if self.pages[other]:
                        anchor_result = extraction_cache.extract_pages(
                            self.paths.get(other) or other,
                            self.pages[other],
                            self.profiles[other],
                        )
                        projections[side] = projection_service.project(
                            self.page,
                            self.pages[other],
                            anchor_result,
                            self.profiles[other],
                            sources[side],
                            self.modes.get(other, "plain"),
                            self.modes.get(side, "html"),
                        )
                    elif self.ocr_items:
                        anchor_result = extract_ocr_items(
                            self.ocr_items, self.ocr_profiles[side], self.page, self.ocr_source_id
                        )
                        projections[side] = self._direct_projection(
                            self.page, anchor_result.items, sources[side], self.modes.get(side, "html")
                        )
                    else:
                        projections[side] = PageProjection(
                            "", self.page, reasons=("structured projection needs page or OCR headword anchors",)
                        )
                elif self.ocr_items:
                    anchor_result = extract_ocr_items(
                        self.ocr_items, self.ocr_profiles[side], self.page, self.ocr_source_id
                    )
                    projections[side] = self._direct_projection(
                        self.page, anchor_result.items, sources[side], self.modes.get(side, "html")
                    )
                else:
                    projections[side] = PageProjection(
                        "", self.page, reasons=("both sides are structured; current page needs OCR headword anchors",)
                    )
                reasons.extend(projections[side].reasons)
                if projections[side].fallback_full:
                    reasons.append(
                        f"{side}: {len(projections[side].fallback_full)} entries fell back to full body"
                    )
            self.ready.emit(self.request_id, {
                "active": True,
                "page": self.page,
                "left": projections["left"],
                "right": projections["right"],
                "readonly_left": structured["left"],
                "readonly_right": structured["right"],
                "sources": sources,
                "reasons": tuple(dict.fromkeys(reasons)),
            })
        except Exception as exc:
            self.failed.emit(self.request_id, str(exc))


class StructuredProjectionController(QObject):
    projection_ready = pyqtSignal(object)
    projection_failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.paths = {"left": "", "right": ""}
        self.profiles = {}
        self.sources = {}
        self.adapters = {}
        self.worker = None
        self._retired_workers = []
        self.request_id = 0

    def configure(self, left_path, right_path, left_profile, right_profile, adapters=None):
        paths = {"left": left_path or "", "right": right_path or ""}
        profiles = {"left": left_profile, "right": right_profile}
        changed = paths != self.paths or any(
            side not in self.profiles or self.profiles[side].fingerprint() != profiles[side].fingerprint()
            for side in ("left", "right")
        )
        incoming_adapters = dict(adapters or {})
        adapters_changed = any(
            incoming_adapters.get(side) is not self.adapters.get(side)
            for side in ("left", "right")
        )
        self.paths, self.profiles = paths, profiles
        self.adapters = incoming_adapters
        if changed or adapters_changed:
            self.cancel()
            self.sources = {}

    def is_structured(self, side):
        path = self.paths.get(side, "")
        return bool(path and detect_source_kind(path) != KIND_PAGED_TEXT)
    def has_structured_side(self):
        return any(path and detect_source_kind(path) != KIND_PAGED_TEXT for path in self.paths.values())

    def request(self, page, left_pages, right_pages, ocr_items=(), ocr_source_id="", modes=None, ocr_profiles=None):
        self.cancel()
        self.request_id += 1
        request_id = self.request_id
        self.worker = _ProjectionWorker(
            request_id, self.paths, self.profiles,
            {"left": left_pages, "right": right_pages}, page,
            ocr_items, ocr_source_id, modes or {}, self.sources, ocr_profiles,
            self.adapters,
        )
        self.worker.ready.connect(self._on_ready)
        self.worker.failed.connect(self._on_failed)
        self.worker.finished.connect(self._on_finished)
        self.worker.start()
        return request_id

    def cancel(self):
        self.request_id += 1
        if self.worker and self.worker.isRunning():
            retired = self.worker
            retired.requestInterruption()
            self._retired_workers.append(retired)
            retired.finished.connect(lambda worker=retired: self._release_retired(worker))
            self.worker = None

    def _release_retired(self, worker):
        if worker in self._retired_workers:
            self._retired_workers.remove(worker)
        worker.deleteLater()

    def _on_ready(self, request_id, payload):
        if request_id != self.request_id:
            return
        self.sources = payload.get("sources", self.sources)
        self.projection_ready.emit(payload)

    def _on_failed(self, request_id, message):
        if request_id == self.request_id:
            self.projection_failed.emit(message)

    def _on_finished(self):
        if self.sender() is self.worker:
            self.worker = None

    def shutdown(self):
        self.cancel()
        for worker in list(self._retired_workers):
            if worker.isRunning():
                worker.requestInterruption()
                worker.wait(1500)
        self._retired_workers.clear()