"""Shared, non-Qt text difference engine."""

from __future__ import annotations

from dataclasses import dataclass
import difflib

from tools.markup_support import build_markup_projection, map_projection_opcodes


@dataclass(frozen=True)
class DiffResult:
    opcodes: tuple[tuple, ...]
    visible_opcodes: tuple[tuple, ...]
    errors: tuple[str, ...] = ()
    ratio: float = 1.0


class DiffEngine:
    def compare(self, left, right, ignore_markup=False, mode_left="plain", mode_right="plain"):
        errors = []
        if ignore_markup:
            projection_left = build_markup_projection(left, mode_left)
            projection_right = build_markup_projection(right, mode_right)
            matcher = difflib.SequenceMatcher(
                None,
                projection_left.visible_text,
                projection_right.visible_text,
                autojunk=False,
            )
            visible_opcodes = tuple(matcher.get_opcodes())
            opcodes = tuple(map_projection_opcodes(
                visible_opcodes, projection_left, projection_right
            ))
            errors.extend(f"\u5de6\u4fa7: {error.display()}" for error in projection_left.errors)
            errors.extend(f"\u53f3\u4fa7: {error.display()}" for error in projection_right.errors)
        else:
            matcher = difflib.SequenceMatcher(None, left, right, autojunk=False)
            opcodes = tuple(matcher.get_opcodes())
            visible_opcodes = opcodes
        return DiffResult(opcodes, visible_opcodes, tuple(errors), matcher.ratio())

    def compare_for_ocr(self, left, ocr_text):
        if not ocr_text:
            return ()
        return tuple(difflib.SequenceMatcher(None, left, ocr_text, autojunk=False).get_opcodes())


DEFAULT_DIFF_ENGINE = DiffEngine()