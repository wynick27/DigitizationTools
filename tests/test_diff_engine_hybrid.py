import difflib
import unittest

from tools.diff_engine import DiffEngine


def _equal_length(opcodes):
    return sum(i2 - i1 for tag, i1, i2, _j1, _j2 in opcodes if tag == "equal")


class HybridDiffTests(unittest.TestCase):
    def test_small_repetitive_replace_block_is_refined_with_explicit_junk(self):
        left = "AAAAA" + ("x " * 150) + "BBBB"
        right = "CCCCC" + ("x " * 150) + "DDDD"

        fast_only = difflib.SequenceMatcher(
            None, left, right, autojunk=True
        ).get_opcodes()
        result = DiffEngine().compare(left, right)

        self.assertGreater(_equal_length(result.opcodes), _equal_length(fast_only))
        self.assertTrue(any(
            tag == "equal" and left[i1:i2].count("x") >= 140
            for tag, i1, i2, _j1, _j2 in result.opcodes
        ))

    def test_page_scale_markup_and_line_break_changes_keep_global_alignment(self):
        left = "\n".join(
            f"**entry{index}** common definition {index} with repeated words"
            for index in range(180)
        )
        right = " ".join(
            f"entry{index}\ncommon definition {index} with repeated words"
            for index in range(180)
        )

        result = DiffEngine().compare(left, right)
        largest_replace = max(
            (
                max(i2 - i1, j2 - j1)
                for tag, i1, i2, j1, j2 in result.opcodes
                if tag == "replace"
            ),
            default=0,
        )

        self.assertGreater(result.ratio, 0.9)
        self.assertLess(largest_replace, 100)

    def test_repeated_words_do_not_override_unique_global_anchors(self):
        repeated = "medio de la cosa que " * 20
        left = f"alpha {repeated}unique_center {repeated}omega"
        right = f"page header medio\nalpha {repeated}unique_center {repeated}omega footer"

        result = DiffEngine().compare(left, right)

        self.assertGreater(result.ratio, 0.95)
        self.assertTrue(any(
            tag == "equal" and "unique_center" in left[i1:i2]
            for tag, i1, i2, _j1, _j2 in result.opcodes
        ))

    def test_low_frequency_repeated_tokens_can_anchor_a_large_region(self):
        first = "a" * 200
        second = "b" * 200
        body = f"{first} {second} {first} {second}"
        left = f"LLLLL {body} XXXXX"
        right = f"RRRRR {body} YYYYY"

        result = DiffEngine().compare(left, right)

        self.assertGreater(result.ratio, 0.95)

    def test_large_unrelated_replace_block_keeps_bounded_fast_alignment(self):
        left = "LEFT:" + ("a " * 400) + ":OLD"
        right = "RIGHT:" + ("b " * 400) + ":NEW"

        result = DiffEngine().compare(left, right)

        self.assertTrue(result.opcodes)
        self.assertEqual(result.opcodes[0][1], 0)
        self.assertEqual(result.opcodes[-1][2], len(left))
        self.assertEqual(result.opcodes[-1][4], len(right))
