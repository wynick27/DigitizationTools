"""OCR block adaptation and paragraph-first headword extraction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from tools.headword_matching import (
    ExtractionResult,
    HeadwordItem,
    HeadwordProfileError,
    compile_profile,
    normalize_headword,
)
from tools.headword_rules import HeadwordProfile, SCOPE_LINE


@dataclass(frozen=True)
class OCRTextPart:
    text: str
    start: int
    end: int
    bbox: tuple[float, ...] = ()
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class OCRTextBlock:
    text: str
    bbox: tuple[float, ...] = ()
    granularity: str = "paragraph"
    parts: tuple[OCRTextPart, ...] = ()
    source_index: int = 0
    metadata: dict = field(default_factory=dict)


def _bbox(value):
    if isinstance(value, (list, tuple)) and len(value) >= 4:
        try:
            return tuple(float(item) for item in value)
        except (TypeError, ValueError):
            return ()
    return ()


def _part_from_value(value, fallback_start=0):
    if not isinstance(value, dict):
        return None
    text = str(value.get("text", "") or "")
    if not text:
        return None
    start = value.get("start_index", value.get("start", fallback_start))
    end = value.get("end_index", value.get("end", int(start) + len(text)))
    try:
        start, end = int(start), int(end)
    except (TypeError, ValueError):
        start, end = fallback_start, fallback_start + len(text)
    return OCRTextPart(text, start, end, _bbox(value.get("bbox")), dict(value))


def ocr_blocks_from_items(items: Iterable, page=None, source_id=""):
    """Convert normalized OCR output to engine-independent text blocks."""
    blocks = []
    for index, value in enumerate(items or ()):
        if isinstance(value, dict):
            text = str(value.get("text", "") or "")
            granularity = str(
                value.get("granularity")
                or value.get("type")
                or value.get("block_type")
                or ("paragraph" if value.get("sub_items") else "line")
            ).lower()
            raw_parts = value.get("sub_items") or value.get("lines") or value.get("words") or ()
            parts = []
            cursor = 0
            for raw_part in raw_parts:
                part = _part_from_value(raw_part, cursor)
                if part is not None:
                    parts.append(part)
                    cursor = max(cursor, part.end + 1)
            metadata = dict(value)
            metadata.update({"page": page, "source_id": source_id})
            blocks.append(
                OCRTextBlock(
                    text=text,
                    bbox=_bbox(value.get("bbox")),
                    granularity=granularity,
                    parts=tuple(parts),
                    source_index=index,
                    metadata=metadata,
                )
            )
            continue
        if isinstance(value, (list, tuple)) and len(value) == 2:
            points, recognized = value
            text = str(recognized[0] if isinstance(recognized, (list, tuple)) else recognized)
            xs = [point[0] for point in points or () if len(point) >= 2]
            ys = [point[1] for point in points or () if len(point) >= 2]
            bbox = (min(xs), min(ys), max(xs), max(ys)) if xs and ys else ()
            blocks.append(OCRTextBlock(text, bbox, "line", (), index, {"page": page, "source_id": source_id}))
    return tuple(blocks)


def _first_nonempty_line(text):
    offset = 0
    for line in str(text or "").splitlines(True):
        stripped_eol = line.rstrip("\r\n")
        leading = len(stripped_eol) - len(stripped_eol.lstrip())
        if stripped_eol.strip():
            return stripped_eol[leading:], offset + leading
        offset += len(line)
    stripped = str(text or "").strip()
    if stripped:
        start = str(text).find(stripped)
        return stripped, start
    return "", 0


def _match_candidate(text, base_offset, block, extractor, ignores, filters, profile, order, page, part=None):
    match = extractor.search(text)
    if match is None:
        return None, False
    try:
        raw = match.group(profile.group)
        start = match.start(profile.group)
        end = match.end(profile.group)
    except (IndexError, KeyError) as exc:
        raise HeadwordProfileError(f"\u6355\u83b7\u7ec4\u4e0d\u5b58\u5728: {profile.group}") from exc
    if any(regex.search(raw) for regex in ignores):
        return None, True
    key, hits = normalize_headword(raw, filters)
    bbox = part.bbox if part is not None and part.bbox else block.bbox
    metadata = {
        "block_index": block.source_index,
        "granularity": block.granularity,
        "bbox": bbox,
        "entry_start": base_offset + match.start(0),
        "entry_end": base_offset + match.end(0),
        "source_id": block.metadata.get("source_id", ""),
    }
    if part is not None:
        metadata["part_start"] = part.start
        metadata["part_end"] = part.end
    item = HeadwordItem(
        raw=raw,
        key=key,
        page=page,
        start=base_offset + start,
        end=base_offset + end,
        order=order,
        filter_hits=hits,
        source_id=str(block.metadata.get("source_id", "")),
        metadata=metadata,
    )
    return item, True


def extract_ocr_headwords(blocks, profile: HeadwordProfile, page=None, order_start=0):
    """Extract only from block starts, with an explicit line fallback."""
    extractor, ignores, filters = compile_profile(profile)
    items = []
    unmatched = 0
    empty_keys = 0
    order = order_start
    for block in blocks or ():
        candidates = []
        if profile.extraction_scope == SCOPE_LINE or block.granularity in ("line", "word"):
            if block.parts:
                candidates.extend((part.text, part.start, part) for part in block.parts)
            else:
                first, offset = _first_nonempty_line(block.text)
                candidates.append((first, offset, None))
        else:
            first_part = next((part for part in block.parts if part.text.strip()), None)
            if first_part is not None:
                candidates.append((first_part.text, first_part.start, first_part))
            else:
                first, offset = _first_nonempty_line(block.text)
                candidates.append((first, offset, None))
            if profile.line_fallback and block.parts:
                candidates.extend((part.text, part.start, part) for part in block.parts)

        matched_or_ignored = False
        seen_ranges = set()
        for candidate, offset, part in candidates:
            marker = (offset, candidate)
            if not candidate or marker in seen_ranges:
                continue
            seen_ranges.add(marker)
            item, attempted = _match_candidate(
                candidate, offset, block, extractor, ignores, filters,
                profile, order, page, part,
            )
            matched_or_ignored = matched_or_ignored or attempted
            if item is None:
                continue
            items.append(item)
            empty_keys += int(not item.key)
            order += 1
            break
        if not matched_or_ignored and block.text.strip():
            unmatched += 1
    return ExtractionResult(items, unmatched, empty_keys)


def extract_ocr_items(items, profile, page=None, source_id=""):
    return extract_ocr_headwords(
        ocr_blocks_from_items(items, page=page, source_id=source_id),
        profile,
        page=page,
    )