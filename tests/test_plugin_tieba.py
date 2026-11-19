from __future__ import annotations

import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock

from _helpers import load_package

load_package()

from maibot_sdk.context import PluginContext  # noqa: E402

from local_bilibili_comment_learner.plugin import (  # noqa: E402
    BilibiliCommentLearner,
    CommentLearnerConfig,
)
from local_bilibili_comment_learner.state import default_state, record_imported  # noqa: E402
from local_bilibili_comment_learner.tieba import TiebaError  # noqa: E402


class FakeTieba:
    def __init__(self):
        self.thread_calls = []

    def bar_threads(self, bar, *, pages=2, limit=30):
        return [{"tid": "123456", "title": "今天的离谱事", "reply_num": 80, "abstract": ""}]

    def thread(self, tid, *, pages=1, limit=30):
        self.thread_calls.append(tid)
        return {
            "posts": [
                {"content": "楼主首楼", "is_op": True, "agree": 1},
                {"content": "这也太离谱了吧，我真服了", "is_op": False, "agree": 5},
                {"content": "[图片]", "is_op": False, "agree": 0},
                {"content": "不是，哥们，这也能行？", "is_op": False, "agree": 2},
                {"content": "我昨天也遇到类似的事", "is_op": False, "agree": 4},
                {"content": "换我我肯定不敢这么干", "is_op": False, "agree": 3},
                {"content": "他后来怎么样了，有人知道吗", "is_op": False, "agree": 1},
            ]
        }


class FailingTieba:
    def bar_threads(self, bar, *, pages=2, limit=30):
        raise TiebaError("贴吧接口繁忙或触发风控（110001）")

    def thread(self, tid, *, pages=1, limit=30):
        raise TiebaError("should not be called")


class ShortTieba:
    def bar_threads(self, bar, *, pages=2, limit=30):
        return [{"tid": "short1", "title": "楼层不够", "reply_num": 80, "abstract": ""}]

    def thread(self, tid, *, pages=1, limit=30):
        return {
            "posts": [
                {"content": "这也太离谱了吧，我真服了", "is_op": False, "agree": 5},
                {"content": "不是，哥们，这也能行？", "is_op": False, "agree": 2},
                {"content": "我昨天也遇到类似的事", "is_op": False, "agree": 4},
                {"content": "换我我肯定不敢这么干", "is_op": False, "agree": 3},
            ]
        }


class MultiBarFake:
    """每个吧都有多个候选，用来验证 threads_per_bar 的分配。"""

    def bar_threads(self, bar, *, pages=2, limit=30):
        prefix = "1" if "A" in bar else "2"
        return [
            {"tid": f"{prefix}001", "title": f"{bar}帖子一", "reply_num": 80, "abstract": ""},
            {"tid": f"{prefix}002", "title": f"{bar}帖子二", "reply_num": 70, "abstract": ""},
        ]

    def thread(self, tid, *, pages=1, limit=30):
        return {
            "posts": [
                {
                    "content": f"{tid} 的回复内容第{i}条，这也太离谱了吧",
                    "is_op": False,
                    "agree": 3,
                }
                for i in range(5)
            ]
        }


