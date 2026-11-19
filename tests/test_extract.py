from __future__ import annotations

import unittest

from _helpers import load_package

load_package()

from local_bilibili_comment_learner.extract import (  # noqa: E402
    build_extract_prompt,
    build_review_prompt,
    is_meaningful_comment,
    normalize_candidate,
    parse_json_payload,
    parse_review_payload,
    prepare_comments,
    sanitize_comment,
)


class SanitizeTests(unittest.TestCase):
    def test_remove_mentions_urls_and_emotes(self):
        raw = "回复 @张三 : 这也太离谱了吧[doge] https://b23.tv/x @李四"
        self.assertEqual(sanitize_comment(raw), "这也太离谱了吧")

    def test_collapse_newlines(self):
        self.assertEqual(sanitize_comment("第一行\n第二行\r第三行"), "第一行 第二行 第三行")

    def test_remove_bv(self):
        self.assertEqual(sanitize_comment("看 BV1xx411c7mD 这个"), "看 这个")


class MeaningfulTests(unittest.TestCase):
    def test_normal_comment_kept(self):
        ok, _ = is_meaningful_comment("这也太离谱了吧")
        self.assertTrue(ok)

    def test_short_and_generic_dropped(self):
        self.assertFalse(is_meaningful_comment("哈", min_chars=4)[0])
        self.assertFalse(is_meaningful_comment("哈哈哈哈", min_chars=4)[0])
        self.assertFalse(is_meaningful_comment("666666")[0])
        self.assertFalse(is_meaningful_comment("笑死我了")[0])

    def test_ad_dropped(self):
        self.assertFalse(is_meaningful_comment("加群领取福利")[0])
        self.assertFalse(is_meaningful_comment("点个关注不迷路")[0])

    def test_fan_comments_dropped(self):
        self.assertFalse(is_meaningful_comment("关注潘子")[0])
        self.assertFalse(is_meaningful_comment("好的潘子！")[0])
        self.assertFalse(is_meaningful_comment("潘子你好")[0])

    def test_short_slang_dropped_as_generic(self):
        self.assertFalse(is_meaningful_comment("awsl")[0])

    def test_long_english_dropped(self):
        self.assertFalse(is_meaningful_comment("hello world this is english")[0])


class PrepareCommentsTests(unittest.TestCase):
    def test_ids_and_cross_video_dedup(self):
        seen: set[str] = set()
        first, next_id = prepare_comments(
            [
                {"message": "这也太离谱了吧", "like": 10},
                {"message": "这也太离谱了吧", "like": 20},
                {"message": "不是，哥们", "like": 5},
            ],
            source="BV1",
            start_id=1,
            seen_keys=seen,
        )
        self.assertEqual([item["id"] for item in first], [1, 2])
        self.assertEqual(first[0]["like"], 10)
        self.assertEqual(next_id, 3)

        second, next_id = prepare_comments(
            [{"message": "这也太离谱了吧"}, {"message": "你是真的秀"}],
            source="BV2",
            start_id=next_id,
            seen_keys=seen,
        )
        self.assertEqual([item["text"] for item in second], ["你是真的秀"])
        self.assertEqual(second[0]["id"], 3)
        self.assertEqual(next_id, 4)


