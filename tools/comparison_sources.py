"""Unified non-Qt comparison data sources and format detection."""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
import tempfile
import threading

from tools.headword_rules import HeadwordProfile
from tools.structured_sources import load_structured_source


KIND_PAGED_TEXT = "paged_text"
KIND_MDX_TEXT = "mdx_text"
KIND_JSON = "json"
KIND_MDX_BINARY = "mdx_binary"
PAGE_PATTERN = re.compile(r"<([0-9]+)>")


@dataclass(frozen=True)
class SourceDescriptor:
    path: str
    kind: str
    structured: bool


def detect_source_kind(path, sample_bytes=2 * 1024 * 1024):
    lower = str(path or "").lower()
    if lower.endswith(".json"):
        return KIND_JSON
    if lower.endswith(".mdx"):
        return KIND_MDX_BINARY
    if lower.endswith(".mdx.txt"):
        return KIND_MDX_TEXT
    if not path or not os.path.exists(path):
        return KIND_PAGED_TEXT
    try:
        with open(path, "rb") as stream:
            sample = stream.read(sample_bytes).decode("utf-8-sig", errors="replace")
    except OSError:
        return KIND_PAGED_TEXT
    page_markers = sum(
        1 for line in sample.splitlines() if PAGE_PATTERN.fullmatch(line.strip())
    )
    record_markers = sum(1 for line in sample.splitlines() if line.strip() == "</>")
    if record_markers and not page_markers:
        return KIND_MDX_TEXT
    return KIND_PAGED_TEXT


def describe_source(path):
    kind = detect_source_kind(path)
    return SourceDescriptor(
        os.path.abspath(path) if path else "",
        kind,
        kind != KIND_PAGED_TEXT,
    )


def _atomic_write_text(path, callback):
    target = os.path.abspath(path)
    directory = os.path.dirname(target) or "."
    descriptor, temporary = tempfile.mkstemp(
        prefix=".digitizationtools-", suffix=".tmp", dir=directory
    )
    os.close(descriptor)
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as stream:
            callback(stream)
        os.replace(temporary, target)
    except Exception:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise
    return target


class PagedTextSource:
    """Editable text source, either page-marked or a single plain document."""

    kind = KIND_PAGED_TEXT
    structured = False

    def __init__(self, path):
        self.path = os.path.abspath(path) if path else ""
        self.pages = {}
        self.dirty_pages = set()
        self.has_page_markers = False

    def load(self):
        pages = {}
        self.has_page_markers = False
        if not self.path or not os.path.exists(self.path):
            self.pages = pages
            return pages
        with open(self.path, "r", encoding="utf-8-sig") as stream:
            text = stream.read()
        matches = list(re.finditer(r"(?m)^\s*<([0-9]+)>\s*$", text))
        if not matches:
            pages[1] = text
        else:
            self.has_page_markers = True
            for index, match in enumerate(matches):
                start = match.end()
                if start < len(text) and text[start:start + 2] == "\r\n":
                    start += 2
                elif start < len(text) and text[start] in "\r\n":
                    start += 1
                end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
                value = text[start:end]
                if value.endswith("\r\n"):
                    value = value[:-2]
                elif value.endswith("\n"):
                    value = value[:-1]
                pages[int(match.group(1))] = value.replace("\r\n", "\n")
        self.pages = pages
        self.dirty_pages.clear()
        return pages

    def page_numbers(self):
        return tuple(sorted(self.pages))

    def get_page(self, page, default=""):
        return self.pages.get(int(page), default)

    def update_page(self, page, text):
        page = int(page)
        text = str(text)
        if self.pages.get(page, "") != text:
            self.pages[page] = text
            self.dirty_pages.add(page)

    def replace_pages(self, pages):
        self.pages = pages if isinstance(pages, dict) else dict(pages or {})
        return self.pages

    def fingerprint(self):
        try:
            stat = os.stat(self.path)
            return self.path, stat.st_size, stat.st_mtime_ns
        except OSError:
            return self.path, 0, 0

    def save(self, path=None):
        target = os.path.abspath(path or self.path)

        def writer(stream):
            if not self.has_page_markers and set(self.pages).issubset({1}):
                stream.write(self.pages.get(1, ""))
                return
            for page in sorted(self.pages):
                stream.write(f"<{page}>\n")
                stream.write(self.pages[page])
                if not self.pages[page].endswith("\n"):
                    stream.write("\n")

        _atomic_write_text(target, writer)
        self.path = target
        self.dirty_pages.clear()
        return target


