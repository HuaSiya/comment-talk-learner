from __future__ import annotations

import unittest

from _helpers import load_package

load_package()

from comment_talk_learner.bili import (  # noqa: E402
    BiliClient,
    BiliError,
    merge_video_lists,
)


class ParseVideoTests(unittest.TestCase):
    def test_popular_and_ranking_shape(self):
        raw = [
            {
                "bvid": "BV1xx411c7mD",
                "aid": 123,
                "title": "标题",
                "owner": {"name": "UP"},
                "stat": {"view": 1000, "reply": 80},
                "pubdate": 1700000000,
                "duration": 300,
                "tname": "生活",
            }
        ]
        videos = BiliClient._parse_video_list(raw, source="popular")
        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0]["bvid"], "BV1xx411c7mD")
        self.assertEqual(videos[0]["aid"], 123)
        self.assertEqual(videos[0]["reply"], 80)
        self.assertEqual(videos[0]["duration"], 300)

    def test_search_shape_and_em_tags(self):
        raw = [
            {
                "bvid": "BV1yy411c7mE",
                "title": '<em class="keyword">测试</em>视频',
                "author": "作者",
                "play": "2.3万",
                "review": 42,
                "pubdate": 1700000000,
                "duration": "12:34",
            }
        ]
        videos = BiliClient._parse_video_list(raw, source="search:x")
        self.assertEqual(videos[0]["title"], "测试视频")
        self.assertEqual(videos[0]["owner"], "作者")
        self.assertEqual(videos[0]["duration"], 754)

    def test_invalid_items_skipped(self):
        self.assertEqual(BiliClient._parse_video_list([None, {}, {"bvid": ""}], source="x"), [])


class ParseCommentTests(unittest.TestCase):
    def test_comment_shape(self):
        raw = {
            "rpid": 9,
            "like": 10,
            "ctime": 1700000000,
            "content": {"message": "这句真好笑"},
            "member": {"uname": "用户"},
        }
        parsed = BiliClient._parse_comment(raw)
        self.assertEqual(parsed["rpid"], 9)
        self.assertEqual(parsed["message"], "这句真好笑")
        self.assertEqual(parsed["user"], "用户")

    def test_empty_comment_skipped(self):
        self.assertIsNone(BiliClient._parse_comment({"content": {"message": "  "}}))
        self.assertIsNone(BiliClient._parse_comment(None))


class CommentsPagingTests(unittest.TestCase):
    def test_pages_and_dedup(self):
        client = BiliClient(rate_limit=0)
        pages = [
            {
                "code": 0,
                "data": {
                    "replies": [
                        {"rpid": 1, "content": {"message": "第一条"}, "like": 3},
                        {"rpid": 1, "content": {"message": "重复"}, "like": 3},
                    ],
                    "cursor": {"next": 2, "is_end": False},
                },
            },
            {
                "code": 0,
                "data": {
                    "replies": [{"rpid": 2, "content": {"message": "第二条"}, "like": 1}],
                    "cursor": {"next": 0, "is_end": True},
                },
            },
        ]
        calls = []

        def fake_get_json(url):
            calls.append(url)
            return pages[len(calls) - 1]

        client._get_json = fake_get_json  # type: ignore[method-assign]
        comments = client.comments(123, pages=2, page_size=20)
        self.assertEqual([item["message"] for item in comments], ["第一条", "第二条"])
        self.assertEqual(len(calls), 2)
        self.assertIn("oid=123", calls[0])

    def test_risk_control_raises(self):
        client = BiliClient(rate_limit=0)

        # _get_json 的校验逻辑在真实方法里；这里直接验证真实方法会抛错。
        client._raw_json = lambda url: {"code": -352, "message": "风控"}  # type: ignore[method-assign]
        with self.assertRaises(BiliError):
            client._get_json("https://example.com")

    def test_sub_replies_included(self):
        client = BiliClient(rate_limit=0)

        def fake_get_json(url):
            if "/x/v2/reply/reply" in url:
                return {
                    "code": 0,
                    "data": {
                        "replies": [
                            {"rpid": 2, "content": {"message": "楼中楼一"}},
                            {"rpid": 3, "content": {"message": "楼中楼二"}},
                        ]
                    },
                }
            return {
                "code": 0,
                "data": {
                    "replies": [
                        {"rpid": 1, "rcount": 2, "content": {"message": "根评论"}},
                    ],
                    "cursor": {"is_end": True, "next": 0},
                },
            }

        client._get_json = fake_get_json  # type: ignore[method-assign]
        comments = client.comments(
            123,
            pages=1,
            include_sub_replies=True,
            sub_reply_roots=1,
            sub_replies_per_root=20,
        )
        self.assertEqual([item["message"] for item in comments], ["根评论", "楼中楼一", "楼中楼二"])


class MergeTests(unittest.TestCase):
    def test_round_robin_priorities(self):
        groups = [
            [{"bvid": "A"}, {"bvid": "B"}],
            [{"bvid": "C"}, {"bvid": "D"}],
            [{"bvid": "E"}],
        ]
        merged = merge_video_lists(groups, limit=5)
        self.assertEqual([item["bvid"] for item in merged], ["A", "C", "E", "B", "D"])

    def test_limit_and_duplicates(self):
        merged = merge_video_lists([[{"bvid": "A"}, {"bvid": "A"}], [{"bvid": "B"}]], limit=1)
        self.assertEqual([item["bvid"] for item in merged], ["A"])


if __name__ == "__main__":
    unittest.main()