class ParseJsonTests(unittest.TestCase):
    def test_fenced_json(self):
        self.assertEqual(parse_json_payload('```json\n[{"a":1}]\n```'), [{"a": 1}])

    def test_json_with_explanation(self):
        self.assertEqual(parse_json_payload('结果如下：\n[{"a":1}]\n希望有帮助'), [{"a": 1}])

    def test_json_with_trailing_comma(self):
        self.assertEqual(parse_json_payload('[{"a":1,},]'), [{"a": 1}])
        self.assertEqual(parse_json_payload('{"candidates":[{"a":1,}],}'), {"candidates": [{"a": 1}]})

    def test_invalid(self):
        self.assertIsNone(parse_json_payload("没有 JSON"))
        self.assertIsNone(parse_json_payload(""))


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.comments = {
            1: "这也太离谱了吧",
            2: "不是，哥们，这也行",
            3: "你是真的秀",
            4: "不知道的先出院吧",
        }

    def test_valid_candidate(self):
        item = {
            "situation": "看到离谱操作时",
            "style": "用「不是，哥们」开头接一句吐槽",
            "evidence_ids": [2],
            "confidence": 0.8,
            "taboo": "none",
        }
        candidate, reason = normalize_candidate(item, comments_by_id=self.comments)
        self.assertIsNotNone(candidate, reason)
        self.assertEqual(candidate["style"], "用「不是，哥们」开头接一句吐槽")
        self.assertEqual(candidate["examples"], ["不是，哥们，这也行"])

    def test_quote_must_exist_in_evidence(self):
        item = {
            "situation": "看到离谱操作时",
            "style": "用「离谱」开头",
            "evidence_ids": [3],
            "confidence": 0.8,
            "taboo": "none",
        }
        candidate, reason = normalize_candidate(item, comments_by_id=self.comments)
        self.assertIsNone(candidate)
        self.assertIn("不在证据", reason)

    def test_unknown_evidence_id_rejected(self):
        item = {
            "situation": "看到离谱操作时",
            "style": "用短句吐槽",
            "evidence_ids": [99],
            "confidence": 0.8,
            "taboo": "none",
        }
        candidate, reason = normalize_candidate(item, comments_by_id=self.comments)
        self.assertIsNone(candidate)
        self.assertIn("不存在", reason)

    def test_low_confidence_rejected(self):
        item = {
            "situation": "看到离谱操作时",
            "style": "用短句吐槽",
            "evidence_ids": [1],
            "confidence": 0.2,
            "taboo": "none",
        }
        candidate, _ = normalize_candidate(item, comments_by_id=self.comments)
        self.assertIsNone(candidate)

    def test_strong_taboo_rejected(self):
        item = {
            "situation": "看到离谱操作时",
            "style": "用短句吐槽",
            "evidence_ids": [1],
            "confidence": 0.9,
            "taboo": "strong",
        }
        candidate, _ = normalize_candidate(item, comments_by_id=self.comments)
        self.assertIsNone(candidate)

    def test_missing_evidence_rejected(self):
        item = {
            "situation": "看到离谱操作时",
            "style": "用短句吐槽",
            "confidence": 0.9,
            "taboo": "none",
        }
        candidate, _ = normalize_candidate(item, comments_by_id=self.comments)
        self.assertIsNone(candidate)

    def test_bar_specific_term_rejected(self):
        item = {
            "situation": "对方说离谱话时",
            "style": "用「出院」作为调侃术语，暗示对方需要精神科",
            "evidence_ids": [4],
            "confidence": 0.9,
            "taboo": "none",
        }
        candidate, reason = normalize_candidate(item, comments_by_id=self.comments)
        self.assertIsNone(candidate)
        self.assertIn("黑话", reason)


class PromptTests(unittest.TestCase):
    def test_extract_prompt_contains_ids_and_schema(self):
        prompt = build_extract_prompt([{"id": 7, "text": "不是，哥们"}], max_candidates=3)
        self.assertIn('"id": 7', prompt)
        self.assertIn("evidence_ids", prompt)
        self.assertIn("最多 3 条", prompt)

    def test_review_prompt_contains_candidates(self):
        prompt = build_review_prompt(
            [{"situation": "情境", "style": "表达", "examples": ["例子"]}]
        )
        self.assertIn('"index": 0', prompt)
        self.assertIn("keep", prompt)

    def test_parse_review_payload(self):
        parsed = parse_review_payload(
            '{"results":[{"index":0,"keep":true,"score":0.8,"reason":"ok"}]}', 1
        )
        self.assertTrue(parsed[0]["keep"])
        self.assertIsNone(parse_review_payload("bad", 1))


if __name__ == "__main__":
    unittest.main()