class SchemaLocalizationTests(unittest.TestCase):
    def test_labels_and_task_select(self):
        schema = BilibiliCommentLearner.build_config_schema(
            plugin_id="local.test",
            plugin_name="test",
            plugin_version="0.0.0",
        )
        fields = schema["sections"]["plugin"]["fields"]
        self.assertEqual(fields["enabled"]["label"], "启用插件")
        self.assertIsNone(fields["max_expressions_per_day"]["max"])
        self.assertEqual(fields["llm_task"]["label"], "分析模型任务")
        self.assertEqual(fields["llm_task"]["ui_type"], "select")
        self.assertIn("learner", fields["llm_task"]["options"])
        self.assertIn("learner", fields["llm_task"]["choices"])
        self.assertIn("宿主模型任务", fields["llm_task"]["hint"])
        self.assertEqual(fields["llm_task"]["x-description-display"], "inline")
        self.assertNotIn("llm_model", fields)
        sources = schema["sections"]["sources"]["fields"]
        self.assertEqual(sources["sessdata"]["label"], "B站 SESSDATA")
        self.assertEqual(sources["rate_limit_seconds"]["label"], "B站请求间隔（秒）")
        self.assertEqual(sources["formulaic_filter"]["label"], "严格过滤公式化评论")
        self.assertIn("公式化", sources["formulaic_filter"]["hint"])
        self.assertEqual(sources["max_comments_per_video"]["label"], "每视频有效评论数")
        self.assertIn("跳过", sources["max_comments_per_video"]["hint"])
        self.assertIn("51", sources["min_reply_count"]["hint"])
        self.assertEqual(sources["reaction_keep_ratio"]["label"], "纯反应保留比例")
        self.assertIn("8%", sources["reaction_keep_ratio"]["hint"])
        self.assertEqual(sources["ai_bot_threshold"]["label"], "AI/Bot 判定分数")
        self.assertIn("60", sources["ai_bot_threshold"]["hint"])
        tieba = schema["sections"]["tieba"]["fields"]
        self.assertEqual(tieba["enabled"]["label"], "启用贴吧来源")
        self.assertEqual(tieba["threads_per_bar"]["label"], "每吧达标帖子数")
        self.assertEqual(tieba["quality_filter"]["label"], "严格过滤公式化楼层")
        self.assertEqual(tieba["max_posts_per_thread"]["label"], "每帖有效楼层数")
        self.assertIn("51", tieba["min_replies"]["hint"])


class ConfigMigrationTests(unittest.TestCase):
    def test_legacy_bili_fields_move_to_sources(self):
        cfg = CommentLearnerConfig.model_validate(
            {
                "plugin": {"max_videos_per_run": 3, "rate_limit_seconds": 2.5},
                "sources": {"popular_enabled": False},
                "tieba": {},
            }
        )
        self.assertEqual(cfg.sources.max_videos_per_run, 3)
        self.assertEqual(cfg.sources.rate_limit_seconds, 2.5)
        self.assertNotIn("max_videos_per_run", cfg.model_dump()["plugin"])

    def test_new_sources_value_wins(self):
        cfg = CommentLearnerConfig.model_validate(
            {
                "plugin": {"max_videos_per_run": 3},
                "sources": {"max_videos_per_run": 5},
                "tieba": {},
            }
        )
        self.assertEqual(cfg.sources.max_videos_per_run, 5)


class TiebaCollectionTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self):
        plugin = BilibiliCommentLearner()
        plugin.set_plugin_config(
            {
                "plugin": {"config_version": "1.0.0", "min_post_chars": 6},
                "sources": {},
                "tieba": {
                    "config_version": "1.0.0",
                    "enabled": True,
                    "bars": ["弱智吧"],
                    "scan_pages": 1,
                    "max_threads": 1,
                    "thread_pages": 1,
                    "min_replies": 10,
                    "max_posts_per_thread": 5,
                    "min_post_chars": 6,
                    "max_post_chars": 300,
                    "include_op": False,
                },
            }
        )
        plugin._state = default_state()
        return plugin

    async def test_collects_replies_and_skips_op(self):
        plugin = self.make_plugin()
        comments, by_id, processed, next_id, warnings = await plugin._collect_tieba(
            FakeTieba(), plugin.config.tieba, start_id=5
        )
        self.assertCountEqual(
            [item["text"] for item in comments],
            [
                "这也太离谱了吧，我真服了",
                "不是，哥们，这也能行？",
                "我昨天也遇到类似的事",
                "换我我肯定不敢这么干",
                "他后来怎么样了，有人知道吗",
            ],
        )
        self.assertCountEqual([item["id"] for item in comments], [5, 6, 7, 8, 9])
        self.assertEqual(by_id[5], "这也太离谱了吧，我真服了")
        self.assertEqual(processed, ["123456"])
        self.assertEqual(next_id, 10)
        self.assertFalse(any("达标帖子" in item for item in warnings))

    async def test_risk_control_is_warning_not_crash(self):
        plugin = self.make_plugin()
        comments, by_id, processed, next_id, warnings = await plugin._collect_tieba(
            FailingTieba(), plugin.config.tieba, start_id=1
        )
        self.assertEqual(comments, [])
        self.assertEqual(by_id, {})
        self.assertEqual(processed, [])
        self.assertTrue(any("110001" in item for item in warnings))

    async def test_thread_with_too_few_valid_posts_is_skipped(self):
        plugin = self.make_plugin()
        comments, by_id, processed, next_id, warnings = await plugin._collect_tieba(
            ShortTieba(), plugin.config.tieba, start_id=1
        )
        self.assertEqual(comments, [])
        self.assertEqual(by_id, {})
        self.assertEqual(processed, [])
        self.assertEqual(next_id, 1)
        self.assertTrue(any("有效楼层 4/5" in item for item in warnings))
        self.assertTrue(any("贴吧达标帖子 0/1" in item for item in warnings))

    async def test_threads_per_bar_quota(self):
        plugin = self.make_plugin()
        plugin.set_plugin_config(
            {
                "plugin": {"config_version": "1.0.0"},
                "sources": {},
                "tieba": {
                    "config_version": "1.0.0",
                    "enabled": True,
                    "bars": ["A吧", "B吧"],
                    "scan_pages": 1,
                    "max_threads": 0,
                    "threads_per_bar": 1,
                    "thread_pages": 1,
                    "min_replies": 10,
                    "max_posts_per_thread": 5,
                    "min_post_chars": 6,
                    "max_post_chars": 300,
                    "include_op": False,
                },
            }
        )
        plugin._state = default_state()
        comments, by_id, processed, next_id, warnings = await plugin._collect_tieba(
            MultiBarFake(), plugin.config.tieba, start_id=1
        )
        self.assertEqual(len(processed), 2)
        self.assertTrue(any(tid.startswith("1") for tid in processed))
        self.assertTrue(any(tid.startswith("2") for tid in processed))
        self.assertEqual(warnings, [])


