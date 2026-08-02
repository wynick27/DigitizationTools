from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import unquote

from .models import CropSegment, ReviewItem, ReviewMode


_PAGE_MARKER_RE = re.compile(r"(?m)^\s*<(\d+)>\s*$")
_BBOX_NAME_RE = re.compile(r"_(\d+)_(\d+)_(\d+)_(\d+)(?=\.[^.]+$)")
_PAGE_NAME_RE = re.compile(r"(?:^|_)page[_-]?(\d+)(?:_|$)", re.I)
_AUDIT_BBOX_RE = re.compile(r"_(\d+)_(\d+)_(\d+)_(\d+)(?=\.[^.]+$)")
_MARKDOWN_IMAGE_RE = re.compile(
    r"!\[[^\]]*\]\(\s*(?P<src><[^>]+>|[^\s)]+)(?:\s+['\"][^'\"]*['\"])?\s*\)",
    re.S,
)
_HTML_IMAGE_RE = re.compile(
    r"<img\b[^>]*?\bsrc\s*=\s*(?P<quote>['\"])(?P<src>.*?)(?P=quote)[^>]*>",
    re.I | re.S,
)


def _stable_id(*parts) -> str:
    payload = "\0".join(str(part) for part in parts)
    return hashlib.sha1(payload.encode("utf-8", errors="replace")).hexdigest()[:20]


def dictionary_item_id(source_path, side, headword, page, page_index):
    return _stable_id(
        os.path.abspath(source_path) if source_path else side,
        headword,
        page,
        page_index,
    )


def _page_at(markers, position):
    page = 0
    for marker_position, marker_page in markers:
        if marker_position > position:
            break
        page = marker_page
    return page


def _context_span(text, start, end, markers):
    left = max((position for position, _page in markers if position <= start), default=max(0, start - 1200))
    right = min((position for position, _page in markers if position > end), default=min(len(text), end + 1200))
    return (left, right)


def default_override_path(source_path, mode, project_config=None, side="left"):
    project_config = project_config or {}
    configured_key = (
        "image_review_overrides_path"
        if mode == ReviewMode.MARKDOWN_IMAGES
        else f"slice_review_overrides_{side}_path"
    )
    configured = str(project_config.get(configured_key) or "")
    if configured:
        return configured
    source_path = str(source_path or "")
    if source_path:
        suffix = ".image-review.json" if mode == ReviewMode.MARKDOWN_IMAGES else ".slice-review.json"
        return source_path + suffix
    base = project_config.get("ocr_json_path") or project_config.get("export_dir") or os.getcwd()
    filename = "markdown-image-review.json" if mode == ReviewMode.MARKDOWN_IMAGES else f"{side}-slice-review.json"
    return os.path.join(base, filename)


class MarkdownImageSource:
    def __init__(self, path, image_base_dir=""):
        self.path = str(path or "")
        self.text = ""
        document_dir = os.path.dirname(os.path.abspath(self.path)) if self.path else os.getcwd()
        configured = str(image_base_dir or "").strip()
        self.image_base_dir = (
            os.path.abspath(configured)
            if configured and os.path.isabs(configured)
            else os.path.normpath(os.path.join(document_dir, configured))
            if configured else document_dir
        )

    def resolve_local_path(self, source):
        clean = unquote(str(source or "").split("?", 1)[0].split("#", 1)[0])
        if not clean or re.match(r"^(?:https?|data):", clean, re.I):
            return ""
        if clean.lower().startswith("file://"):
            clean = clean[7:].lstrip("/") if os.name == "nt" else clean[7:]
        clean = clean.replace("/", os.sep)
        if os.path.isabs(clean):
            return os.path.normpath(clean)
        return os.path.normpath(os.path.join(self.image_base_dir, clean))

    def scan(self):
        if not self.path or not os.path.isfile(self.path):
            return []
        self.text = Path(self.path).read_text(encoding="utf-8")
        markers = [(match.start(), int(match.group(1))) for match in _PAGE_MARKER_RE.finditer(self.text)]
        matches = list(_MARKDOWN_IMAGE_RE.finditer(self.text)) + list(_HTML_IMAGE_RE.finditer(self.text))
        matches.sort(key=lambda match: match.start())
        page_sequences = {}
        items = []
        for match in matches:
            raw_src = match.group("src")
            src = raw_src[1:-1] if raw_src.startswith("<") and raw_src.endswith(">") else raw_src
            src_start, src_end = match.span("src")
            if raw_src.startswith("<"):
                src_start += 1
                src_end -= 1
            page = _page_at(markers, match.start())
            name = os.path.basename(src.split("?", 1)[0].replace("\\", "/"))
            if page <= 0 and (page_match := _PAGE_NAME_RE.search(name)):
                page = int(page_match.group(1))
            page_sequences[page] = page_sequences.get(page, 0) + 1
            item_id = _stable_id(
                os.path.abspath(self.path), page, page_sequences[page]
            )
            segments = []
            bbox_match = _BBOX_NAME_RE.search(name)
            if bbox_match and page > 0:
                segments.append(CropSegment(
                    f"{item_id}:0",
                    page,
                    tuple(float(value) for value in bbox_match.groups()),
                    order=0,
                    label="image",
                ))
            local_path = self.resolve_local_path(src)
            missing_local = bool(local_path and not os.path.isfile(local_path))
            items.append(ReviewItem(
                item_id=item_id,
                mode=ReviewMode.MARKDOWN_IMAGES,
                label=name or src,
                page=page,
                segments=segments,
                original_ref=src,
                original_name=name,
                source_path=self.path,
                source_span=(src_start, src_end),
                context_span=_context_span(self.text, match.start(), match.end(), markers),
                sequence=page_sequences[page],
                status=(
                    "missing_file" if missing_local
                    else "unreviewed" if segments
                    else "missing_bbox"
                ),
                error=(
                    f"图片不存在：{local_path}" if missing_local
                    else "" if page > 0
                    else "无法确定图片所属页"
                ),
                metadata={
                    "local_path": local_path,
                    "markup_start": match.start(),
                    "markup_end": match.end(),
                },
            ))
        return items


