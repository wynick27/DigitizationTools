from __future__ import annotations

from dataclasses import dataclass, field
import json
import mmap
import os
import re
import tempfile
import threading

from tools.headword_matching import HeadwordItem, HeadwordProfile, compile_profile, normalize_headword


INDEX_THRESHOLD_BYTES = 100 * 1024 * 1024
HEADWORD_FIELDS = ("headword", "headline", "word", "entry", "title", "txt_head", "\u8bcd\u5934", "\u898b\u51fa\u3057")
BODY_FIELDS = ("body", "content", "text", "definition", "html", "\u6b63\u6587", "\u91ca\u4e49")
ALIAS_FIELDS = ("aliases", "alias", "keys", "variants", "expanded_words", "phrase_aliases", "\u522b\u540d")
LIST_FIELDS = ("entries", "words", "items", "data", "records")


@dataclass
class StructuredEntry:
    headword: str
    body: str = ""
    aliases: list[str] = field(default_factory=list)
    source_index: int = 0
    source_object: object = None
    stable_id: str = ""
    metadata: dict = field(default_factory=dict)


def should_build_index(path, threshold=INDEX_THRESHOLD_BYTES):
    try:
        return os.path.getsize(path) >= int(threshold)
    except OSError:
        return False


def _first_existing_field(sample, candidates):
    if not isinstance(sample, dict):
        return ""
    return next((name for name in candidates if name in sample), "")


def _find_entry_list(value):
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return []
    for key in LIST_FIELDS:
        candidate = value.get(key)
        if isinstance(candidate, list):
            return candidate
    return next(
        (
            candidate
            for candidate in value.values()
            if isinstance(candidate, list)
            and candidate
            and isinstance(candidate[0], dict)
        ),
        [],
    )


def _aliases(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item)]
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[\n,\uff0c\uff1b]+", value) if part.strip()]
    return [str(value)]


def _body_text(value):
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _atomic_replace(path, writer):
    target = os.path.abspath(path)
    directory = os.path.dirname(target) or "."
    fd, temporary = tempfile.mkstemp(prefix=".digitizationtools-", suffix=".tmp", dir=directory)
    os.close(fd)
    try:
        writer(temporary)
        os.replace(temporary, target)
    except Exception:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise
    return target