class ComparisonDataSource:
    """Unified lazy facade for paged documents and structured dictionaries."""

    def __init__(self, path, profile=None, index_threshold=None):
        self.descriptor = describe_source(path)
        self.profile = profile or HeadwordProfile("")
        self.index_threshold = index_threshold
        self.source = None
        self._load_lock = threading.RLock()

    @property
    def path(self):
        return self.descriptor.path

    @property
    def kind(self):
        return self.descriptor.kind

    @property
    def structured(self):
        return self.descriptor.structured

    @property
    def pages(self):
        if self.structured:
            return {}
        source = self.ensure_loaded()
        return source.pages

    @property
    def entries(self):
        source = self.ensure_loaded()
        return source.entries if self.structured else ()

    @property
    def dirty(self):
        if self.source is None:
            return False
        if self.structured:
            return bool(self.source.dirty)
        return bool(self.source.dirty_pages)

    def ensure_loaded(self):
        with self._load_lock:
            if self.source is not None:
                return self.source
            if self.structured:
                kwargs = (
                    {} if self.index_threshold is None
                    else {"threshold": self.index_threshold}
                )
                self.source = load_structured_source(
                    self.path, self.profile, **kwargs
                )
            else:
                self.source = PagedTextSource(self.path)
                self.source.load()
            return self.source
    def load(self):
        return self.ensure_loaded()

    def get_page(self, page, default=""):
        if self.structured:
            return default
        return self.ensure_loaded().get_page(page, default)

    def update_page(self, page, text):
        if self.structured:
            raise TypeError("结构化数据源不能按页直接修改")
        self.ensure_loaded().update_page(page, text)

    def replace_pages(self, pages):
        if self.structured:
            raise TypeError("结构化数据源没有可替换的分页文本")
        return self.ensure_loaded().replace_pages(pages)

    def get_body(self, order):
        if not self.structured:
            raise TypeError("分页文本数据源没有词条正文")
        return self.ensure_loaded().get_body(order)

    def update_body(self, order, body):
        if not self.structured:
            raise TypeError("分页文本数据源没有词条正文")
        self.ensure_loaded().update_body(order, body)

    def save(self, path=None):
        source = self.ensure_loaded()
        target = path or self.path
        if self.kind == KIND_MDX_BINARY:
            raise TypeError("二进制 MDX 是只读源；请保存编辑源后重建 MDX")
        if hasattr(source, "save"):
            return source.save(target)
        if hasattr(source, "save_source"):
            return source.save_source(target)
        raise TypeError(f"数据源不支持保存: {self.kind}")

    def fingerprint(self):
        return (
            self.source.fingerprint()
            if self.source is not None
            else (self.path, self.descriptor.kind)
        )


class ComparisonSourceRegistry:
    """Owns left/right adapters while exposing legacy page dictionaries."""

    def __init__(self):
        self.left = ComparisonDataSource("")
        self.right = {}
        self.active_right = 0

    def configure(self, left_path, right_candidates, profiles=None, active_right=0):
        profiles = profiles or {}
        self.left = ComparisonDataSource(left_path, profiles.get("left"))
        self.right = {
            index: ComparisonDataSource(
                candidate.get("path", ""), profiles.get("right")
            )
            for index, candidate in enumerate(right_candidates or ())
        }
        self.active_right = min(
            max(0, int(active_right or 0)),
            max(0, len(self.right) - 1),
        )
        return self

    def set_right(self, index, path, profile=None):
        index = int(index)
        self.right[index] = ComparisonDataSource(path, profile)
        self.active_right = index
        return self.right[index]

    def right_source(self, index=None):
        index = self.active_right if index is None else int(index)
        return self.right.get(index)

    def page_maps(self):
        return (
            self.left.pages,
            {index: source.pages for index, source in self.right.items()},
        )
