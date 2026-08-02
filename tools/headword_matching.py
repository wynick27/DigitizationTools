from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import difflib
import hashlib
import re
import unicodedata

from tools.headword_rules import HeadwordFilterRule, HeadwordProfile


KIND_EXACT = "完全一致"
KIND_FILTERED = "过滤等价"
KIND_DIFF = "词头差异"
KIND_FUZZY = "疑似 OCR 错误"
KIND_AMBIGUOUS = "歧义"
KIND_LEFT_ONLY = "左侧孤立"
KIND_RIGHT_ONLY = "右侧孤立"


class HeadwordProfileError(ValueError):
    pass


@dataclass(frozen=True)
class FilterHit:
    rule_name: str
    pattern: str
    matched: tuple[str, ...]


@dataclass
class HeadwordItem:
    raw: str
    key: str
    page: int | None
    start: int | None
    end: int | None
    order: int
    filter_hits: tuple[FilterHit, ...] = ()
    aliases: tuple[str, ...] = ()
    body: str = ""
    source_id: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def span(self):
        if self.start is None or self.end is None:
            return None
        return self.start, self.end


@dataclass
class ExtractionResult:
    items: list[HeadwordItem]
    unmatched_lines: int = 0
    empty_keys: int = 0

    @property
    def collisions(self):
        counts = Counter(item.key for item in self.items if item.key)
        return {key: count for key, count in counts.items() if count > 1}


def compile_profile(profile: HeadwordProfile):
    try:
        extractor = re.compile(profile.extract_pattern, profile.flags)
    except re.error as exc:
        raise HeadwordProfileError(f"词头提取正则错误: {exc}") from exc

    ignores = []
    for pattern in profile.ignore_patterns:
        try:
            ignores.append(re.compile(pattern, profile.flags))
        except re.error as exc:
            raise HeadwordProfileError(f"忽略正则错误 ({pattern}): {exc}") from exc

    filters = []
    for index, rule in enumerate(profile.filters, start=1):
        if not rule.enabled:
            continue
        if not rule.pattern:
            raise HeadwordProfileError(f"第 {index} 条过滤规则为空")
        try:
            filters.append((rule, re.compile(rule.regex_pattern(), rule.flags)))
        except re.error as exc:
            raise HeadwordProfileError(
                f"过滤规则 {rule.name or index} 错误: {exc}"
            ) from exc
    return extractor, ignores, filters


def normalize_headword(raw: str, compiled_filters) -> tuple[str, tuple[FilterHit, ...]]:
    value = unicodedata.normalize("NFC", str(raw or "")).strip()
    hits = []
    for index, (rule, regex) in enumerate(compiled_filters, start=1):
        matched = tuple(match.group(0) for match in regex.finditer(value))
        if not matched:
            continue
        value = regex.sub(rule.replacement, value)
        hits.append(FilterHit(rule.name or f"规则 {index}", rule.pattern, matched))
    return unicodedata.normalize("NFC", value).strip(), tuple(hits)


def extract_page_headwords(text: str, profile: HeadwordProfile, page=None, order_start=0):
    extractor, ignores, filters = compile_profile(profile)
    items = []
    unmatched = 0
    empty_keys = 0
    offset = 0
    order = order_start
    for line in (text or "").splitlines(True):
        line_without_eol = line.rstrip("\r\n")
        match = extractor.search(line_without_eol)
        if match is None:
            if line_without_eol.strip():
                unmatched += 1
            offset += len(line)
            continue
        try:
            raw = match.group(profile.group)
            start = match.start(profile.group)
            end = match.end(profile.group)
        except (IndexError, KeyError) as exc:
            raise HeadwordProfileError(f"捕获组不存在: {profile.group}") from exc
        if any(regex.search(raw) for regex in ignores):
            offset += len(line)
            continue
        key, filter_hits = normalize_headword(raw, filters)
        if not key:
            empty_keys += 1
        items.append(HeadwordItem(
            raw=raw,
            key=key,
            page=page,
            start=offset + start,
            end=offset + end,
            order=order,
            filter_hits=filter_hits,
            metadata={
                "entry_start": offset + match.start(0),
                "entry_end": offset + match.end(0),
            },
        ))
        order += 1
        offset += len(line)
    return ExtractionResult(items, unmatched, empty_keys)