def discover_image_audit_files(source_path, project_config=None):
    project_config = project_config or {}
    source_path = os.path.abspath(str(source_path or "")) if source_path else ""
    source_dir = os.path.dirname(source_path) if source_path else os.getcwd()
    map_path = str(
        project_config.get("image_review_map_path")
        or project_config.get("image_map_path")
        or ""
    )
    if not map_path:
        candidates = sorted(Path(source_dir).glob("*_image_map.tsv"))
        if len(candidates) == 1:
            map_path = str(candidates[0])
    override_path = str(project_config.get("image_review_overrides_path") or "")
    if not override_path and map_path:
        candidate = re.sub(r"_image_map\.tsv$", "_image_overrides.json", map_path, flags=re.I)
        if os.path.isfile(candidate):
            override_path = candidate
    image_dir = str(
        project_config.get("image_review_image_dir")
        or project_config.get("image_base_dir")
        or ""
    )
    if not image_dir:
        configured = str(project_config.get("image_dir") or "")
        local_imgs = os.path.join(os.path.dirname(map_path) if map_path else source_dir, "imgs")
        image_dir = configured if configured and os.path.isdir(configured) else local_imgs
    return map_path, override_path, image_dir


class ImageAuditSource:
    """Load the TSV/override pair used by serve_image_audit.py."""

    def __init__(self, source_path, project_config=None, side="left"):
        self.source_path = str(source_path or "")
        self.project_config = project_config or {}
        self.side = str(side or "left")
        self.text = ""
        self.map_path, self.override_path, self.image_dir = discover_image_audit_files(
            self.source_path, self.project_config
        )

    def scan(self):
        if not self.map_path or not os.path.isfile(self.map_path):
            markup_mode = str(
                self.project_config.get(f"markup_mode_{self.side}") or "plain"
            ).lower()
            if (
                markup_mode in {"markdown", "html"}
                or self.source_path.lower().endswith((".md", ".markdown"))
            ):
                markdown = MarkdownImageSource(
                    self.source_path,
                    self.project_config.get("image_base_dir", ""),
                )
                items = markdown.scan()
                self.text = markdown.text
                return items
            return []
        overrides = {}
        if self.override_path and os.path.isfile(self.override_path):
            try:
                payload = json.loads(Path(self.override_path).read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    overrides = payload
            except (OSError, ValueError):
                pass
        with open(self.map_path, "r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream, delimiter="\t"))
        page_offset = int(self.project_config.get("page_offset", 0) or 0)
        hidden = {"appendix_ignored", "missing_file_ignored"}
        page_sequences = {}
        items = []
        for row in rows:
            if str(row.get("status") or "") in hidden:
                continue
            filename = os.path.basename(str(row.get("image") or ""))
            if not filename:
                continue
            override = overrides.get(filename)
            if isinstance(override, dict):
                row = {**row, **override}
            logical_page = _int_value(row.get("page"))
            pdf_page = _int_value(row.get("pdf_page"))
            if logical_page <= 0 and pdf_page > 0:
                logical_page = max(1, pdf_page - page_offset)
            page_sequences[logical_page] = page_sequences.get(logical_page, 0) + 1
            item_id = _stable_id(os.path.abspath(self.map_path), filename)
            segments = []
            bbox_match = _AUDIT_BBOX_RE.search(filename)
            if bbox_match and logical_page > 0:
                segments.append(CropSegment(
                    f"{item_id}:0", logical_page,
                    tuple(float(value) for value in bbox_match.groups()),
                    source_page=pdf_page or None, order=0, label="image",
                ))
            normalized_box = row.get("box")
            if isinstance(normalized_box, str):
                try:
                    normalized_box = json.loads(normalized_box)
                except ValueError:
                    normalized_box = None
            if isinstance(normalized_box, list) and len(normalized_box) == 4:
                # A filename bbox is the original crop; the user override wins.
                segments = []
            action = str(row.get("action") or "")
            if not action:
                action = "ignore" if row.get("status") == "user_ignored" else "attach"
            caption = str(row.get("caption") or row.get("layout_caption") or "")
            headword = str(row.get("headword") or "")
            item = ReviewItem(
                item_id=item_id, mode=ReviewMode.MARKDOWN_IMAGES,
                label=headword or filename, page=logical_page, segments=segments,
                original_name=filename, source_path=self.source_path,
                sequence=page_sequences[logical_page],
                status="ignored" if action == "ignore" else str(row.get("status") or "unreviewed"),
                metadata={
                    "entry_id": str(row.get("entry_id") or ""),
                    "headword": headword,
                    "caption": caption,
                    "layout_caption": str(row.get("layout_caption") or ""),
                    "action": action,
                    "image_order": _int_value(row.get("image_order") or row.get("order")),
                    "confidence": str(row.get("confidence") or ""),
                    "reason": str(row.get("reason") or ""),
                    "local_path": os.path.join(self.image_dir, filename),
                    "legacy_key": filename,
                    "legacy_box": normalized_box if isinstance(normalized_box, list) else [],
                    "legacy_normalized_box": normalized_box if isinstance(normalized_box, list) else None,
                    "map_path": self.map_path,
                    "override_path": self.override_path,
                    "pdf_page": pdf_page,
                },
            )
            items.append(item)
        return items


def _int_value(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class DictionarySliceSource:
    def __init__(self, pages, regex, group=0, source_path="", side="left", project_config=None, adapter=None):
        self.pages = pages or {}
        self.regex = regex or ""
        self.group = int(group or 0)
        self.source_path = str(source_path or "")
        self.side = side
        self.project_config = project_config or {}
        self.adapter = adapter
        self._page_match_cache = {}
        self._compiled_regex = None
        self._structured_source = None
        self._structured_key_index = None

    def scan_page(self, page, anchor_headwords=()):
        """Return only entries which occur on *page*.

        Paged text is parsed from a narrow cross-page window. Structured sources
        fetch bodies only for records matched to this page's anchor headwords.
        """
        from tools.comparison_sources import detect_source_kind, KIND_PAGED_TEXT

        page = int(page or 0)
        if page <= 0:
            return []
        structured = (
            self.adapter.structured
            if self.adapter is not None
            else detect_source_kind(self.source_path) != KIND_PAGED_TEXT
        )
        if not structured:
            entries = self._paged_entries_for_page(page)
        else:
            entries = self._structured_entries_for_page(page, anchor_headwords)
        return self._review_items(entries)

    def _page_has_headword(self, page):
        if page in self._page_match_cache:
            return self._page_match_cache[page]
        if self._compiled_regex is None:
            try:
                self._compiled_regex = re.compile(self.regex)
            except re.error:
                self._compiled_regex = False
        text = str(self.pages.get(page, ""))
        matched = bool(self._compiled_regex and any(
            self._compiled_regex.search(line) for line in text.splitlines()
        ))
        self._page_match_cache[page] = matched
        return matched

    def _paged_entries_for_page(self, page):
        if page not in self.pages or not self.regex:
            return []
        ordered = sorted(int(value) for value in self.pages)
        try:
            current = ordered.index(page)
        except ValueError:
            return []

        start = current
        for index in range(current - 1, -1, -1):
            if self._page_has_headword(ordered[index]):
                start = index
                break

        end = current
        for index in range(current + 1, len(ordered)):
            end = index
            if self._page_has_headword(ordered[index]):
                break

        window = {value: self.pages[value] for value in ordered[start:end + 1]}
        from tools.export_manager import ExportParser

        entries = ExportParser(window, self.regex, self.group).parse()
        return [entry for entry in entries if page in (entry.get("pages") or ())]

    def _load_structured(self):
        if self._structured_source is not None:
            return self._structured_source
        from tools.headword_rules import HeadwordProfile
        from tools.structured_sources import load_structured_source

        profile = HeadwordProfile.from_dict(
            self.project_config.get(f"headword_profile_{self.side}") or {},
            self.regex, self.group,
        )
        self._structured_source = (
            self.adapter.ensure_loaded()
            if self.adapter is not None
            else load_structured_source(self.source_path, profile)
        )
        return self._structured_source

    def _structured_entries_for_page(self, page, anchor_headwords):
        anchors = tuple(dict.fromkeys(
            str(value).strip() for value in anchor_headwords if str(value).strip()
        ))
        if not anchors:
            return []

        source = self._load_structured()
        if self._structured_key_index is None:
            self._structured_key_index = source.headword_key_index()

        from tools.headword_matching import normalize_headword
        from tools.headword_rules import compile_profile

        _extractor, _ignores, filters = compile_profile(source.profile)
        records = []
        used = set()
        for anchor in anchors:
            key, _hits = normalize_headword(anchor, filters)
            candidates = self._structured_key_index.get(key, ())
            if len(candidates) != 1:
                continue
            match = candidates[0]
            if match.order in used:
                continue
            used.add(match.order)
            entry = source.entries[match.order]
            body = source.get_body(match.order)
            match_text = body
            if source.kind == "mdx":
                from tools.markup_support import build_markup_projection

                projection = build_markup_projection(body, "html")
                if not projection.errors:
                    match_text = projection.visible_text
            records.append({
                "headword": entry.headword,
                "text": body,
                "match_text": match_text,
                "pages": [page],
                "page_index": len(records) + 1,
                "aliases": list(entry.aliases),
                "stable_id": entry.stable_id,
            })
        return records

    def scan(self):
        from tools.comparison_sources import detect_source_kind, KIND_PAGED_TEXT

        kind = detect_source_kind(self.source_path)
        structured_adapter = self.adapter is not None and self.adapter.structured
        if kind == KIND_PAGED_TEXT and not structured_adapter:
            if not self.pages or not self.regex:
                return []
            from tools.export_manager import ExportParser
            entries = ExportParser(self.pages, self.regex, self.group).parse()
        else:
            from tools.headword_rules import HeadwordProfile
            from tools.structured_sources import load_structured_source

            profile = HeadwordProfile.from_dict(
                self.project_config.get(f"headword_profile_{self.side}") or {},
                self.regex, self.group,
            )
            structured = (
                self.adapter.ensure_loaded()
                if self.adapter is not None
                else load_structured_source(self.source_path, profile)
            )
            entries = [{
                "headword": entry.headword, "text": structured.get_body(index),
                "pages": [], "page_index": index + 1,
                "aliases": list(entry.aliases), "stable_id": entry.stable_id,
            } for index, entry in enumerate(structured.entries)]
        return self._review_items(entries)

    def _review_items(self, entries):
        items = []
        for index, entry in enumerate(entries):
            pages = [int(page) for page in entry.get("pages") or ()]
            page = pages[0] if pages else 0
            page_index = int(entry.get("page_index") or 1)
            source_key = entry.get("stable_id") or entry.get("headword", "")
            item_id = dictionary_item_id(
                self.source_path, self.side, source_key, page, page_index
            )
            items.append(ReviewItem(
                item_id=item_id,
                mode=ReviewMode.DICTIONARY_SLICES,
                label=str(entry.get("headword") or f"词条 {index + 1}"),
                page=page,
                sequence=page_index,
                source_path=self.source_path,
                status="unmapped",
                metadata={
                    "entry_id": item_id,
                    "headword": entry.get("headword", ""),
                    "text": entry.get("text", ""),
                    "match_text": entry.get("match_text", entry.get("text", "")),
                    "pages": pages,
                    "page_index": page_index,
                    "aliases": list(entry.get("aliases") or ()),
                    "side": self.side,
                    "orientation": "vertical" if "vertical_text" in entry.get("labels", ()) else "horizontal",
                },
            ))
        return items
