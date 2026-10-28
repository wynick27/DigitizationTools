"""Shared, non-Qt text difference engine."""

from __future__ import annotations

from dataclasses import dataclass
import difflib
from functools import lru_cache
import re

from tools.markup_support import (
    build_markup_projection,
    collapse_projection_whitespace,
    map_projection_opcodes,
)


@dataclass(frozen=True)
class DiffResult:
    opcodes: tuple[tuple, ...]
    visible_opcodes: tuple[tuple, ...]
    errors: tuple[str, ...] = ()
    ratio: float = 1.0


_LOCAL_REFINE_MAX_LENGTH = 512
_LOCAL_REFINE_MAX_CELLS = 128 * 1024
_DIFF_JUNK_CHARACTERS = frozenset(" \t\r\n")
_DIFF_TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)


def _is_diff_junk(character):
    return character in _DIFF_JUNK_CHARACTERS


def _refine_small_replace_blocks(left, right, opcodes):
    """Improve small changed regions without making whole-page diff quadratic."""
    refined = []
    for tag, i1, i2, j1, j2 in opcodes:
        left_length = i2 - i1
        right_length = j2 - j1
        should_refine = (
            tag == "replace"
            and max(left_length, right_length) <= _LOCAL_REFINE_MAX_LENGTH
            and left_length * right_length <= _LOCAL_REFINE_MAX_CELLS
        )
        if not should_refine:
            refined.append((tag, i1, i2, j1, j2))
            continue

        local_opcodes = difflib.SequenceMatcher(
            _is_diff_junk,
            left[i1:i2],
            right[j1:j2],
            autojunk=False,
        ).get_opcodes()
        refined.extend(
            (local_tag, local_i1 + i1, local_i2 + i1, local_j1 + j1, local_j2 + j1)
            for local_tag, local_i1, local_i2, local_j1, local_j2 in local_opcodes
        )
    return refined


def _offset_opcodes(opcodes, left_offset, right_offset):
    return [
        (
            tag,
            i1 + left_offset,
            i2 + left_offset,
            j1 + right_offset,
            j2 + right_offset,
        )
        for tag, i1, i2, j1, j2 in opcodes
    ]


def _diff_unmatched_region(left, right, i1, i2, j1, j2):
    left_length = i2 - i1
    right_length = j2 - j1
    if not left_length and not right_length:
        return []
    if not left_length:
        return [("insert", i1, i2, j1, j2)]
    if not right_length:
        return [("delete", i1, i2, j1, j2)]
    if left[i1:i2] == right[j1:j2]:
        return [("equal", i1, i2, j1, j2)]

    is_small = (
        max(left_length, right_length) <= _LOCAL_REFINE_MAX_LENGTH
        and left_length * right_length <= _LOCAL_REFINE_MAX_CELLS
    )
    matcher = difflib.SequenceMatcher(
        _is_diff_junk if is_small else None,
        left[i1:i2],
        right[j1:j2],
        autojunk=not is_small,
    )
    opcodes = matcher.get_opcodes()
    if not is_small:
        opcodes = _refine_small_replace_blocks(
            left[i1:i2], right[j1:j2], opcodes
        )
    return _offset_opcodes(opcodes, i1, j1)


def _token_anchor_junk(left_counts, right_counts, maximum_frequency):
    """Keep frequent words from becoming ambiguous page-level anchors."""
    return lambda token: (
        left_counts.get(token, 0) > maximum_frequency
        or right_counts.get(token, 0) > maximum_frequency
    )


def _token_candidate_opcodes(
    left,
    right,
    left_matches,
    right_matches,
    left_tokens,
    right_tokens,
    left_counts,
    right_counts,
    maximum_frequency,
):
    matcher = difflib.SequenceMatcher(
        _token_anchor_junk(left_counts, right_counts, maximum_frequency),
        left_tokens,
        right_tokens,
        autojunk=False,
    )

    opcodes = []
    left_position = 0
    right_position = 0
    for left_index, right_index, size in matcher.get_matching_blocks():
        if not size:
            opcodes.extend(_diff_unmatched_region(
                left,
                right,
                left_position,
                len(left),
                right_position,
                len(right),
            ))
            break
        for offset in range(size):
            left_match = left_matches[left_index + offset]
            right_match = right_matches[right_index + offset]
            opcodes.extend(_diff_unmatched_region(
                left,
                right,
                left_position,
                left_match.start(),
                right_position,
                right_match.start(),
            ))
            opcodes.append((
                "equal",
                left_match.start(),
                left_match.end(),
                right_match.start(),
                right_match.end(),
            ))
            left_position = left_match.end()
            right_position = right_match.end()
    return opcodes


