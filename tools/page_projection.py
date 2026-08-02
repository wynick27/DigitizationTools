"""Page projection from paged anchors to structured dictionary entries."""

from __future__ import annotations

from dataclasses import dataclass
import difflib
import hashlib

from tools.headword_matching import (
    ExtractionResult,
    compare_headword_items,
    entry_body_segments,
    read_entry_body,
)
from tools.markup_support import build_markup_projection


@dataclass(frozen=True)
class PageProjection:
    text: str
    page: int
    entry_ids: tuple[str, ...] = ()
    matched: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    fallback_full: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    locations: tuple[dict, ...] = ()
    readonly: bool = True


@dataclass(frozen=True)
class _AlignedSlice:
    text: str
    reliable: bool
    reason: str = ""


def _digest_text(value):
    return hashlib.blake2b(str(value or "").encode("utf-8"), digest_size=16).hexdigest()


def _map_position(opcodes, source_length, target_length, position):
    position = max(0, min(int(position), source_length))
    if position == source_length:
        return target_length
    previous_source = previous_target = 0
    for tag, i1, i2, j1, j2 in opcodes:
        if position < i1:
            gap = max(1, i1 - previous_source)
            ratio = (position - previous_source) / gap
            return round(previous_target + ratio * (j1 - previous_target))
        if i1 <= position <= i2:
            if tag == "equal":
                return j1 + min(position - i1, j2 - j1)
            if i2 == i1:
                return j1
            ratio = (position - i1) / (i2 - i1)
            return round(j1 + ratio * (j2 - j1))
        previous_source, previous_target = i2, j2
    return target_length


def _slice_structured_body(anchor_parts, structured_visible, part_index, minimum_ratio):
    if len(anchor_parts) <= 1:
        return _AlignedSlice(structured_visible, True)
    anchor_text = "\n".join(anchor_parts)
    matcher = difflib.SequenceMatcher(None, anchor_text, structured_visible, autojunk=False)
    opcodes = matcher.get_opcodes()
    equal = sum(i2 - i1 for tag, i1, i2, _j1, _j2 in opcodes if tag == "equal")
    coverage = equal / max(1, len(anchor_text))
    ratio = matcher.ratio()
    if ratio < minimum_ratio or coverage < min(0.3, minimum_ratio * 0.7):
        return _AlignedSlice(
            structured_visible,
            False,
            f"alignment confidence {ratio:.1%}, coverage {coverage:.1%}",
        )
    starts = []
    cursor = 0
    for part in anchor_parts:
        starts.append(cursor)
        cursor += len(part) + 1
    start = starts[part_index]
    end = start + len(anchor_parts[part_index])
    mapped_start = _map_position(opcodes, len(anchor_text), len(structured_visible), start)
    mapped_end = _map_position(opcodes, len(anchor_text), len(structured_visible), end)
    if mapped_end < mapped_start:
        return _AlignedSlice(structured_visible, False, "non-monotonic page boundary")
    return _AlignedSlice(structured_visible[mapped_start:mapped_end], True)


class PageProjectionService:
    def __init__(self, max_cached_pages=512, minimum_alignment_ratio=0.42):
        self.max_cached_pages = max(1, int(max_cached_pages))
        self.minimum_alignment_ratio = float(minimum_alignment_ratio)
        self._cache = {}

    def clear(self):
        self._cache.clear()

    def invalidate(self, entry_ids=(), pages=()):
        entry_ids = set(str(value) for value in entry_ids)
        pages = set(int(value) for value in pages)
        for key, projection in list(self._cache.items()):
            if pages and projection.page in pages:
                self._cache.pop(key, None)
            elif entry_ids and entry_ids.intersection(projection.entry_ids):
                self._cache.pop(key, None)

    def _cache_key(self, page, anchor_pages, anchor_profile, structured_source, anchor_mode, body_mode):
        page_text = anchor_pages.get(page, "")
        try:
            source_fingerprint = structured_source.fingerprint()
        except OSError:
            source_fingerprint = (structured_source.path,)
        return (
            int(page),
            anchor_profile.fingerprint(),
            source_fingerprint,
            anchor_mode,
            body_mode,
            _digest_text(page_text),
        )

    def project(self, page, anchor_pages, anchor_result: ExtractionResult, anchor_profile,
                structured_source, anchor_mode="plain", body_mode="html"):
        key = self._cache_key(
            page, anchor_pages, anchor_profile, structured_source, anchor_mode, body_mode
        )
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        structured_index = structured_source.headword_key_index()
        pieces = []
        entry_ids = []
        matched = []
        missing = []
        fallback = []
        reasons = []
        locations = []

        for anchor in anchor_result.items:
            segments = entry_body_segments(anchor_result.items, anchor, anchor_pages)
            segment_pages = [segment_page for segment_page, _start, _end in segments]
            if page not in segment_pages and anchor.page != page:
                continue
            candidates = {
                id(item): item
                for candidate_key in (anchor.key, *anchor.aliases)
                for item in structured_index.get(candidate_key, ())
            }
            target = next(iter(candidates.values())) if len(candidates) == 1 else None
            if target is None:
                missing.append(anchor.raw)
                continue
            stable_id = str(target.metadata.get("stable_id", target.order))
            raw_body = structured_source.get_body(target.order)
            projection = build_markup_projection(raw_body, body_mode)
            if projection.errors:
                missing.append(anchor.raw)
                reasons.append(f"{anchor.raw}: {projection.errors[0].message}")
                continue
            visible_body = projection.visible_text
            selected = _AlignedSlice(visible_body, True)
            if len(segments) > 1:
                parts = [anchor_pages[part_page][start:end] for part_page, start, end in segments]
                part_index = segment_pages.index(page)
                visible_parts = [build_markup_projection(part, anchor_mode).visible_text for part in parts]
                selected = _slice_structured_body(
                    visible_parts, visible_body, part_index, self.minimum_alignment_ratio
                )
            if not selected.reliable:
                fallback.append(anchor.raw)
                reasons.append(f"{anchor.raw}: {selected.reason}")
            if selected.text:
                pieces.append(selected.text)
            entry_ids.append(stable_id)
            matched.append(anchor.raw)
            locations.append({
                "entry_id": stable_id,
                "page": page,
                "span": anchor.span,
                "bbox": anchor.metadata.get("bbox"),
                "source_item": anchor,
            })

        result = PageProjection(
            text="\n".join(pieces),
            page=int(page),
            entry_ids=tuple(entry_ids),
            matched=tuple(matched),
            missing=tuple(missing),
            fallback_full=tuple(fallback),
            reasons=tuple(reasons),
            locations=tuple(locations),
        )
        if len(self._cache) >= self.max_cached_pages:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = result
        return result