class FakeBili:
    NORMAL = [
        "这也太离谱了吧，我今天第一次见",
        "不是，哥们，这也行？",
        "这个视频的BGM是什么，挺好听",
        "我昨天在店里也遇到类似的事",
        "说实话这个处理方式有点意思",
        "换我我肯定不敢这么干",
        "老板这波操作我是真没想到",
        "他后来怎么样了，有人知道吗",
        "这事要是我妈知道了肯定得念叨",
        "评论区有没有懂行的说说",
    ]

    def comments(self, aid, **kwargs):
        return [
            {"message": "火钳刘明", "like": 100},
            {"message": "前排打卡", "like": 90},
            {"message": "下次一定", "like": 85},
            {"message": "哈哈哈哈", "like": 60},
            *[{"message": text, "like": 10} for text in self.NORMAL],
        ]


class BiliCollectionQualityTests(unittest.IsolatedAsyncioTestCase):
    async def test_collection_filters_formulaic_comments(self):
        plugin = BilibiliCommentLearner()
        plugin.set_plugin_config(
            {
                "plugin": {"config_version": "1.0.0"},
                "sources": {
                    "config_version": "1.0.0",
                    "formulaic_filter": True,
                    "reaction_keep_ratio": 0.0,
                    "ai_bot_threshold": 60,
                    "max_comments_per_video": 10,
                    "include_sub_replies": False,
                    "min_comment_chars": 4,
                    "max_comment_chars": 120,
                },
                "tieba": {},
            }
        )
        plugin._state = default_state()
        comments, by_id, processed, next_id, warnings = await plugin._collect_comments(
            FakeBili(),
            [{"bvid": "BV1", "aid": 1, "reply": 100}],
            plugin.config.sources,
        )
        self.assertCountEqual([item["text"] for item in comments], FakeBili.NORMAL)
        self.assertEqual(processed, ["BV1"])
        self.assertEqual(next_id, 11)
        self.assertEqual(len(by_id), 10)
        self.assertTrue(any("B站质量过滤" in item for item in warnings))

    async def test_video_with_too_few_valid_comments_is_skipped(self):
        plugin = BilibiliCommentLearner()
        plugin.set_plugin_config(
            {
                "plugin": {"config_version": "1.0.0"},
                "sources": {
                    "config_version": "1.0.0",
                    "formulaic_filter": True,
                    "reaction_keep_ratio": 0.0,
                    "ai_bot_threshold": 60,
                    "max_comments_per_video": 11,
                    "include_sub_replies": False,
                    "min_comment_chars": 4,
                    "max_comment_chars": 120,
                },
                "tieba": {},
            }
        )
        plugin._state = default_state()
        comments, by_id, processed, next_id, warnings = await plugin._collect_comments(
            FakeBili(),
            [{"bvid": "BV1", "aid": 1, "reply": 100}],
            plugin.config.sources,
        )
        self.assertEqual(comments, [])
        self.assertEqual(by_id, {})
        self.assertEqual(processed, [])
        self.assertEqual(next_id, 1)
        self.assertTrue(any("有效评论 10/11" in item for item in warnings))
        self.assertTrue(any("B站达标视频 0/6" in item for item in warnings))

    def test_reply_count_boundary(self):
        self.assertFalse(BilibiliCommentLearner._reply_count_ok(50, 50))
        self.assertTrue(BilibiliCommentLearner._reply_count_ok(51, 50))
        self.assertFalse(BilibiliCommentLearner._reply_count_ok(None, 50))


class LLMTimeoutTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self):
        plugin = BilibiliCommentLearner()
        plugin.set_plugin_config(
            {
                "plugin": {
                    "config_version": "1.0.0",
                    "llm_task": "learner",
                    "llm_fallback_task": "utils",
                    "llm_timeout_seconds": 180,
                    "llm_batch_size": 40,
                    "max_candidates_per_call": 4,
                    "min_confidence": 0.6,
                },
                "sources": {},
                "tieba": {},
            }
        )
        return plugin

    async def test_primary_timeout_falls_back_to_second_task(self):
        plugin = self.make_plugin()
        valid = json.dumps(
            [
                {
                    "situation": "看到离谱操作时",
                    "style": "用短句吐槽",
                    "evidence_ids": [1],
                    "confidence": 0.9,
                    "taboo": "none",
                }
            ],
            ensure_ascii=False,
        )
        plugin._llm_generate = AsyncMock(side_effect=[TimeoutError("timeout"), valid])
        comments = [{"id": 1, "text": "这也太离谱了吧"}]
        candidates, warnings = await plugin._extract_candidates(
            comments, {1: "这也太离谱了吧"}, plugin.config.plugin
        )
        self.assertEqual(len(candidates), 1)
        self.assertTrue(any("抽取模型失败" in item for item in warnings))
        self.assertEqual(plugin._llm_generate.await_count, 2)

    async def test_timeout_is_forwarded_to_capability(self):
        plugin = self.make_plugin()
        captured = {}

        async def rpc(method, plugin_id, payload, **kwargs):
            captured["timeout_ms"] = kwargs.get("timeout_ms")
            return {"success": True, "response": "[]"}

        plugin._set_context(PluginContext("test", rpc_call=rpc))
        await plugin._llm_generate(
            "prompt",
            task_name="learner",
            max_tokens=100,
            temperature=0.1,
            timeout_ms=180000,
        )
        self.assertEqual(captured["timeout_ms"], 180000)


class QuotaGuardTests(unittest.IsolatedAsyncioTestCase):
    def test_zero_daily_limit_means_unlimited(self):
        plugin = BilibiliCommentLearner()
        plugin.set_plugin_config(
            {
                "plugin": {
                    "config_version": "1.0.0",
                    "max_expressions_per_day": 0,
                    "max_total_imported": 100,
                },
                "sources": {},
                "tieba": {},
            }
        )
        plugin._state = default_state()
        for index in range(5):
            record_imported(plugin._state, expression_id=index, situation="情境", style="表达", now_ts=time.time())
        selected, skipped, _warning = plugin._dedup_and_quota(
            [
                {"situation": "情境A", "style": "用短句吐槽"},
                {"situation": "情境B", "style": "用反问接话"},
                {"situation": "情境C", "style": "先自嘲再收尾"},
            ],
            [],
            plugin.config.plugin,
        )
        self.assertEqual(len(selected), 3)
        self.assertEqual(skipped, 0)

    async def test_skips_before_fetch_when_daily_quota_used(self):
        plugin = BilibiliCommentLearner()
        plugin.set_plugin_config(
            {
                "plugin": {
                    "config_version": "1.0.0",
                    "max_expressions_per_day": 1,
                    "write_to_db": True,
                },
                "sources": {},
                "tieba": {},
            }
        )
        plugin._lock = asyncio.Lock()
        plugin._state = default_state()
        record_imported(plugin._state, expression_id=1, situation="情境", style="表达", now_ts=time.time())
        plugin._run_once = AsyncMock()
        result = await plugin._run_guarded(trigger="manual")
        self.assertEqual(result["status"], "quota_exhausted")
        plugin._run_once.assert_not_awaited()
        self.assertIn("今日配额已用完", result["warnings"][0])


if __name__ == "__main__":
    unittest.main()