def _matching_opcode_length(opcodes):
    return sum(i2 - i1 for tag, i1, i2, _j1, _j2 in opcodes if tag == "equal")


def _token_anchored_sequence_diff(left, right):
    """Use the best of strict and low-frequency lexical anchor alignments."""
    left_matches = list(_DIFF_TOKEN_PATTERN.finditer(left))
    right_matches = list(_DIFF_TOKEN_PATTERN.finditer(right))
    if not left_matches or not right_matches:
        return _diff_unmatched_region(left, right, 0, len(left), 0, len(right))

    left_tokens = [match.group(0) for match in left_matches]
    right_tokens = [match.group(0) for match in right_matches]
    left_counts = {}
    for token in left_tokens:
        left_counts[token] = left_counts.get(token, 0) + 1
    right_counts = {}
    for token in right_tokens:
        right_counts[token] = right_counts.get(token, 0) + 1

    candidates = [
        _token_candidate_opcodes(
            left,
            right,
            left_matches,
            right_matches,
            left_tokens,
            right_tokens,
            left_counts,
            right_counts,
            maximum_frequency,
        )
        for maximum_frequency in (1, 3)
    ]
    return max(candidates, key=_matching_opcode_length)


def _trimmed_sequence_diff(left, right):
    """Diff only the changed middle while preserving full-text coordinates."""
    if left == right:
        opcode = (("equal", 0, len(left), 0, len(right)),) if left else ()
        return opcode, 1.0

    limit = min(len(left), len(right))
    prefix = 0
    while prefix < limit and left[prefix] == right[prefix]:
        prefix += 1

    suffix = 0
    suffix_limit = limit - prefix
    while (
        suffix < suffix_limit
        and left[len(left) - suffix - 1] == right[len(right) - suffix - 1]
    ):
        suffix += 1

    left_end = len(left) - suffix
    right_end = len(right) - suffix
    middle = _token_anchored_sequence_diff(
        left[prefix:left_end], right[prefix:right_end]
    )

    opcodes = []
    if prefix:
        opcodes.append(("equal", 0, prefix, 0, prefix))
    opcodes.extend(
        (tag, i1 + prefix, i2 + prefix, j1 + prefix, j2 + prefix)
        for tag, i1, i2, j1, j2 in middle
    )
    if suffix:
        opcodes.append(("equal", left_end, len(left), right_end, len(right)))

    merged = []
    for opcode in opcodes:
        if (
            merged
            and merged[-1][0] == opcode[0]
            and merged[-1][2] == opcode[1]
            and merged[-1][4] == opcode[3]
        ):
            previous = merged[-1]
            merged[-1] = (
                previous[0], previous[1], opcode[2], previous[3], opcode[4]
            )
        else:
            merged.append(opcode)

    matched = sum(
        i2 - i1 for tag, i1, i2, _j1, _j2 in merged if tag == "equal"
    )
    total = len(left) + len(right)
    ratio = (2.0 * matched / total) if total else 1.0
    return tuple(merged), ratio


class DiffEngine:
    def compare(self, left, right, ignore_markup=False, mode_left="plain", mode_right="plain"):
        return self._compare_cached(
            left, right, ignore_markup, mode_left, mode_right
        )

    @staticmethod
    @lru_cache(maxsize=64)
    def _compare_cached(left, right, ignore_markup, mode_left, mode_right):
        errors = []
        if ignore_markup:
            projection_left = build_markup_projection(left, mode_left)
            projection_right = build_markup_projection(right, mode_right)
            projection_left = collapse_projection_whitespace(projection_left)
            projection_right = collapse_projection_whitespace(projection_right)
            visible_opcodes, ratio = _trimmed_sequence_diff(
                projection_left.visible_text, projection_right.visible_text
            )
            opcodes = tuple(map_projection_opcodes(
                visible_opcodes, projection_left, projection_right
            ))
            errors.extend(f"\u5de6\u4fa7: {error.display()}" for error in projection_left.errors)
            errors.extend(f"\u53f3\u4fa7: {error.display()}" for error in projection_right.errors)
        else:
            opcodes, ratio = _trimmed_sequence_diff(left, right)
            visible_opcodes = opcodes
        return DiffResult(opcodes, visible_opcodes, tuple(errors), ratio)

    def compare_for_ocr(self, left, ocr_text):
        if not ocr_text:
            return ()
        return self._compare_for_ocr_cached(left, ocr_text)

    @staticmethod
    @lru_cache(maxsize=64)
    def _compare_for_ocr_cached(left, ocr_text):
        return _trimmed_sequence_diff(left, ocr_text)[0]

    def clear_cache(self):
        self._compare_cached.cache_clear()
        self._compare_for_ocr_cached.cache_clear()


DEFAULT_DIFF_ENGINE = DiffEngine()
