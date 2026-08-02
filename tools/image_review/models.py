from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ReviewMode(str, Enum):
    MARKDOWN_IMAGES = "markdown_images"
    DICTIONARY_SLICES = "dictionary_slices"


class NamingPolicy(str, Enum):
    KEEP = "keep"
    PAGE_SEQUENCE = "page_sequence"
    PAGE_BBOX = "page_bbox"


def normalized_bbox(values) -> tuple[float, float, float, float]:
    if not values or len(values) != 4:
        return (0.0, 0.0, 0.0, 0.0)
    x1, y1, x2, y2 = (float(value) for value in values)
    return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))


@dataclass
class CropSegment:
    segment_id: str
    page: int
    bbox: tuple[float, float, float, float]
    source_page: int | None = None
    order: int = 0
    coordinate_type: str = "absolute"
    label: str = "text"
    origin: str = "manual"

    def __post_init__(self):
        self.page = int(self.page or 0)
        self.source_page = int(self.source_page) if self.source_page is not None else None
        self.order = int(self.order or 0)
        self.bbox = normalized_bbox(self.bbox)

    @property
    def valid(self) -> bool:
        x1, y1, x2, y2 = self.bbox
        return self.page > 0 and x2 > x1 and y2 > y1

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.segment_id,
            "page": self.page,
            "source_page": self.source_page,
            "bbox": [round(value, 4) for value in self.bbox],
            "order": self.order,
            "coordinate_type": self.coordinate_type,
            "label": self.label,
            "origin": self.origin,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any], fallback_id="segment"):
        return cls(
            str(value.get("id") or fallback_id),
            int(value.get("page") or value.get("pdf_page") or 0),
            tuple(value.get("bbox") or value.get("box") or ()),
            value.get("source_page") or value.get("pdf_page"),
            int(value.get("order") or 0),
            str(value.get("coordinate_type") or "absolute"),
            str(value.get("label") or "text"),
            str(value.get("origin") or ("ocr_auto" if ":ocr:" in str(value.get("id") or fallback_id) else "manual")),
        )


@dataclass(frozen=True)
class EntryRecord:
    entry_id: str
    headword: str
    text: str = ""
    pages: tuple[int, ...] = ()
    page_index: int = 0
    side: str = "left"
    aliases: tuple[str, ...] = ()


@dataclass
class ImageRecord:
    record_id: str
    filename: str
    page: int
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    entry_id: str = ""
    headword: str = ""
    caption: str = ""
    action: str = "attach"
    order: int = 0
    local_path: str = ""
    status: str = "unreviewed"
    confidence: str = ""
    reason: str = ""
    layout_caption: str = ""
    dirty: bool = False

    @property
    def ignored(self) -> bool:
        return self.action == "ignore"


@dataclass(frozen=True)
class OcrMatchResult:
    entry_id: str
    segments: tuple[CropSegment, ...] = ()
    status: str = "unmatched"
    confidence: float = 0.0
    reason: str = ""
    orientation: str = "horizontal"
    manual: bool = False


@dataclass(frozen=True)
class EntryPreview:
    entry_id: str
    headword: str
    text: str
    text_image: Any = None
    images: tuple[ImageRecord, ...] = ()


@dataclass
class ReviewItem:
    item_id: str
    mode: ReviewMode
    label: str
    page: int
    segments: list[CropSegment] = field(default_factory=list)
    original_ref: str = ""
    original_name: str = ""
    source_path: str = ""
    source_span: tuple[int, int] | None = None
    context_span: tuple[int, int] | None = None
    sequence: int = 1
    status: str = "unreviewed"
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    naming_policy: NamingPolicy = NamingPolicy.KEEP
    output_name: str = ""
    dirty: bool = False
    geometry_dirty: bool = False
    metadata_dirty: bool = False

    def ordered_segments(self) -> list[CropSegment]:
        return sorted(self.segments, key=lambda segment: (segment.order, segment.page))

    def segment_for_page(self, page: int) -> list[CropSegment]:
        return [segment for segment in self.ordered_segments() if segment.page == page]

    def to_override(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "label": self.label,
            "page": self.page,
            "segments": [segment.to_dict() for segment in self.ordered_segments()],
            "original_ref": self.original_ref,
            "original_name": self.original_name,
            "sequence": self.sequence,
            "naming_policy": self.naming_policy.value,
            "output_name": self.output_name,
            "status": "applied",
            "metadata": {
                key: value
                for key, value in self.metadata.items()
                if key in {
                    "entry_id", "headword", "side", "orientation", "caption",
                    "action", "image_order", "confidence", "reason", "local_path",
                    "ocr_source", "ocr_source_identity",
                }
            },
        }

    def apply_override(self, value: dict[str, Any]):
        raw_segments = value.get("segments")
        if raw_segments is not None:
            self.segments = [
                CropSegment.from_dict(segment, f"{self.item_id}:{index}")
                for index, segment in enumerate(raw_segments)
                if isinstance(segment, dict)
            ]
        try:
            self.naming_policy = NamingPolicy(value.get("naming_policy", self.naming_policy.value))
        except ValueError:
            pass
        self.output_name = str(value.get("output_name") or self.output_name)
        self.sequence = int(value.get("sequence") or self.sequence)
        self.status = str(value.get("status") or "applied")
        metadata = value.get("metadata")
        if isinstance(metadata, dict):
            self.metadata.update(metadata)
        self.dirty = False
        self.geometry_dirty = False
        self.metadata_dirty = False

    @property
    def entry_id(self) -> str:
        return str(self.metadata.get("entry_id") or "")

    @property
    def caption(self) -> str:
        return str(self.metadata.get("caption") or "")

    @property
    def ignored(self) -> bool:
        return self.metadata.get("action") == "ignore"

    def mark_geometry_dirty(self):
        self.dirty = True
        self.geometry_dirty = True

    def mark_metadata_dirty(self):
        self.dirty = True
        self.metadata_dirty = True


@dataclass
class ApplyReport:
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    output_paths: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def message(self) -> str:
        parts = [f"已应用 {len(self.applied)} 项"]
        if self.skipped:
            parts.append(f"跳过 {len(self.skipped)} 项")
        if self.errors:
            parts.append(f"失败 {len(self.errors)} 项")
        return "，".join(parts)
