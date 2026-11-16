from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from _helpers import load_package

load_package()

from comment_talk_learner.schedule import (  # noqa: E402
    SHANGHAI,
    day_key,
    jitter_seconds,
    next_run_at,
    scheduled_at,
    should_run,
)
from comment_talk_learner.state import default_state  # noqa: E402


def config(**overrides):
    values = {
        "daily_enabled": True,
        "daily_hour": 4,
        "daily_minute": 30,
        "jitter_minutes": 0,
        "run_on_start_if_missed": True,
        "max_retries_per_day": 2,
        "retry_interval_minutes": 30,
    }
    values.update(overrides)
    return values


def at(hour, minute, day=13):
    return datetime(2026, 11, day, hour, minute, tzinfo=SHANGHAI)


class ShouldRunTests(unittest.TestCase):
    def test_waiting_before_schedule(self):
        run, reason = should_run(at(4, 0), default_state(), config())
        self.assertFalse(run)
        self.assertEqual(reason, "waiting")

    def test_runs_at_schedule(self):
        start = scheduled_at(at(4, 0), 4, 30, 0)
        run, reason = should_run(start + timedelta(minutes=1), default_state(), config())
        self.assertTrue(run)
        self.assertEqual(reason, "scheduled")

    def test_does_not_run_twice(self):
        state = default_state()
        state["last_success_date"] = day_key(at(4, 0))
        run, reason = should_run(at(5, 0), state, config())
        self.assertFalse(run)
        self.assertEqual(reason, "already_succeeded")

    def test_retry_backoff(self):
        now = at(4, 40)
        state = default_state()
        state["last_attempt_date"] = day_key(now)
        state["failures_today"] = 1
        state["next_retry_at"] = (now + timedelta(minutes=10)).timestamp()
        run, reason = should_run(now, state, config())
        self.assertFalse(run)
        self.assertEqual(reason, "waiting_retry")
        run, reason = should_run(now + timedelta(minutes=11), state, config())
        self.assertTrue(run)
        self.assertEqual(reason, "retry")

    def test_retry_exhausted(self):
        now = at(5, 0)
        state = default_state()
        state["last_attempt_date"] = day_key(now)
        state["failures_today"] = 2
        run, reason = should_run(now, state, config())
        self.assertFalse(run)
        self.assertEqual(reason, "retry_exhausted")

    def test_startup_missed_policy(self):
        now = at(15, 0)
        run, reason = should_run(now, default_state(), config(run_on_start_if_missed=False), startup=True)
        self.assertFalse(run)
        self.assertEqual(reason, "startup_missed")
        run, reason = should_run(now, default_state(), config(run_on_start_if_missed=True), startup=True)
        self.assertTrue(run)
        self.assertEqual(reason, "startup_catchup")

    def test_skip_date(self):
        state = default_state()
        state["skip_date"] = day_key(at(4, 0))
        run, reason = should_run(at(5, 0), state, config())
        self.assertFalse(run)
        self.assertEqual(reason, "skipped_today")

    def test_disabled(self):
        run, reason = should_run(at(5, 0), default_state(), config(daily_enabled=False))
        self.assertFalse(run)
        self.assertEqual(reason, "daily_disabled")


class NextRunTests(unittest.TestCase):
    def test_today_then_tomorrow(self):
        now = at(3, 0)
        upcoming = next_run_at(now, default_state(), config())
        self.assertEqual((upcoming.hour, upcoming.minute), (4, 30))
        later = at(5, 0)
        upcoming = next_run_at(later, default_state(), config())
        self.assertEqual(upcoming.date(), (later + timedelta(days=1)).date())

    def test_after_success_tomorrow(self):
        now = at(5, 0)
        state = default_state()
        state["last_success_date"] = day_key(now)
        upcoming = next_run_at(now, state, config())
        self.assertEqual(upcoming.date(), (now + timedelta(days=1)).date())


class JitterTests(unittest.TestCase):
    def test_deterministic(self):
        first = jitter_seconds("2026-11-13", 4, 30, 20)
        second = jitter_seconds("2026-11-13", 4, 30, 20)
        self.assertEqual(first, second)
        self.assertGreaterEqual(first, 0)
        self.assertLessEqual(first, 1200)

    def test_zero(self):
        self.assertEqual(jitter_seconds("2026-11-13", 4, 30, 0), 0)


if __name__ == "__main__":
    unittest.main()
