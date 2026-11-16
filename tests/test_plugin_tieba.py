from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock

from _helpers import load_package

load_package()

from maibot_sdk.context import PluginContext  # noqa: E402

from comment_talk_learner.plugin import (  # noqa: E402
    BilibiliCommentLearner,
    CommentLearnerConfig,
)
from comment_talk_learner.state import default_state  # noqa: E402
from comment_talk_learner.tieba import TiebaError  # noqa: E402


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
            ]
        }


class FailingTieba:
    def bar_threads(self, bar, *, pages=2, limit=30):
        raise TiebaError("贴吧接口繁忙或触发风控（110001）")

    def thread(self, tid, *, pages=1, limit=30):
        raise TiebaError("should not be called")


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
                {"content": f"{tid} 的回复内容，这也太离谱了吧", "is_op": False, "agree": 3},
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
        tieba = schema["sections"]["tieba"]["fields"]
        self.assertEqual(tieba["enabled"]["label"], "启用贴吧来源")
        self.assertEqual(tieba["threads_per_bar"]["label"], "每吧帖子数")


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
                    "max_threads": 2,
                    "thread_pages": 1,
                    "min_replies": 10,
                    "max_posts_per_thread": 30,
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
        self.assertEqual([item["text"] for item in comments], ["这也太离谱了吧，我真服了", "不是，哥们，这也能行？"])
        self.assertEqual([item["id"] for item in comments], [5, 6])
        self.assertEqual(by_id[5], "这也太离谱了吧，我真服了")
        self.assertEqual(processed, ["123456"])
        self.assertEqual(next_id, 7)
        self.assertEqual(warnings, [])

    async def test_risk_control_is_warning_not_crash(self):
        plugin = self.make_plugin()
        comments, by_id, processed, next_id, warnings = await plugin._collect_tieba(
            FailingTieba(), plugin.config.tieba, start_id=1
        )
        self.assertEqual(comments, [])
        self.assertEqual(by_id, {})
        self.assertEqual(processed, [])
        self.assertTrue(any("110001" in item for item in warnings))

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
                    "max_posts_per_thread": 30,
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


if __name__ == "__main__":
    unittest.main()
