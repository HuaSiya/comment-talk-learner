"""插件状态文件：原子读写、运行记录、已看视频与已导入快照。"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("bilibili_comment_learner.state")

STATE_VERSION = 1
MAX_HISTORY = 20


def default_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "last_attempt_date": "",
        "last_success_date": "",
        "skip_date": "",
        "last_attempt_at": 0,
        "last_success_at": 0,
        "last_status": "",
        "last_error": "",
        "failures_today": 0,
        "next_retry_at": 0,
        "seen_videos": {},
        "seen_threads": {},
        "imported": {},
        "stats": {
            "runs": 0,
            "videos": 0,
            "tieba_threads": 0,
            "comments": 0,
            "candidates": 0,
            "imported": 0,
            "failed_writes": 0,
        },
        "history": [],
    }


def _merge_defaults(state: dict[str, Any]) -> dict[str, Any]:
    merged = default_state()
    for key, value in state.items():
        if key == "stats" and isinstance(value, dict):
            merged["stats"].update({k: v for k, v in value.items() if isinstance(v, (int, float))})
        elif key == "history" and isinstance(value, list):
            merged["history"] = [item for item in value if isinstance(item, dict)][-MAX_HISTORY:]
        elif key == "seen_videos" and isinstance(value, dict):
            merged["seen_videos"] = {
                str(k): v for k, v in value.items() if isinstance(v, (int, float)) and not isinstance(v, bool)
            }
        elif key == "seen_threads" and isinstance(value, dict):
            merged["seen_threads"] = {
                str(k): v for k, v in value.items() if isinstance(v, (int, float)) and not isinstance(v, bool)
            }
        elif key == "imported" and isinstance(value, dict):
            merged["imported"] = {str(k): v for k, v in value.items() if isinstance(v, dict)}
        elif (
            isinstance(merged[key], (int, float))
            and not isinstance(merged[key], bool)
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            # 时间戳会以 float 写入，默认值可能是 int；数值类型之间允许互换。
            merged[key] = value
        elif key in merged and not isinstance(value, type(merged[key])) and merged[key] is not None:
            # 类型不匹配时保留默认值，避免坏状态把插件带崩。
            logger.warning("状态字段 %s 类型异常，已使用默认值", key)
        else:
            merged[key] = value
    return merged


def load_state(path: Path) -> dict[str, Any]:
    """读取状态；文件损坏时备份并返回默认状态。"""
    path = Path(path)
    if not path.exists():
        return default_state()
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("状态根不是对象")
        return _merge_defaults(value)
    except Exception as exc:  # noqa: BLE001 - 状态损坏不应阻塞插件启动
        backup = path.with_suffix(path.suffix + f".corrupt-{int(time.time())}")
        try:
            os.replace(path, backup)
            logger.warning("状态文件损坏，已备份到 %s: %s", backup, exc)
        except OSError:
            logger.exception("状态文件损坏且备份失败: %s", exc)
        return default_state()


def save_state(path: Path, state: dict[str, Any]) -> None:
    """原子写入状态文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    os.replace(temp, path)


def prune_seen_videos(state: dict[str, Any], *, now_ts: float, ttl_days: int) -> int:
    """清理超过 TTL 的已看视频，返回删除数量。"""
    ttl_seconds = max(0, int(ttl_days)) * 86400
    seen = state.get("seen_videos")
    if not isinstance(seen, dict):
        state["seen_videos"] = {}
        return 0
    cutoff = float(now_ts) - ttl_seconds
    before = len(seen)
    state["seen_videos"] = {
        bvid: stamp for bvid, stamp in seen.items() if isinstance(stamp, (int, float)) and stamp >= cutoff
    }
    return before - len(state["seen_videos"])


def is_video_seen(state: dict[str, Any], bvid: str, *, now_ts: float, ttl_days: int) -> bool:
    seen = state.get("seen_videos")
    if not isinstance(seen, dict):
        return False
    stamp = seen.get(str(bvid))
    if not isinstance(stamp, (int, float)):
        return False
    return float(stamp) >= float(now_ts) - max(0, int(ttl_days)) * 86400


def mark_videos_seen(state: dict[str, Any], bvids: Iterable[str], *, now_ts: float) -> None:
    seen = state.setdefault("seen_videos", {})
    if not isinstance(seen, dict):
        seen = {}
        state["seen_videos"] = seen
    for bvid in bvids:
        text = str(bvid or "").strip()
        if text:
            seen[text] = now_ts


def prune_seen_threads(state: dict[str, Any], *, now_ts: float, ttl_days: int) -> int:
    """清理超过 TTL 的已看贴吧帖子，返回删除数量。"""
    ttl_seconds = max(0, int(ttl_days)) * 86400
    seen = state.get("seen_threads")
    if not isinstance(seen, dict):
        state["seen_threads"] = {}
        return 0
    cutoff = float(now_ts) - ttl_seconds
    before = len(seen)
    state["seen_threads"] = {
        tid: stamp for tid, stamp in seen.items() if isinstance(stamp, (int, float)) and stamp >= cutoff
    }
    return before - len(state["seen_threads"])


def is_thread_seen(state: dict[str, Any], tid: str, *, now_ts: float, ttl_days: int) -> bool:
    seen = state.get("seen_threads")
    if not isinstance(seen, dict):
        return False
    stamp = seen.get(str(tid))
    if not isinstance(stamp, (int, float)):
        return False
    return float(stamp) >= float(now_ts) - max(0, int(ttl_days)) * 86400


def mark_threads_seen(state: dict[str, Any], tids: Iterable[str], *, now_ts: float) -> None:
    seen = state.setdefault("seen_threads", {})
    if not isinstance(seen, dict):
        seen = {}
        state["seen_threads"] = seen
    for tid in tids:
        text = str(tid or "").strip()
        if text:
            seen[text] = now_ts


def imported_count(state: dict[str, Any]) -> int:
    imported = state.get("imported")
    return len(imported) if isinstance(imported, dict) else 0


def imported_since(state: dict[str, Any], since_ts: float) -> int:
    """统计某个时间点之后导入的条数（用于每日配额）。"""
    imported = state.get("imported")
    if not isinstance(imported, dict):
        return 0
    return sum(
        1
        for item in imported.values()
        if isinstance(item, dict) and float(item.get("at") or 0) >= float(since_ts)
    )


def record_imported(
    state: dict[str, Any],
    *,
    expression_id: Any,
    situation: str,
    style: str,
    now_ts: float,
) -> None:
    imported = state.setdefault("imported", {})
    if not isinstance(imported, dict):
        imported = {}
        state["imported"] = imported
    imported[str(expression_id)] = {
        "situation": situation,
        "style": style,
        "at": now_ts,
    }


def record_history(state: dict[str, Any], entry: dict[str, Any]) -> None:
    history = state.setdefault("history", [])
    if not isinstance(history, list):
        history = []
        state["history"] = history
    history.append(entry)
    state["history"] = history[-MAX_HISTORY:]


def bump_stats(state: dict[str, Any], **increments: int) -> None:
    stats = state.setdefault("stats", {})
    if not isinstance(stats, dict):
        stats = {}
        state["stats"] = stats
    for key, value in increments.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        stats[key] = int(stats.get(key, 0) or 0) + int(value)