class _PersistentIndexMixin:
    index_version = 1

    @property
    def index_path(self):
        return self.path + ".dtidx.json"

    def _source_index_signature(self):
        stat = os.stat(self.path)
        return {
            "version": self.index_version,
            "path": self.path,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "profile": self.profile.fingerprint(),
        }

    def _read_index(self, kind):
        try:
            with open(self.index_path, "r", encoding="utf-8") as stream:
                payload = json.load(stream)
            expected = self._source_index_signature()
            if payload.get("kind") != kind:
                return None
            if any(payload.get(key) != value for key, value in expected.items()):
                return None
            return payload
        except (OSError, ValueError, TypeError):
            return None

    def _write_index(self, kind, entries, **extra):
        payload = self._source_index_signature()
        payload.update({"kind": kind, "entries": entries, **extra})
        def writer(temporary):
            with open(temporary, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
        _atomic_replace(self.index_path, writer)

class BaseStructuredSource:
    kind = "structured"
    uses_persistent_index = False

    def __init__(self, path, profile: HeadwordProfile):
        self.path = os.path.abspath(path)
        self.profile = profile
        self.entries = []
        self.dirty = False
        self.pending_bodies = {}
        self._headword_items_cache = None
        self._headword_key_index_cache = None

    def fingerprint(self):
        stat = os.stat(self.path)
        return (self.path, stat.st_size, stat.st_mtime_ns, self.profile.fingerprint())

    def get_body(self, order):
        if order in self.pending_bodies:
            return self.pending_bodies[order]
        return self.entries[order].body

    def update_body(self, order, body):
        body = str(body)
        order = int(order)
        self.pending_bodies[order] = body
        self.entries[order].body = body
        if self._headword_items_cache is not None:
            self._headword_items_cache[order].body = body
        self.dirty = True
    def as_headword_items(self):
        if self._headword_items_cache is not None:
            return self._headword_items_cache
        extractor, ignores, filters = compile_profile(self.profile)

        def matching_key(value):
            raw = str(value or "")
            extracted = raw
            if self.profile.extract_pattern:
                match = extractor.search(raw)
                if match is None:
                    return "", ()
                try:
                    extracted = match.group(self.profile.group)
                except (IndexError, KeyError) as exc:
                    raise ValueError(
                        f"Headword capture group does not exist: {self.profile.group}"
                    ) from exc
            if any(regex.search(extracted) for regex in ignores):
                return "", ()
            return normalize_headword(extracted, filters)

        result = []
        for order, entry in enumerate(self.entries):
            key, hits = matching_key(entry.headword)
            normalized_aliases = tuple(
                normalized
                for alias in entry.aliases
                for normalized in (matching_key(alias)[0],)
                if normalized
            )
            result.append(
                HeadwordItem(
                    raw=entry.headword,
                    key=key,
                    page=None,
                    start=None,
                    end=None,
                    order=order,
                    filter_hits=hits,
                    aliases=normalized_aliases,
                    body=entry.body if not self.uses_persistent_index else "",
                    source_id=self.path,
                    metadata={
                        "source_index": entry.source_index,
                        "stable_id": entry.stable_id or f"{self.path}:{entry.source_index}",
                    },
                )
            )
        self._headword_items_cache = tuple(result)
        return self._headword_items_cache

    def headword_key_index(self):
        with self._headword_cache_lock:
            if self._headword_key_index_cache is None:
                index = {}
                for item in self.as_headword_items():
                    for key in (item.key, *item.aliases):
                        if key:
                            index.setdefault(key, []).append(item)
                self._headword_key_index_cache = {
                    key: tuple(values) for key, values in index.items()
                }
            return self._headword_key_index_cache

class JsonEntrySource(BaseStructuredSource):
    kind = "json"

    def __init__(self, path, profile):
        super().__init__(path, profile)
        self.root = None
        self.entry_list = []
        self.headword_field = profile.json_headword_field
        self.body_field = profile.json_body_field
        self.alias_field = profile.json_alias_field

    def load(self):
        with open(self.path, "r", encoding="utf-8-sig") as stream:
            self.root = json.load(stream)
        self.entry_list = _find_entry_list(self.root)
        sample = next((item for item in self.entry_list if isinstance(item, dict)), {})
        self._resolve_fields(sample)
        self.entries = []
        for index, item in enumerate(self.entry_list):
            if not isinstance(item, dict):
                continue
            entry = self._entry_from_object(index, item, include_body=True)
            if entry is not None:
                self.entries.append(entry)
        return self.entries

    def _resolve_fields(self, sample):
        self.headword_field = self.headword_field or _first_existing_field(sample, HEADWORD_FIELDS)
        self.body_field = self.body_field or _first_existing_field(sample, BODY_FIELDS)
        self.alias_field = self.alias_field or _first_existing_field(sample, ALIAS_FIELDS)
        if not self.headword_field:
            raise ValueError("无法自动识别 JSON 词头字段")

    def _entry_from_object(self, index, item, include_body):
        raw = item.get(self.headword_field, "")
        if raw is None or str(raw) == "":
            return None
        body = _body_text(item.get(self.body_field, "")) if include_body and self.body_field else ""
        aliases = _aliases(item.get(self.alias_field)) if self.alias_field else []
        stable = str(item.get("id") or item.get("entry_id") or index)
        return StructuredEntry(str(raw), body, aliases, index, item, stable)

    def update_body(self, order, body):
        entry = self.entries[order]
        if not self.body_field:
            raise ValueError("JSON 未配置正文字段")
        entry.source_object[self.body_field] = body
        super().update_body(order, body)

    def save(self, path=None):
        target = os.path.abspath(path or self.path)
        def writer(temporary):
            with open(temporary, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(self.root, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
        _atomic_replace(target, writer)
        self.pending_bodies.clear()
        self.dirty = False
        return target


class _JsonByteScanner:
    WHITESPACE = b" \t\r\n"

    def __init__(self, data):
        self.data = data
        self.length = len(data)

    def skip_ws(self, index):
        while index < self.length and self.data[index] in self.WHITESPACE:
            index += 1
        return index

    def string_end(self, index):
        index += 1
        escaped = False
        while index < self.length:
            value = self.data[index]
            if escaped:
                escaped = False
            elif value == 92:
                escaped = True
            elif value == 34:
                return index + 1
            index += 1
        raise ValueError("JSON 字符串未闭合")

    def value_end(self, index):
        index = self.skip_ws(index)
        if index >= self.length:
            raise ValueError("JSON 值缺失")
        first = self.data[index]
        if first == 34:
            return self.string_end(index)
        if first in (123, 91):
            opening, closing = (123, 125) if first == 123 else (91, 93)
            depth = 1
            cursor = index + 1
            while cursor < self.length:
                value = self.data[cursor]
                if value == 34:
                    cursor = self.string_end(cursor)
                    continue
                if value == opening:
                    depth += 1
                elif value == closing:
                    depth -= 1
                    if depth == 0:
                        return cursor + 1
                cursor += 1
            raise ValueError("JSON 容器未闭合")
        cursor = index
        while cursor < self.length and self.data[cursor] not in b",]}":
            cursor += 1
        return cursor

    def entry_array_start(self):
        start = self.skip_ws(3 if self.data[:3] == b"\xef\xbb\xbf" else 0)
        if self.data[start] == 91:
            return start
        if self.data[start] != 123:
            raise ValueError("JSON 顶层必须为数组或对象")
        cursor = start + 1
        fallback = None
        while True:
            cursor = self.skip_ws(cursor)
            if cursor >= self.length or self.data[cursor] == 125:
                break
            if self.data[cursor] != 34:
                raise ValueError("JSON 顶层字段无效")
            key_end = self.string_end(cursor)
            key = json.loads(bytes(self.data[cursor:key_end]).decode("utf-8"))
            cursor = self.skip_ws(key_end)
            if self.data[cursor] != 58:
                raise ValueError("JSON 顶层字段缺少冒号")
            value_start = self.skip_ws(cursor + 1)
            if self.data[value_start] == 91:
                if key in LIST_FIELDS:
                    return value_start
                fallback = fallback if fallback is not None else value_start
            cursor = self.skip_ws(self.value_end(value_start))
            if cursor < self.length and self.data[cursor] == 44:
                cursor += 1
        if fallback is not None:
            return fallback
        raise ValueError("JSON 中没有可用的词条数组")

    def array_values(self, array_start):
        cursor = array_start + 1
        while True:
            cursor = self.skip_ws(cursor)
            if cursor >= self.length or self.data[cursor] == 93:
                return
            end = self.value_end(cursor)
            yield cursor, end
            cursor = self.skip_ws(end)
            if cursor < self.length and self.data[cursor] == 44:
                cursor += 1


class IndexedJsonEntrySource(_PersistentIndexMixin, JsonEntrySource):
    uses_persistent_index = True

    def __init__(self, path, profile):
        super().__init__(path, profile)
        self._ranges = []

    def load(self):
        self.entries = []
        self._ranges = []
        cached = self._read_index("json")
        if cached is not None:
            self.headword_field = cached.get("headword_field", self.headword_field)
            self.body_field = cached.get("body_field", self.body_field)
            self.alias_field = cached.get("alias_field", self.alias_field)
            for item in cached.get("entries", ()):
                entry = StructuredEntry(
                    item["headword"], "", list(item.get("aliases", ())),
                    int(item["source_index"]), None, str(item.get("stable_id", item["source_index"])),
                    {"byte_start": int(item["byte_start"]), "byte_end": int(item["byte_end"])},
                )
                self.entries.append(entry)
                self._ranges.append((entry.metadata["byte_start"], entry.metadata["byte_end"]))
            return self.entries

        with open(self.path, "rb") as stream:
            with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
                scanner = _JsonByteScanner(data)
                array_start = scanner.entry_array_start()
                sample = None
                for index, (start, end) in enumerate(scanner.array_values(array_start)):
                    value = json.loads(bytes(data[start:end]).decode("utf-8"))
                    if not isinstance(value, dict):
                        continue
                    if sample is None:
                        sample = value
                        self._resolve_fields(sample)
                    entry = self._entry_from_object(index, value, include_body=False)
                    if entry is None:
                        continue
                    entry.source_object = None
                    entry.metadata.update({"byte_start": start, "byte_end": end})
                    self.entries.append(entry)
                    self._ranges.append((start, end))
        self._write_index(
            "json",
            [{
                "headword": entry.headword, "aliases": entry.aliases,
                "source_index": entry.source_index, "stable_id": entry.stable_id,
                "byte_start": entry.metadata["byte_start"], "byte_end": entry.metadata["byte_end"],
            } for entry in self.entries],
            headword_field=self.headword_field,
            body_field=self.body_field,
            alias_field=self.alias_field,
        )
        return self.entries
    def _read_object(self, order):
        entry = self.entries[order]
        start, end = entry.metadata["byte_start"], entry.metadata["byte_end"]
        with open(self.path, "rb") as stream:
            stream.seek(start)
            return json.loads(stream.read(end - start).decode("utf-8"))

    def get_body(self, order):
        if order in self.pending_bodies:
            return self.pending_bodies[order]
        value = self._read_object(order)
        return _body_text(value.get(self.body_field, "")) if self.body_field else ""

    def update_body(self, order, body):
        if not self.body_field:
            raise ValueError("JSON 未配置正文字段")
        BaseStructuredSource.update_body(self, order, body)

    def save(self, path=None):
        target = os.path.abspath(path or self.path)
        replacements = {}
        for order, body in self.pending_bodies.items():
            obj = self._read_object(order)
            obj[self.body_field] = body
            entry = self.entries[order]
            replacements[entry.metadata["byte_start"]] = (
                entry.metadata["byte_end"],
                json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            )
        if not replacements:
            return target
        def writer(temporary):
            with open(self.path, "rb") as source, open(temporary, "wb") as output:
                cursor = 0
                for start in sorted(replacements):
                    end, replacement = replacements[start]
                    source.seek(cursor)
                    output.write(source.read(start - cursor))
                    output.write(replacement)
                    cursor = end
                source.seek(cursor)
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
        _atomic_replace(target, writer)
        self.pending_bodies.clear()
        self.dirty = False
        if target == self.path:
            self.load()
        return target


class MdxEntrySource(BaseStructuredSource):
    kind = "mdx"

    @staticmethod
    def _decode(value):
        return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)

    def load(self):
        if self.path.lower().endswith(".mdx"):
            from mdict_utils.reader import MDX
            source_items = MDX(self.path).items()
        else:
            source_items = self._read_source_text(self.path)
        self.entries = self._fold_aliases(source_items)
        return self.entries

    def _fold_aliases(self, source_items):
        canonical = []
        aliases_by_target = {}
        for key_value, body_value in source_items:
            key = self._decode(key_value).strip()
            body = self._decode(body_value)
            alias_match = re.fullmatch(r"\s*@@@LINK=(.*?)\s*", body, re.S)
            if alias_match:
                aliases_by_target.setdefault(alias_match.group(1).strip(), []).append(key)
                continue
            canonical.append(StructuredEntry(key, body, [], len(canonical), None, str(len(canonical))))
        for entry in canonical:
            entry.aliases.extend(aliases_by_target.pop(entry.headword, []))
        for target, aliases in aliases_by_target.items():
            canonical.append(StructuredEntry(target, "", aliases, len(canonical), None, str(len(canonical))))
        return canonical

    @staticmethod
    def _read_source_text(path):
        with open(path, "r", encoding="utf-8") as stream:
            key = None
            body = []
            for line in stream:
                if key is None:
                    if line.strip():
                        key = line.rstrip("\r\n")
                    continue
                if line.rstrip("\r\n") == "</>":
                    yield key, "".join(body)
                    key, body = None, []
                else:
                    body.append(line)
            if key is not None:
                raise ValueError(f"MDX 源文件末尾缺少 </>: {key}")

    def save_source(self, path):
        target = os.path.abspath(path)
        def writer(temporary):
            with open(temporary, "w", encoding="utf-8", newline="\n") as stream:
                for order, entry in enumerate(self.entries):
                    stream.write(entry.headword + "\n")
                    body = self.get_body(order)
                    stream.write(body)
                    if body and not body.endswith("\n"):
                        stream.write("\n")
                    stream.write("</>\n")
                    for alias in entry.aliases:
                        stream.write(alias + "\n")
                        stream.write(f"@@@LINK={entry.headword}\n</>\n")
        _atomic_replace(target, writer)
        self.pending_bodies.clear()
        self.dirty = False
        return target

    def rebuild(self, source_path, output_path, title=""):
        from mdict_utils.writer import pack, pack_mdx_txt
        dictionary = pack_mdx_txt(source_path)
        pack(output_path, dictionary, title=title or os.path.splitext(os.path.basename(output_path))[0])
        return output_path


class IndexedMdxTextSource(_PersistentIndexMixin, MdxEntrySource):
    uses_persistent_index = True

    def _source_index_signature(self):
        stat = os.stat(self.path)
        return {
            "version": self.index_version,
            "path": self.path,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }

    def load(self):
        if self.path.lower().endswith(".mdx"):
            return super().load()
        cached = self._read_index("mdx_text")
        if cached is not None:
            self.entries = []
            for item in cached.get("entries", ()):
                self.entries.append(StructuredEntry(
                    item["headword"], "", list(item.get("aliases", ())),
                    int(item["source_index"]), None, str(item.get("stable_id", item["source_index"])),
                    {key: int(item[key]) for key in ("record_start", "body_start", "body_end", "record_end") if item.get(key) is not None},
                ))
            return self.entries

        records = []
        with open(self.path, "rb") as stream:
            while True:
                record_start = stream.tell()
                key_line = stream.readline()
                if not key_line:
                    break
                if not key_line.strip():
                    continue
                key = key_line.rstrip(b"\r\n").decode("utf-8", errors="replace")
                body_start = stream.tell()
                while True:
                    line_start = stream.tell()
                    line = stream.readline()
                    if not line:
                        raise ValueError(f"MDX source is missing final </>: {key}")
                    if line.rstrip(b"\r\n") == b"</>":
                        body_end, record_end = line_start, stream.tell()
                        break
                body_size = body_end - body_start
                body = ""
                if body_size <= 4096:
                    position = stream.tell(); stream.seek(body_start)
                    body = stream.read(body_size).decode("utf-8", errors="replace")
                    stream.seek(position)
                records.append((key, body, record_start, body_start, body_end, record_end))
        canonical, aliases_by_target = [], {}
        for key, small_body, record_start, body_start, body_end, record_end in records:
            alias = re.fullmatch(r"\s*@@@LINK=(.*?)\s*", small_body, re.S) if small_body else None
            if alias:
                aliases_by_target.setdefault(alias.group(1).strip(), []).append(key)
                continue
            entry = StructuredEntry(key, "", [], len(canonical), None, str(len(canonical)))
            entry.metadata.update({"record_start": record_start, "body_start": body_start, "body_end": body_end, "record_end": record_end})
            canonical.append(entry)
        for entry in canonical:
            entry.aliases.extend(aliases_by_target.pop(entry.headword, []))
        for target, aliases in aliases_by_target.items():
            canonical.append(StructuredEntry(target, "", aliases, len(canonical), None, str(len(canonical))))
        self.entries = canonical
        self._write_index(
            "mdx_text",
            [{
                "headword": entry.headword, "aliases": entry.aliases,
                "source_index": entry.source_index, "stable_id": entry.stable_id,
                **entry.metadata,
            } for entry in self.entries],
        )
        return self.entries
    def save_source(self, path):
        target = os.path.abspath(path)
        if target != self.path or not self.pending_bodies:
            return super().save_source(target)
        replacements = {}
        for order, body in self.pending_bodies.items():
            entry = self.entries[order]
            start, end = entry.metadata.get("body_start"), entry.metadata.get("body_end")
            if start is None or end is None:
                raise ValueError(f"entry has no writable source range: {entry.headword}")
            encoded = str(body).encode("utf-8")
            replacements[int(start)] = (int(end), encoded)
        def writer(temporary):
            with open(self.path, "rb") as source, open(temporary, "wb") as output:
                cursor = 0
                for start in sorted(replacements):
                    end, replacement = replacements[start]
                    source.seek(cursor)
                    output.write(source.read(start - cursor))
                    output.write(replacement)
                    cursor = end
                source.seek(cursor)
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
        _atomic_replace(target, writer)
        self.pending_bodies.clear()
        self.dirty = False
        self.load()
        return target
    def get_body(self, order):
        if order in self.pending_bodies:
            return self.pending_bodies[order]
        entry = self.entries[order]
        start = entry.metadata.get("body_start")
        end = entry.metadata.get("body_end")
        if start is None or end is None:
            return ""
        with open(self.path, "rb") as stream:
            stream.seek(start)
            return stream.read(end - start).decode("utf-8", errors="replace")


def load_structured_source(path, profile, threshold=INDEX_THRESHOLD_BYTES):
    lower = str(path).lower()
    indexed = should_build_index(path, threshold)
    if lower.endswith(".json"):
        source = IndexedJsonEntrySource(path, profile) if indexed else JsonEntrySource(path, profile)
    elif lower.endswith(".mdx"):
        source = MdxEntrySource(path, profile)
    elif lower.endswith(".mdx.txt") or lower.endswith(".txt"):
        source = IndexedMdxTextSource(path, profile) if indexed else MdxEntrySource(path, profile)
    else:
        raise ValueError(f"不支持的数据源格式: {os.path.splitext(path)[1].lower()}")
    source.load()
    return source