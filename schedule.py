"""每日调度判定（纯函数，便于测试）。"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Any

SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")


def _cfg_value(config: Any, name: str, default: Any) -> Any:
    if isinstance(config, dict):
        return config.get(name, default)
    return getattr(config, name, default)


def ensure_aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now 必须是带时区的 datetime")
    return now


def local_now(now: datetime) -> datetime:
    return ensure_aware(now).astimezone(SHANGHAI)


def day_key(now: datetime) -> str:
    return local_now(now).strftime("%Y-%m-%d")


def target_at(now: datetime, hour: int, minute: int) -> datetime:
    local = local_now(now)
    return local.replace(hour=max(0, min(23, int(hour))), minute=max(0, min(59, int(minute))), second=0, microsecond=0)


def jitter_seconds(day: str, hour: int, minute: int, jitter_minutes: int) -> int:
    """按天确定性地生成抖动，避免每次检查都换一个时间。"""
    span = max(0, int(jitter_minutes)) * 60
    if span <= 0:
        return 0
    rng = random.Random(f"{day}:{hour}:{minute}:v1")
    return rng.randint(0, span)


def scheduled_at(now: datetime, hour: int, minute: int, jitter_minutes: int) -> datetime:
    local = local_now(now)
    target = target_at(local, hour, minute)
    return target + timedelta(seconds=jitter_seconds(day_key(local), hour, minute, jitter_minutes))


def should_run(
    now: datetime,
    state: dict[str, Any],
    config: Any,
    *,
    startup: bool = False,
) -> tuple[bool, str]:
    """判断现在是否应该跑一轮。

    返回 ``(是否运行, 原因)``。原因用于日志和状态展示。
    """
    local = local_now(now)
    today = day_key(local)
    if not bool(_cfg_value(config, "daily_enabled", True)):
        return False, "daily_disabled"
    if state.get("skip_date") == today:
        return False, "skipped_today"
    if state.get("last_success_date") == today:
        return False, "already_succeeded"

    hour = int(_cfg_value(config, "daily_hour", 4))
    minute = int(_cfg_value(config, "daily_minute", 30))
    jitter_minutes = int(_cfg_value(config, "jitter_minutes", 20))
    scheduled = scheduled_at(local, hour, minute, jitter_minutes)

    if state.get("last_attempt_date") == today:
        failures = int(state.get("failures_today") or 0)
        max_retries = int(_cfg_value(config, "max_retries_per_day", 2))
        if failures >= max_retries:
            return False, "retry_exhausted"
        next_retry_at = float(state.get("next_retry_at") or 0)
        if local.timestamp() < next_retry_at:
            return False, "waiting_retry"
        return True, "retry"

    if local < scheduled:
        return False, "waiting"

    if startup and not bool(_cfg_value(config, "run_on_start_if_missed", True)):
        # 启动时已经错过太久，今天不再补跑；插件会把 skip_date 写成今天。
        if local - scheduled > timedelta(hours=1):
            return False, "startup_missed"
    return True, "startup_catchup" if startup else "scheduled"


def next_run_at(now: datetime, state: dict[str, Any], config: Any) -> datetime:
    """下一次计划运行时间，用于状态展示。"""
    local = local_now(now)
    today = day_key(local)
    hour = int(_cfg_value(config, "daily_hour", 4))
    minute = int(_cfg_value(config, "daily_minute", 30))
    jitter_minutes = int(_cfg_value(config, "jitter_minutes", 20))
    today_run = scheduled_at(local, hour, minute, jitter_minutes)
    if state.get("last_success_date") == today or state.get("skip_date") == today:
        tomorrow = local + timedelta(days=1)
        return scheduled_at(tomorrow, hour, minute, jitter_minutes)
    if local < today_run:
        return today_run
    return scheduled_at(local + timedelta(days=1), hour, minute, jitter_minutes)


def format_dt(value: datetime) -> str:
    return local_now(value).strftime("%Y-%m-%d %H:%M")