def extract_pages_headwords(pages: dict[int, str], profile: HeadwordProfile):
    items = []
    unmatched = 0
    empty_keys = 0
    for page in sorted(pages):
        result = extract_page_headwords(pages[page], profile, page, len(items))
        items.extend(result.items)
        unmatched += result.unmatched_lines
        empty_keys += result.empty_keys
    return ExtractionResult(items, unmatched, empty_keys)


def _stable_duplicate_context(items, index, counts):
    key = items[index].key
    if counts[key] <= 1:
        return None
    previous = items[index - 1].key if index else None
    following = items[index + 1].key if index + 1 < len(items) else None
    if previous == key:
        previous = None
    if following == key:
        following = None
    return previous, following


def _make_row(kind, left=None, right=None, score=1.0):
    page = left.page if left is not None else (right.page if right is not None else None)
    return {
        "page": page,
        "left_page": left.page if left else None,
        "right_page": right.page if right else None,
        "kind": kind,
        "left": left.raw if left else "",
        "right": right.raw if right else "",
        "left_key": left.key if left else "",
        "right_key": right.key if right else "",
        "left_span": left.span if left else None,
        "right_span": right.span if right else None,
        "left_hits": left.filter_hits if left else (),
        "right_hits": right.filter_hits if right else (),
        "left_item": left,
        "right_item": right,
        "score": float(score),
        "sync_allowed": bool(left and right and kind not in (KIND_AMBIGUOUS, KIND_FUZZY, KIND_DIFF)),
    }


def _item_match_keys(item):
    return {value for value in (item.key, *item.aliases) if value}


def _sequence_tokens(left_items, right_items):
    left_index = {}
    right_index = {}
    for index, item in enumerate(left_items):
        for key in _item_match_keys(item):
            left_index.setdefault(key, set()).add(index)
    for index, item in enumerate(right_items):
        for key in _item_match_keys(item):
            right_index.setdefault(key, set()).add(index)

    left_candidates = {index: set() for index in range(len(left_items))}
    right_candidates = {index: set() for index in range(len(right_items))}
    for key in left_index.keys() & right_index.keys():
        for left in left_index[key]:
            left_candidates[left].update(right_index[key])
        for right in right_index[key]:
            right_candidates[right].update(left_index[key])

    pairs = {}
    for left, candidates in left_candidates.items():
        if len(candidates) != 1:
            continue
        right = next(iter(candidates))
        if right_candidates.get(right) == {left}:
            pairs[left] = right
    reverse_pairs = {right: left for left, right in pairs.items()}
    left_tokens = [
        f"@{index}:{pairs[index]}" if index in pairs else item.key
        for index, item in enumerate(left_items)
    ]
    right_tokens = [
        f"@{reverse_pairs[index]}:{index}" if index in reverse_pairs else item.key
        for index, item in enumerate(right_items)
    ]
    return left_tokens, right_tokens


def compare_headword_items(left_items, right_items, fuzzy_threshold=0.72):
    left_items = [item for item in left_items if item.key]
    right_items = [item for item in right_items if item.key]
    left_counts = Counter(item.key for item in left_items)
    right_counts = Counter(item.key for item in right_items if item.key)
    left_tokens, right_tokens = _sequence_tokens(left_items, right_items)
    # Repeated aliases in large dictionaries otherwise make SequenceMatcher quadratic.
    use_autojunk = max(len(left_tokens), len(right_tokens)) >= 5000
    matcher = difflib.SequenceMatcher(
        None, left_tokens, right_tokens, autojunk=use_autojunk
    )
    rows = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for left_index, right_index in zip(range(i1, i2), range(j1, j2)):
                left = left_items[left_index]
                right = right_items[right_index]
                duplicate = left_counts[left.key] > 1 or right_counts[right.key] > 1
                if duplicate:
                    left_context = _stable_duplicate_context(left_items, left_index, left_counts)
                    right_context = _stable_duplicate_context(right_items, right_index, right_counts)
                    if not left_context or left_context != right_context or left_context == (None, None):
                        rows.append(_make_row(KIND_AMBIGUOUS, left, right))
                        continue
                kind = KIND_EXACT if left.raw == right.raw else KIND_FILTERED
                rows.append(_make_row(kind, left, right))
            continue

        left_range = list(range(i1, i2))
        right_range = list(range(j1, j2))
        paired_count = min(len(left_range), len(right_range)) if tag == "replace" else 0
        for offset in range(paired_count):
            left = left_items[left_range[offset]]
            right = right_items[right_range[offset]]
            score = difflib.SequenceMatcher(None, left.key, right.key, autojunk=False).ratio()
            kind = KIND_FUZZY if score >= fuzzy_threshold else KIND_DIFF
            rows.append(_make_row(kind, left, right, score))
        for left_index in left_range[paired_count:]:
            rows.append(_make_row(KIND_LEFT_ONLY, left_items[left_index], None, 0.0))
        for right_index in right_range[paired_count:]:
            rows.append(_make_row(KIND_RIGHT_ONLY, None, right_items[right_index], 0.0))

    for index, row in enumerate(rows, start=1):
        row["index"] = index
    return rows

