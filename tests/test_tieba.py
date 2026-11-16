from __future__ import annotations

import hashlib
import unittest

from _helpers import load_package

load_package()

from comment_talk_learner.tieba import (  # noqa: E402
    TiebaClient,
    normalize_bar,
    normalize_post,
    normalize_thread,
    parse_bar_threads_html,
    parse_thread_posts_html,
    select_threads,
)


class NormalizeTests(unittest.TestCase):
    def test_bar_name(self):
        self.assertEqual(normalize_bar("弱智"), "弱智吧")
        self.assertEqual(normalize_bar("弱智吧"), "弱智吧")
        self.assertEqual(normalize_bar("#生活吧#"), "生活吧")
        self.assertEqual(normalize_bar(""), "")

    def test_thread(self):
        raw = {
            "id": "123456789",
            "title": "今天的离谱事",
            "abstract": "摘要",
            "author": {"name_show": "吧友"},
            "reply_num": "88",
            "view_num": "1000",
            "agree_num": "7",
            "create_time": 1700000000,
            "is_top": 1,
        }
        thread = normalize_thread(raw)
        self.assertEqual(thread["tid"], "123456789")
        self.assertEqual(thread["title"], "今天的离谱事")
        self.assertEqual(thread["reply_num"], 88)
        self.assertEqual(thread["author"], "吧友")
        self.assertTrue(thread["is_top"])
        self.assertEqual(thread["url"], "https://tieba.baidu.com/p/123456789")

    def test_post_text_and_image(self):
        raw = {
            "id": "p1",
            "floor": 2,
            "content": [{"text": "这也太离谱了"}, {"type": 3, "src": "x.jpg"}],
            "author": {"name": "A"},
            "agree": {"agree_num": 3},
        }
        post = normalize_post(raw)
        self.assertEqual(post["content"], "这也太离谱了\n[图片]")
        self.assertEqual(post["agree"], 3)
        self.assertFalse(post["is_op"])

    def test_op_floor(self):
        post = normalize_post({"id": "p1", "floor": 1, "content": "楼主"})
        self.assertTrue(post["is_op"])

    def test_invalid(self):
        self.assertEqual(normalize_thread({}), {})
        self.assertEqual(normalize_thread({"id": "abc"}), {})


class SignTests(unittest.TestCase):
    def test_sign_matches_manual(self):
        params = {"kw": "弱智吧", "pn": 0, "rn": 30, "sort_type": 0}
        raw = "&".join(f"{key}={params[key]}" for key in sorted(params))
        expected = hashlib.md5((raw + "tiebaclient!!!").encode("utf-8")).hexdigest()
        self.assertEqual(TiebaClient.sign_params(params), expected)


class ClientTests(unittest.TestCase):
    def test_forum_threads_normalizes(self):
        client = TiebaClient(rate_limit=0)
        client._client_get = lambda path, params: {  # type: ignore[method-assign]
            "thread_list": [
                {"id": "123456", "title": "标题", "reply_num": 30},
                {"id": "bad", "title": "坏 id"},
            ],
            "forum": {"name": "弱智吧"},
        }
        page = client.forum_threads("弱智", page=0, limit=30)
        self.assertEqual(page["bar"], "弱智吧")
        self.assertEqual(len(page["threads"]), 1)
        self.assertEqual(page["threads"][0]["tid"], "123456")

    def test_thread_posts_normalizes(self):
        client = TiebaClient(rate_limit=0)
        client._client_get = lambda path, params: {  # type: ignore[method-assign]
            "thread": {"id": "123456", "title": "标题"},
            "post_list": [
                {"id": "p1", "floor": 1, "content": [{"text": "楼主"}]},
                {"id": "p2", "floor": 2, "content": [{"text": "回复"}]},
            ],
            "page": {"total_page": 1},
        }
        page = client.thread_posts("123456", page=0, limit=30)
        self.assertEqual(len(page["posts"]), 2)
        self.assertTrue(page["posts"][0]["is_op"])

    def test_bar_threads_stops_on_short_page(self):
        client = TiebaClient(rate_limit=0)
        calls = []

        def fake_forum_threads(bar, *, page=0, limit=30, sort_type=0):
            calls.append(page)
            return {"threads": [{"tid": f"1000{page}", "title": "x", "reply_num": 1}]}

        client.forum_threads = fake_forum_threads  # type: ignore[method-assign]
        threads = client.bar_threads("吧", pages=3, limit=30)
        self.assertEqual(len(threads), 1)
        self.assertEqual(calls, [0])


class WebParseTests(unittest.TestCase):
    def test_bar_threads_html(self):
        page = """
        <ul class="threads_list">
          <li class="tl_top" data-tid="1"><div class="ti_title"><span>置顶帖</span></div></li>
          <li class="tl_shadow" data-tid="123456">
            <div class="ti_title"><span>今天的离谱事</span></div>
            <div class="ti_func_btn btn_reply"><span class="btn_icon">67</span></div>
          </li>
        </ul>
        """
        threads = parse_bar_threads_html(page, "生活吧")
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0]["tid"], "123456")
        self.assertEqual(threads[0]["title"], "今天的离谱事")
        self.assertEqual(threads[0]["reply_num"], 67)

    def test_thread_posts_html(self):
        page = """
        <title>标题</title>
        <div class="content" lz="1">楼主首楼<br>第二行</div>
        <div class="content" lz="0">回复文本<div id="pb_less_imgs"><div>图片说明</div></div>结尾</div>
        <div class="content" lz="0">   </div>
        """
        title, posts = parse_thread_posts_html(page, "123456")
        self.assertEqual(title, "标题")
        self.assertEqual(len(posts), 2)
        self.assertTrue(posts[0]["is_op"])
        self.assertEqual(posts[0]["content"], "楼主首楼\n第二行")
        self.assertFalse(posts[1]["is_op"])
        self.assertIn("回复文本", posts[1]["content"])
        self.assertIn("结尾", posts[1]["content"])
        self.assertNotIn("图片说明", posts[1]["content"])


class SelectThreadsTests(unittest.TestCase):
    def setUp(self):
        self.threads = [
            {"tid": "1", "title": "普通帖", "abstract": "", "reply_num": 100},
            {"tid": "2", "title": "水楼", "abstract": "", "reply_num": 200},
            {"tid": "3", "title": "只有一点点", "abstract": "", "reply_num": 10},
            {"tid": "4", "title": "已看", "abstract": "", "reply_num": 100},
            {"tid": "5", "title": "生活求助", "abstract": "", "reply_num": 50},
        ]

    def test_filters_and_sort(self):
        selected = select_threads(
            self.threads,
            seen={"4"},
            min_replies=30,
            max_threads=2,
            keywords=[],
            exclude_pattern="水楼",
            bar="测试吧",
        )
        self.assertEqual([item["tid"] for item in selected], ["1", "5"])
        self.assertEqual(selected[0]["bar"], "测试吧")

    def test_keyword_filter(self):
        selected = select_threads(
            self.threads,
            seen=set(),
            min_replies=0,
            max_threads=10,
            keywords=["生活"],
            bar="测试吧",
        )
        self.assertEqual([item["tid"] for item in selected], ["5"])

    def test_limit(self):
        selected = select_threads(self.threads, seen=set(), min_replies=0, max_threads=1)
        self.assertEqual(len(selected), 1)


if __name__ == "__main__":
    unittest.main()
