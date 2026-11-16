from __future__ import annotations

import pathlib
import tempfile
import time
import unittest

from _helpers import load_package

load_package()

from comment_talk_learner.state import (  # noqa: E402
    default_state,
    imported_count,
    imported_since,
    is_thread_seen,
    is_video_seen,
    load_state,
    mark_threads_seen,
    mark_videos_seen,
    prune_seen_threads,
    prune_seen_videos,
    record_history,
    record_imported,
    save_state,
)


class StateFileTests(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "state.json"
            state = default_state()
            state["last_success_date"] = "2026-11-13"
            state["imported"]["1"] = {"situation": "a", "style": "b", "at": 1}
            save_state(path, state)
            loaded = load_state(path)
            self.assertEqual(loaded["last_success_date"], "2026-11-13")
            self.assertEqual(imported_count(loaded), 1)

    def test_corrupt_file_backed_up(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "state.json"
            path.write_text("{not json", encoding="utf-8")
            loaded = load_state(path)
            self.assertEqual(loaded["last_success_date"], "")
            backups = list(pathlib.Path(directory).glob("state.json.corrupt-*"))
            self.assertEqual(len(backups), 1)
            self.assertFalse(path.exists())

    def test_history_capped_and_imported(self):
        state = default_state()
        for index in range(30):
            record_history(state, {"index": index})
        self.assertEqual(len(state["history"]), 20)
        self.assertEqual(state["history"][-1]["index"], 29)
        record_imported(state, expression_id=7, situation="情境", style="表达", now_ts=1)
        self.assertEqual(state["imported"]["7"]["situation"], "情境")
        self.assertEqual(imported_since(state, 0), 1)
        self.assertEqual(imported_since(state, 2), 0)


class SeenVideoTests(unittest.TestCase):
    def test_prune_and_check(self):
        state = default_state()
        now = time.time()
        state["seen_videos"] = {"old": now - 20 * 86400, "fresh": now - 1 * 86400}
        removed = prune_seen_videos(state, now_ts=now, ttl_days=14)
        self.assertEqual(removed, 1)
        self.assertFalse(is_video_seen(state, "old", now_ts=now, ttl_days=14))
        self.assertTrue(is_video_seen(state, "fresh", now_ts=now, ttl_days=14))

    def test_mark_seen(self):
        state = default_state()
        mark_videos_seen(state, ["BV1", "BV2"], now_ts=100)
        self.assertTrue(is_video_seen(state, "BV1", now_ts=100, ttl_days=1))
        self.assertFalse(is_video_seen(state, "BV3", now_ts=100, ttl_days=1))


class SeenThreadTests(unittest.TestCase):
    def test_prune_and_check(self):
        state = default_state()
        now = time.time()
        state["seen_threads"] = {"old": now - 20 * 86400, "fresh": now - 86400}
        removed = prune_seen_threads(state, now_ts=now, ttl_days=14)
        self.assertEqual(removed, 1)
        self.assertFalse(is_thread_seen(state, "old", now_ts=now, ttl_days=14))
        self.assertTrue(is_thread_seen(state, "fresh", now_ts=now, ttl_days=14))

    def test_mark_seen(self):
        state = default_state()
        mark_threads_seen(state, ["123456", "654321"], now_ts=100)
        self.assertTrue(is_thread_seen(state, "123456", now_ts=100, ttl_days=1))
        self.assertFalse(is_thread_seen(state, "999999", now_ts=100, ttl_days=1))


if __name__ == "__main__":
    unittest.main()