def compare_pages(pages_left, pages_right, profile_left, profile_right):
    left = extract_pages_headwords(pages_left, profile_left)
    right = extract_pages_headwords(pages_right, profile_right)
    return compare_headword_items(left.items, right.items), left, right


def entry_body_segments(items, item, pages):
    """Return page-local body ranges without including the next headword."""
    if item.page is None or item.end is None:
        return []
    try:
        index = items.index(item)
    except ValueError:
        index = item.order
    following = items[index + 1] if index + 1 < len(items) else None
    last_page = (
        following.page
        if following and following.page is not None
        else max(pages or {item.page: ""})
    )
    result = []
    for page in sorted(page for page in pages if item.page <= page <= last_page):
        start = item.metadata.get("entry_end", item.end) if page == item.page else 0
        end = (
            following.metadata.get("entry_start", following.start)
            if following and page == following.page
            else len(pages.get(page, ""))
        )
        if end is None:
            end = len(pages.get(page, ""))
        if end > start:
            result.append((page, start, end))
        if following and page == following.page:
            break
    return result


def read_entry_body(items, item, pages):
    segments = entry_body_segments(items, item, pages)
    return "\n".join(pages[page][start:end] for page, start, end in segments)


def _split_for_page_count(text, old_parts):
    if len(old_parts) <= 1:
        return [text]
    total_old = sum(len(part) for part in old_parts) or len(old_parts)
    desired = []
    running = 0
    for part in old_parts[:-1]:
        running += len(part)
        desired.append(round(len(text) * running / total_old))
    boundaries = []
    previous = 0
    for target in desired:
        candidates = [
            position
            for position in (
                text.rfind("\n", previous, target + 1),
                text.find("\n", target),
            )
            if position >= previous
        ]
        boundary = min(candidates, key=lambda value: abs(value - target)) + 1 if candidates else target
        boundary = max(previous, min(boundary, len(text)))
        boundaries.append(boundary)
        previous = boundary
    starts = [0] + boundaries
    ends = boundaries + [len(text)]
    return [text[start:end] for start, end in zip(starts, ends)]


def replace_entry_body(items, item, pages, new_body):
    segments = entry_body_segments(items, item, pages)
    if not segments:
        raise ValueError("该词条没有可写入的分页文本范围")
    old_parts = [pages[page][start:end] for page, start, end in segments]
    new_parts = _split_for_page_count(new_body, old_parts)
    changed_pages = set()
    for (page, start, end), replacement in zip(segments, new_parts):
        original = pages[page]
        if original[start:end] != replacement:
            pages[page] = original[:start] + replacement + original[end:]
            changed_pages.add(page)
    return changed_pages

class HeadwordExtractionCache:
    def __init__(self, max_pages=12000):
        self.max_pages = max(1, int(max_pages))
        self._page_cache = {}

    @staticmethod
    def _text_digest(text):
        return hashlib.blake2b((text or "").encode("utf-8"), digest_size=16).digest()

    def clear(self):
        self._page_cache.clear()

    def extract_pages(self, source_id, pages, profile):
        profile_key = profile.fingerprint()
        combined = ExtractionResult([])
        for page in sorted(pages):
            text = pages[page]
            key = (str(source_id), page, profile_key, self._text_digest(text))
            result = self._page_cache.get(key)
            if result is None:
                result = extract_page_headwords(text, profile, page, 0)
                if len(self._page_cache) >= self.max_pages:
                    self._page_cache.pop(next(iter(self._page_cache)))
                self._page_cache[key] = result
            for item in result.items:
                clone = HeadwordItem(**{**item.__dict__, "order": len(combined.items)})
                combined.items.append(clone)
            combined.unmatched_lines += result.unmatched_lines
            combined.empty_keys += result.empty_keys
        return combined
