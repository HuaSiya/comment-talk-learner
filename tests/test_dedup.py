from __future__ import annotations

import unittest

from _helpers import load_package

load_package()

from local_bilibili_comment_learner.dedup import (  # noqa: E402
    is_bad_style,
    is_duplicate,
    normalize_for_match,
    normalize_style,
    similarity,
)


class NormalizeTests(unittest.TestCase):
    def test_usage_prefix_and_punctuation(self):
        self.assertEqual(normalize_style("使用「就这？」"), "「就这？」")
        self.assertEqual(normalize_for_match("使用「就这？」"), "使用就这")

    def test_similarity(self):
        self.assertGreater(similarity(normalize_for_match("用「就这」反问"), normalize_for_match("用「就这」反问")), 0)


class DuplicateTests(unittest.TestCase):
    def test_same_style_is_duplicate(self):
        existing = [{"situation": "表示无语时", "style": "用「就这？」反问", "content_list": "[]"}]
        duplicate, reason = is_duplicate("看到离谱操作时", "使用「就这？」反问", existing)
        self.assertTrue(duplicate)
        self.assertIn("风格", reason)

    def test_same_situation_and_similar_style_is_duplicate(self):
        existing = [
            {
                "situation": "看到离谱操作时",
                "style": "用「不是，哥们」开头接一句吐槽",
                "content_list": "[]",
            }
        ]
        duplicate, _ = is_duplicate(
            "看到离谱操作时",
            "用「不是，哥们」开头，后面接一句吐槽",
            existing,
        )
        self.assertTrue(duplicate)

    def test_different_style_and_situation_not_duplicate(self):
        existing = [{"situation": "被夸时", "style": "用自嘲收尾", "content_list": "[]"}]
        duplicate, _ = is_duplicate("看到离谱操作时", "用「不是，哥们」开头", existing)
        self.assertFalse(duplicate)

    def test_content_list_situation_counts(self):
        existing = [
            {
                "situation": "不使用",
                "style": "用自嘲收尾",
                "content_list": '["被夸时"]',
            }
        ]
        duplicate, _ = is_duplicate("被夸时", "用自嘲收尾", existing)
        self.assertTrue(duplicate)


class BadStyleTests(unittest.TestCase):
    def test_generic_styles_rejected(self):
        self.assertTrue(is_bad_style("哈哈哈哈")[0])
        self.assertTrue(is_bad_style("对对对")[0])
        self.assertTrue(is_bad_style("")[0])

    def test_normal_style_kept(self):
        self.assertFalse(is_bad_style("用「不是，哥们」开头接一句吐槽")[0])

    def test_formulaic_style_rejected(self):
        self.assertTrue(is_bad_style("用「火钳刘明」抢前排")[0])
        self.assertTrue(is_bad_style("用「下次一定」敷衍三连")[0])

    def test_too_long_rejected(self):
        self.assertTrue(is_bad_style("用" * 100, max_chars=60)[0])


if __name__ == "__main__":
    unittest.main()
