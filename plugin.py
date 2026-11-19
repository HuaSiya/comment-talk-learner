"""B站 + 贴吧学说话插件。

每天从 B站热门/指定来源的评论区取样，抽取可复用的中文说话方式，
去重过滤后写入麦麦原生的 expressions 表（session_id=NULL 为全局表达）。

命令
    /学说话              立即跑一轮并自动入库
    /学说话预览          跑一轮但不写库，只看候选
    /学说话状态          查看今日运行、配额、下次时间
    /学说话库            查看本插件最近导入的表达
    /学说话回滚 确认     仅 admin_users 可用，删除本插件导入的记录
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from datetime import datetime
from typing import Any, Iterable, Literal

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase
from pydantic import model_validator

from .bili import BiliClient, BiliError, merge_video_lists
from .dedup import is_bad_style, is_duplicate
from .extract import (
    build_extract_prompt,
    build_review_prompt,
    normalize_candidate,
    parse_json_payload,
    parse_review_payload,
    prepare_comments,
)
from .schedule import SHANGHAI, day_key, format_dt, next_run_at, should_run
from .state import (
    bump_stats,
    imported_count,
    imported_since,
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
from .tieba import TiebaClient, TiebaError, select_threads

logger = logging.getLogger("bilibili_comment_learner")

# 旧版把这些 B站字段放在 [plugin]；现在迁到 [sources]（B站来源）。
_LEGACY_BILI_FIELDS = (
    "max_videos_per_run",
    "max_comments_per_video",
    "include_sub_replies",
    "sub_reply_roots",
    "sub_replies_per_root",
    "min_reply_count",
    "min_comment_chars",
    "max_comment_chars",
    "rate_limit_seconds",
    "request_timeout",
    "max_request_retries",
    "sessdata",
)

# WebUI 中文字段名（SDK 默认用字段名当 label）
_CN_FIELD_LABELS = {
    "config_version": "配置版本",
    "enabled": "启用插件",
    "write_to_db": "写入表达库",
    "daily_enabled": "每日自动学习",
    "daily_hour": "每日开始小时",
    "daily_minute": "每日开始分钟",
    "jitter_minutes": "随机抖动（分钟）",
    "run_on_start_if_missed": "启动时补跑",
    "max_expressions_per_day": "每日新增上限",
    "max_total_imported": "累计导入上限",
    "min_confidence": "最低置信度",
    "review_enabled": "启用二次复核",
    "review_min_score": "复核最低分",
    "llm_task": "分析模型任务",
    "llm_fallback_task": "备用模型任务",
    "llm_batch_size": "每次分析条数",
    "llm_timeout_seconds": "模型超时（秒）",
    "max_candidates_per_call": "每次候选上限",
    "seen_video_ttl_days": "来源去重天数",
    "max_retries_per_day": "每日重试次数",
    "retry_interval_minutes": "重试间隔（分钟）",
    "command_cooldown_seconds": "命令冷却（秒）",
    "banned_words": "额外屏蔽词",
    "admin_users": "回滚管理员",
    "popular_enabled": "启用热门来源",
    "popular_pages": "热门取几页",
    "zone_rids": "分区排行 ID",
    "keywords": "关键词",
    "up_uids": "指定 UP 主 UID",
    "bvids": "指定 BV 号",
    "per_source_limit": "每个来源候选数",
    "max_video_age_days": "视频最大天数",
    "max_videos_per_run": "每轮视频上限",
    "max_comments_per_video": "每视频有效评论数",
    "include_sub_replies": "抓取楼中楼",
    "sub_reply_roots": "展开楼中楼根数",
    "sub_replies_per_root": "每条根评论回复数",
    "min_reply_count": "视频评论数下限",
    "min_comment_chars": "评论最短字数",
    "max_comment_chars": "评论最长字数",
    "formulaic_filter": "严格过滤公式化评论",
    "reaction_keep_ratio": "纯反应保留比例",
    "ai_bot_threshold": "AI/Bot 判定分数",
    "rate_limit_seconds": "请求间隔（秒）",
    "request_timeout": "请求超时（秒）",
    "max_request_retries": "请求重试次数",
    "sessdata": "B站 SESSDATA",
    "bars": "贴吧列表",
    "max_bars": "每轮最多吧数",
    "scan_pages": "吧帖扫描页数",
    "max_threads": "达标帖子总数",
    "threads_per_bar": "每吧达标帖子数",
    "thread_pages": "每帖楼层页数",
    "min_replies": "帖子回复数下限",
    "max_posts_per_thread": "每帖有效楼层数",
    "quality_filter": "严格过滤公式化楼层",
    "min_post_chars": "楼层最短字数",
    "max_post_chars": "楼层最长字数",
    "include_op": "包含楼主首楼",
    "exclude_title": "排除标题正则",
    "cookie": "贴吧 Cookie",
    "proxy": "贴吧代理",
    "transport": "贴吧抓取通道",
}

_CN_FIELD_DESCRIPTIONS = {
    "enabled": "关闭后所有命令和定时任务都会停止。",    "write_to_db": "开启后学习结果写入表达方式库；关闭只预览，不写库。",
    "daily_enabled": "启用每天定时学习；关闭后仍可用 /学说话 手动触发。",
    "daily_hour": "每天几点开始自动学习（北京时间，0-23）。",
    "daily_minute": "每天几分开始自动学习（0-59）。",
    "jitter_minutes": "在计划时间后随机延迟多少分钟，避免固定时间请求。",
    "run_on_start_if_missed": "启动时如果已过当天计划时间且今天没成功，是否补跑一次。",
    "max_expressions_per_day": "每天最多写入多少条新表达，定时和手动轮次累计计算；0 表示不限制，可填任意正整数。",
    "max_total_imported": "本插件累计最多写入多少条，达到后停止自动写入。",
    "min_confidence": "模型给候选的置信度低于该值就丢弃。",
    "review_enabled": "抽取后再让模型判断一次是否可复用、是否冒犯。",
    "review_min_score": "二次复核分数低于该值就丢弃。",
    "llm_task": "直接复用麦麦已有的宿主模型任务（如 learner/utils/planner），不再单独填模型名。",
    "llm_fallback_task": "主任务输出为空或不是合法 JSON 时，改用哪个宿主模型任务重试。",
    "llm_batch_size": "每次请求模型分析多少条评论/楼层；越大越省请求，但越容易输出截断。",
    "llm_timeout_seconds": "单次模型调用最长等待多少秒；模型慢或评论多时调大，避免被 RPC 默认 30 秒超时打断。",
    "max_candidates_per_call": "每次模型调用最多返回多少条候选。",
    "seen_video_ttl_days": "同一 B站视频/贴吧帖子多少天内不重复读取。",
    "max_retries_per_day": "当天失败后最多自动重试几次。",
    "retry_interval_minutes": "失败后等待多少分钟再重试。",
    "command_cooldown_seconds": "手动命令的冷却时间，0 表示不限制。",
    "banned_words": "命中这些词的评论/楼层会被过滤，不会进入学习。",
    "admin_users": "允许执行 /学说话回滚 的用户 ID；留空禁用回滚。",
    "popular_enabled": "从 B站全站热门视频的评论区取样。",
    "popular_pages": "热门列表取几页，每页约 20 个视频。",
    "max_videos_per_run": "每轮最多保留几个达标视频；某个视频有效评论不足时会跳过并继续找下一个。",
    "max_comments_per_video": "过滤后每个视频至少要保留多少条有效评论；不足则跳过该视频，达标视频最多保留这么多条。",
    "include_sub_replies": "同时读取热门评论下的楼中楼，样本更接近日常接话。",
    "sub_reply_roots": "每个视频最多展开几条热门评论的楼中楼。",
    "sub_replies_per_root": "每条热门评论最多取多少条楼中楼回复。",
    "min_reply_count": "视频评论数必须大于该值才取样；设为 50 表示 51 条及以上才参与。",
    "formulaic_filter": "过滤B站评论区常见的公式化套话、捧场、打卡、玩梗、标题党、复读和无意义评论；默认开启，建议保持开启。",
    "reaction_keep_ratio": "纯反应评论（草/笑死/awsl/666/太强了等）按比例少量保留，默认 8%；0 表示全部丢弃。",
    "ai_bot_threshold": "账号特征（等级/头像/签名/用户名）加文本模板的 AI/Bot 风险分达到该值就丢弃；默认 60，调低更严格。",
    "rate_limit_seconds": "B站请求间隔，太小容易触发风控。",
    "request_timeout": "单次 B站请求的超时时间。",
    "max_request_retries": "B站请求遇到网络错误/5xx 时的重试次数。",
    "sessdata": "可选 B站登录 Cookie，建议小号；环境变量 BILI_SESSDATA 优先。",
    "bars": "要学习的贴吧，按列表顺序处理，名称会自动补「吧」。",
    "max_bars": "每轮最多处理列表里的前几个吧，超出的本轮忽略。",
    "quality_filter": "过滤贴吧楼层里的公式化套话、玩梗、广告、AI 元讨论和无意义内容；默认开启。",
    "threads_per_bar": "每个吧最多保留几个达标帖子；楼层不足的帖子会跳过并继续找下一个。",
    "max_threads": "本轮最多保留几个达标帖子；0 表示由每吧达标帖子数 × 吧数决定。",
    "min_replies": "帖子回复数必须大于该值才读；设为 50 表示 51 条及以上才参与。",
    "max_posts_per_thread": "过滤后每个帖子至少要保留多少条有效楼层；不足则跳过该帖。建议比帖子回复数下限低 5-10 层，否则过滤后很难达标。",
    "transport": "auto=客户端接口失败自动切移动网页；web 需要 Cookie。",
    "cookie": "贴吧登录 Cookie（BAIDUID/BDUSS/STOKEN），建议小号。",
}

_SECTION_FIELD_LABELS = {
    ("sources", "keywords"): "B站关键词",
    ("sources", "rate_limit_seconds"): "B站请求间隔（秒）",
    ("sources", "request_timeout"): "B站请求超时（秒）",
    ("sources", "max_request_retries"): "B站请求重试次数",
    ("tieba", "enabled"): "启用贴吧来源",
    ("tieba", "keywords"): "贴吧关键词",
    ("tieba", "rate_limit_seconds"): "贴吧请求间隔（秒）",
    ("tieba", "request_timeout"): "贴吧请求超时（秒）",
    ("tieba", "max_request_retries"): "贴吧请求重试次数",
}

_SECTION_FIELD_DESCRIPTIONS = {
    ("sources", "keywords"): "只搜索标题/内容包含这些关键词的 B站视频。",
    ("sources", "rate_limit_seconds"): "B站请求间隔，太小容易触发风控。",
    ("sources", "request_timeout"): "单次 B站请求的超时时间。",
    ("sources", "max_request_retries"): "B站请求遇到网络错误/5xx 时的重试次数。",
    ("tieba", "enabled"): "关闭后只从 B站 取样，不再读取贴吧。",
    ("tieba", "keywords"): "可选：只读标题/摘要包含这些词的帖子；留空表示不过滤。",
    ("tieba", "rate_limit_seconds"): "贴吧请求间隔，建议不低于 1.5 秒。",
    ("tieba", "request_timeout"): "单次贴吧请求的超时时间。",
    ("tieba", "max_request_retries"): "贴吧请求遇到网络错误/5xx/110001 时的重试次数。",
}

_TASK_OPTIONS = ["learner", "utils", "planner", "replyer", "memory", "fast_model", "emoji"]
_TASK_OPTION_LABELS = {
    "learner": "学习任务（learner）",
    "utils": "通用小任务（utils）",
    "planner": "规划任务（planner）",
    "replyer": "回复任务（replyer）",
    "memory": "记忆任务（memory）",
    "fast_model": "快速任务（fast_model）",
    "emoji": "表情任务（emoji）",
}
_TASK_OPTION_DESCRIPTIONS = {
    "learner": "默认推荐；学习类请求，模型配置为空时会回退到 utils。",
    "utils": "适合抽取、整理等通用小任务，通常速度较快。",
    "planner": "复用麦麦规划模型，能力更强但可能更慢/更贵。",
    "replyer": "复用麦麦回复模型，适合需要语气判断的抽取。",
    "memory": "复用记忆任务模型，适合长文本理解。",
    "fast_model": "复用快速任务模型；未配置时回退到 utils。",
    "emoji": "复用表情任务模型；一般不建议用于长文本抽取。",
}


def _localize_config_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """给 SDK 生成的 Schema 补中文字段名、内联说明和宿主模型任务下拉项。"""
    for section_name, section in (schema.get("sections") or {}).items():
        for name, field in (section.get("fields") or {}).items():
            label = _SECTION_FIELD_LABELS.get((section_name, name)) or _CN_FIELD_LABELS.get(name)
            if label:
                field["label"] = label
            description = (
                _SECTION_FIELD_DESCRIPTIONS.get((section_name, name))
                or _CN_FIELD_DESCRIPTIONS.get(name)
                or str(field.get("description") or "").strip()
            )
            if description:
                field["description"] = description
                field["hint"] = description
            field["x-description-display"] = "inline"
            if name in {"llm_task", "llm_fallback_task"}:
                field["choices"] = list(_TASK_OPTIONS)
                field["options"] = list(_TASK_OPTIONS)
                field["ui_type"] = "select"
                field["x-option-labels"] = dict(_TASK_OPTION_LABELS)
                field["x-option-descriptions"] = dict(_TASK_OPTION_DESCRIPTIONS)
    return schema


class PluginSection(PluginConfigBase):
    """[plugin] 段：公共运行参数。"""

    __ui_label__ = "插件"
    __ui_icon__ = "message-circle"
    __ui_order__ = 0

    config_version: str = Field(
        default="1.0.0",
        description="配置结构版本，升级插件时用于迁移，一般不要改。",
        json_schema_extra={"disabled": True, "hidden": True},
    )
    enabled: bool = Field(
        default=True,
        description="总开关。关闭后命令不响应，定时任务也不运行。",
        json_schema_extra={"x-widget": "switch"},
    )
    write_to_db: bool = Field(
        default=True,
        description="学习结果是否写入麦麦的表达方式库。关闭则只预览，不污染库。",
        json_schema_extra={"x-widget": "switch"},
    )
    daily_enabled: bool = Field(
        default=True,
        description="启用每日自动学习。关闭后仍可用 /学说话 手动触发。",
        json_schema_extra={"x-widget": "switch"},
    )
    daily_hour: int = Field(default=4, ge=0, le=23, description="每天几点开始（北京时间，0-23）。")
    daily_minute: int = Field(default=30, ge=0, le=59, description="每天几分开始（0-59）。")
    jitter_minutes: int = Field(
        default=20,
        ge=0,
        le=180,
        description="每日运行随机抖动分钟数，避免固定时间请求。",
    )
    run_on_start_if_missed: bool = Field(
        default=True,
        description="启动时如果已过当天计划时间且今天没成功，是否补跑一次。",
        json_schema_extra={"x-widget": "switch"},
    )
    max_expressions_per_day: int = Field(
        default=8,
        ge=0,
        description="每天最多新增多少条表达（跨轮累计）；0 表示不限制，也可填任意正整数。",
    )
    max_total_imported: int = Field(
        default=800,
        ge=0,
        description="本插件累计最多导入多少条；达到后停止自动写入。",
    )
    min_confidence: float = Field(
        default=0.6,
        ge=0,
        le=1,
        description="低于该置信度的候选不入库。",
    )
    review_enabled: bool = Field(
        default=True,
        description="抽取后再让模型复核一遍，过滤不可复用/冒犯/广告内容。",
        json_schema_extra={"x-widget": "switch"},
    )
    review_min_score: float = Field(
        default=0.65,
        ge=0,
        le=1,
        description="复核分数低于该值则丢弃。",
    )
    llm_task: str = Field(
        default="learner",
        description="选择麦麦已有的模型任务（如 learner/utils/planner），插件直接复用该任务的模型配置，不再单独填模型名。",
    )
    llm_fallback_task: str = Field(
        default="utils",
        description="主任务输出为空或不是合法 JSON 时，改用哪个宿主模型任务重试；留空关闭兜底。",
    )
    llm_batch_size: int = Field(
        default=40,
        ge=10,
        le=200,
        description="每次模型调用喂多少条评论；太大容易触发输出截断。",
    )
    llm_timeout_seconds: int = Field(
        default=180,
        ge=30,
        le=600,
        description="单次模型调用最长等待时间；模型较慢或单批内容较多时调大，超时会切换/重试。",
    )
    max_candidates_per_call: int = Field(
        default=4,
        ge=1,
        le=15,
        description="每次模型调用最多返回多少条候选。",
    )
    seen_video_ttl_days: int = Field(
        default=14,
        ge=1,
        le=365,
        description="同一个 B站视频/贴吧帖子多少天内不重复取样。",
    )
    max_retries_per_day: int = Field(
        default=2,
        ge=0,
        le=10,
        description="每天失败后最多重试几次。",
    )
    retry_interval_minutes: int = Field(
        default=30,
        ge=5,
        le=360,
        description="失败后隔多少分钟重试。",
    )
    command_cooldown_seconds: int = Field(
        default=300,
        ge=0,
        le=3600,
        description="手动命令的冷却秒数，0 为关闭。",
    )
    banned_words: list[str] = Field(
        default_factory=list,
        description="额外屏蔽词；命中这些词的评论不会取样。",
        json_schema_extra={"x-widget": "list"},
    )
    admin_users: list[str] = Field(
        default_factory=list,
        description="允许执行回滚的用户 ID 白名单；留空则回滚命令禁用。",
        json_schema_extra={"x-widget": "list"},
    )


class SourcesSection(PluginConfigBase):
    """[sources] 段：B站来源与抓取参数。"""

    __ui_label__ = "B站来源"
    __ui_icon__ = "video"
    __ui_order__ = 1

    popular_enabled: bool = Field(
        default=True,
        description="从全站热门视频评论区取样。",
        json_schema_extra={"x-widget": "switch"},
    )
    popular_pages: int = Field(
        default=1,
        ge=1,
        le=5,
        description="热门列表取几页（每页约 20 个视频）。",
    )
    zone_rids: list[int] = Field(
        default_factory=list,
        description="分区排行 rid：游戏=4，动画=1，生活=160，知识=36，科技=188 等。",
        json_schema_extra={"x-widget": "list"},
    )
    keywords: list[str] = Field(
        default_factory=list,
        description="按关键词搜索视频，例如“大学生日常”“职场”。",
        json_schema_extra={"x-widget": "list"},
    )
    up_uids: list[int] = Field(
        default_factory=list,
        description="指定 UP 主 UID，取最新投稿的评论区。",
        json_schema_extra={"x-widget": "list"},
    )
    bvids: list[str] = Field(
        default_factory=list,
        description="固定补充视频 BV 号，适合指定想学的评论区。",
        json_schema_extra={"x-widget": "list"},
    )
    per_source_limit: int = Field(
        default=5,
        ge=1,
        le=20,
        description="每个指定来源（UP/关键词/分区/BV）最多取几个视频候选；热门会自动多取一些作为补位池。",
    )
    max_video_age_days: int = Field(
        default=30,
        ge=0,
        le=365,
        description="关键词/UP 来源只取多少天内的视频；0 为不限。",
    )
    max_videos_per_run: int = Field(
        default=6,
        ge=1,
        le=20,
        description="每轮最多保留几个达标视频；某个视频有效评论不足时会跳过并继续找下一个。",
    )
    max_comments_per_video: int = Field(
        default=40,
        ge=10,
        le=100,
        description="过滤后每个视频至少要保留多少条有效评论；不足则跳过该视频，达标视频最多保留这么多条。",
    )
    include_sub_replies: bool = Field(
        default=True,
        description="同时抓取热门评论下的楼中楼；未登录时主评论只有 3 条左右，楼中楼更适合学接话。",
        json_schema_extra={"x-widget": "switch"},
    )
    sub_reply_roots: int = Field(
        default=3,
        ge=0,
        le=10,
        description="每个视频最多展开几条热门评论的楼中楼。",
    )
    sub_replies_per_root: int = Field(
        default=20,
        ge=0,
        le=50,
        description="每条热门评论最多取多少条楼中楼回复。",
    )
    min_reply_count: int = Field(
        default=50,
        ge=0,
        description="视频评论数必须大于该值才取样；设为 50 表示 51 条及以上才参与。",
    )
    min_comment_chars: int = Field(
        default=4,
        ge=1,
        le=20,
        description="评论短于该长度不取样。",
    )
    max_comment_chars: int = Field(
        default=120,
        ge=20,
        le=300,
        description="评论长于该长度不取样。",
    )
    formulaic_filter: bool = Field(
        default=True,
        description="过滤B站评论区常见的公式化套话、捧场、打卡、玩梗、标题党、复读和无意义评论；默认开启，建议保持开启。",
        json_schema_extra={"x-widget": "switch"},
    )
    reaction_keep_ratio: float = Field(
        default=0.08,
        ge=0,
        le=0.3,
        description="纯反应评论（草/笑死/awsl/666/太强了等）按比例少量保留，默认 8%；0 表示全部丢弃。",
    )
    ai_bot_threshold: int = Field(
        default=60,
        ge=20,
        le=100,
        description="账号特征 + 文本模板的 AI/Bot 风险分达到该值就丢弃；调低更严格。",
    )
    rate_limit_seconds: float = Field(
        default=1.5,
        ge=0.5,
        le=10.0,
        description="B站请求间隔秒数，太小容易触发风控。",
    )
    request_timeout: float = Field(
        default=20.0,
        ge=5.0,
        le=60.0,
        description="单次 B站请求超时秒数。",
    )
    max_request_retries: int = Field(
        default=2,
        ge=0,
        le=5,
        description="单个 B站请求遇到网络错误/5xx 时的重试次数。",
    )
    sessdata: str = Field(
        default="",
        description="可选 B站 SESSDATA（建议小号）。环境变量 BILI_SESSDATA 优先。",
        json_schema_extra={"x-widget": "password"},
    )


class TiebaSection(PluginConfigBase):
    """[tieba] 段：从百度贴吧取样。"""

    __ui_label__ = "贴吧来源"
    __ui_icon__ = "message-square"
    __ui_order__ = 2

    enabled: bool = Field(
        default=True,
        description="启用贴吧来源。关闭后只从 B站 取样。",
        json_schema_extra={"x-widget": "switch"},
    )
    quality_filter: bool = Field(
        default=True,
        description="过滤贴吧楼层里的公式化套话、玩梗、广告、AI 元讨论和无意义内容；默认开启。",
        json_schema_extra={"x-widget": "switch"},
    )
    bars: list[str] = Field(
        default_factory=lambda: ["生活吧"],
        description="要扫哪些吧，例如 [\"生活吧\", \"大学生吧\"]；会自动补“吧”。",
        json_schema_extra={"x-widget": "list"},
    )
    max_bars: int = Field(
        default=10,
        ge=1,
        le=20,
        description="每轮最多处理 bars 里的前几个吧；超出的吧本轮忽略。",
    )
    keywords: list[str] = Field(
        default_factory=list,
        description="可选：只读标题/摘要包含这些词的帖子；空列表表示不过滤。",
        json_schema_extra={"x-widget": "list"},
    )
    scan_pages: int = Field(
        default=2,
        ge=1,
        le=5,
        description="每个吧扫前几页帖子列表（每页约 30 帖）。",
    )
    max_threads: int = Field(
        default=0,
        ge=0,
        le=50,
        description="本轮最多保留几个达标帖子；0 表示由 threads_per_bar × 吧数决定。",
    )
    threads_per_bar: int = Field(
        default=2,
        ge=0,
        le=10,
        description="每个吧最多保留几个达标帖子；楼层不足的帖子会跳过并继续找下一个。",
    )
    thread_pages: int = Field(
        default=1,
        ge=1,
        le=5,
        description="每个帖子最多翻几页楼层（每页约 30 楼）；系统会按目标有效楼层数自动多翻 1-2 页作为过滤缓冲。",
    )
    min_replies: int = Field(
        default=50,
        ge=0,
        description="帖子回复数必须大于该值才读；设为 50 表示 51 条及以上才参与。",
    )
    max_posts_per_thread: int = Field(
        default=30,
        ge=5,
        le=100,
        description="过滤后每个帖子至少要保留多少条有效楼层；不足则跳过该帖。建议比帖子回复数下限低 5-10 层，否则过滤后很难达标。",
    )
    min_post_chars: int = Field(
        default=6,
        ge=2,
        le=30,
        description="楼层文本短于该长度不取样。",
    )
    max_post_chars: int = Field(
        default=300,
        ge=50,
        le=800,
        description="楼层文本长于该长度不取样。",
    )
    include_op: bool = Field(
        default=False,
        description="是否学习楼主首楼；默认只学回复楼层，更像日常接话。",
        json_schema_extra={"x-widget": "switch"},
    )
    exclude_title: str = Field(
        default="水楼|签到|吧规|公告|广告|交易|招聘|投诉|申精|回复",
        description="标题/摘要命中该正则的帖子跳过。",
        json_schema_extra={"x-widget": "textarea", "rows": 2},
    )
    rate_limit_seconds: float = Field(
        default=2.0,
        ge=0.5,
        le=10.0,
        description="贴吧请求间隔秒数，建议不低于 1.5。",
    )
    request_timeout: float = Field(
        default=20.0,
        ge=5.0,
        le=60.0,
        description="单次贴吧请求超时秒数。",
    )
    max_request_retries: int = Field(
        default=2,
        ge=0,
        le=5,
        description="贴吧请求遇到网络错误/5xx/110001 时的重试次数。",
    )
    cookie: str = Field(
        default="",
        description="可选贴吧 Cookie（BAIDUID/BDUSS/STOKEN）；触发百度安全验证时再填，建议小号。",
        json_schema_extra={"x-widget": "password"},
    )
    proxy: str = Field(
        default="",
        description="可选 HTTP 代理，例如 http://127.0.0.1:7890；数据中心 IP 被贴吧风控时使用。",
    )
    transport: Literal["auto", "client", "web"] = Field(
        default="auto",
        description="贴吧抓取通道：auto=客户端接口失败自动切移动网页；client=只用客户端接口；web=只用移动网页（需要 cookie）。",
        json_schema_extra={"x-widget": "select"},
    )


class CommentLearnerConfig(PluginConfigBase):
    """插件配置根。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    sources: SourcesSection = Field(default_factory=SourcesSection)
    tieba: TiebaSection = Field(default_factory=TiebaSection)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_bili_fields(cls, data: Any) -> Any:
        """把旧版放在 [plugin] 里的 B站字段迁到 [sources]，避免升级后配置失效。"""
        if not isinstance(data, dict):
            return data
        plugin = data.get("plugin")
        if not isinstance(plugin, dict):
            return data
        sources = data.get("sources")
        if not isinstance(sources, dict):
            sources = {}
        plugin = dict(plugin)
        sources = dict(sources)
        changed = False
        for key in _LEGACY_BILI_FIELDS:
            if key not in plugin:
                continue
            if key not in sources:
                sources[key] = plugin[key]
            plugin.pop(key)
            changed = True
        if "llm_model" in plugin:
            plugin.pop("llm_model", None)
            changed = True
        if not changed:
            return data
        migrated = dict(data)
        migrated["plugin"] = plugin
        migrated["sources"] = sources
        return migrated


class BilibiliCommentLearner(MaiBotPlugin):
    """B站 + 贴吧学说话。"""

    @classmethod
    def get_config_model(cls) -> type[PluginConfigBase] | None:
        return CommentLearnerConfig

    @classmethod
    def build_config_schema(cls, **kwargs: Any) -> dict[str, Any]:
        """在 SDK 生成的 Schema 上补中文字段名和宿主模型任务下拉项。"""
        return _localize_config_schema(super().build_config_schema(**kwargs))

    # ---------------- 生命周期 ----------------

    async def on_load(self) -> None:
        self._lock = asyncio.Lock()
        self._run_task: asyncio.Task[Any] | None = None
        self._scheduler_task: asyncio.Task[Any] | None = None
        self._cooldowns: dict[tuple[str, str], float] = {}
        self._startup_check = True
        data_dir = self.ctx.paths.data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = data_dir / "daily_state.json"
        self._state = load_state(self._state_path)
        self._scheduler_task = asyncio.create_task(self._scheduler_loop(), name="bili-comment-learner")
        logger.info("B站+贴吧学说话插件已加载，状态文件=%s", self._state_path)

    async def on_unload(self) -> None:
        for task in (self._run_task, self._scheduler_task):
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        logger.info("B站+贴吧学说话插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        logger.info("B站+贴吧学说话配置已更新: scope=%s version=%s", scope, version)

    # ---------------- 调度 ----------------

    async def _scheduler_loop(self) -> None:
        await asyncio.sleep(20)
        while True:
            try:
                cfg = self.config.plugin
                now = datetime.now(SHANGHAI)
                run, reason = should_run(now, self._state, cfg, startup=self._startup_check)
                self._startup_check = False
                if reason == "startup_missed":
                    self._state["skip_date"] = day_key(now)
                    self._save_state()
                    logger.info("今天已错过计划时间，按配置跳过；下次=%s", format_dt(next_run_at(now, self._state, cfg)))
                elif run:
                    logger.info("触发每日学习：%s", reason)
                    result = await self._run_guarded(trigger=reason)
                    if result.get("status") == "failed":
                        logger.warning("每日学习失败：%s", result.get("error"))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("每日学习调度异常，稍后重试")
            await asyncio.sleep(60)

    def _save_state(self) -> None:
        try:
            save_state(self._state_path, self._state)
        except Exception:
            logger.exception("保存状态文件失败")

    # ---------------- 运行入口 ----------------

    async def _run_guarded(self, *, trigger: str, dry_run: bool = False) -> dict[str, Any]:
        if self._lock.locked():
            return {"status": "busy", "trigger": trigger, "error": "已有学习任务在运行"}
        async with self._lock:
            cfg = self.config.plugin
            preview_only = bool(dry_run or not cfg.write_to_db)
            now = datetime.now(SHANGHAI)
            now_ts = now.timestamp()
            today = day_key(now)
            if not preview_only:
                day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                today_imported = imported_since(self._state, day_start)
                remaining_total = int(cfg.max_total_imported) - imported_count(self._state)
                max_daily = int(cfg.max_expressions_per_day)
                if max_daily > 0 and today_imported >= max_daily:
                    logger.info(
                        "跳过学习：今日配额已用完（%s/%s）", today_imported, cfg.max_expressions_per_day
                    )
                    return {
                        "status": "quota_exhausted",
                        "trigger": trigger,
                        "dry_run": False,
                        "error": "",
                        "videos": 0,
                        "bili_videos": 0,
                        "tieba_threads": 0,
                        "comments": 0,
                        "candidates": 0,
                        "imported": [],
                        "skipped": 0,
                        "failed_writes": 0,
                        "warnings": [
                            f"今日配额已用完（{today_imported}/{cfg.max_expressions_per_day}），本轮没有执行"
                        ],
                        "preview": [],
                        "elapsed": 0.0,
                    }
                if remaining_total <= 0:
                    logger.info("跳过学习：累计导入上限已达到（%s 条）", cfg.max_total_imported)
                    return {
                        "status": "limit_reached",
                        "trigger": trigger,
                        "dry_run": False,
                        "error": "",
                        "videos": 0,
                        "bili_videos": 0,
                        "tieba_threads": 0,
                        "comments": 0,
                        "candidates": 0,
                        "imported": [],
                        "skipped": 0,
                        "failed_writes": 0,
                        "warnings": [f"累计导入上限已达到（{cfg.max_total_imported} 条），本轮没有执行"],
                        "preview": [],
                        "elapsed": 0.0,
                    }
                if self._state.get("last_attempt_date") != today:
                    self._state["failures_today"] = 0
                    self._state["next_retry_at"] = 0
                self._state["last_attempt_date"] = today
                self._state["last_attempt_at"] = now_ts

            summary: dict[str, Any]
            try:
                result = await self._run_once(cfg, dry_run=preview_only)
                imported = result.get("imported") or []
                failed_writes = int(result.get("failed_writes") or 0)
                status = "partial" if failed_writes else "success"
                summary = {
                    "status": status,
                    "trigger": trigger,
                    "dry_run": preview_only,
                    "error": "",
                    **result,
                }
                for warning in result.get("warnings") or []:
                    logger.warning("学习警告：%s", warning)
                if not preview_only:
                    self._state["last_success_date"] = today
                    self._state["last_success_at"] = now_ts
                    self._state["last_status"] = status
                    self._state["last_error"] = ""
                    self._state["failures_today"] = 0
                    self._state["next_retry_at"] = 0
                    bump_stats(
                        self._state,
                        runs=1,
                        videos=int(result.get("videos") or 0),
                        tieba_threads=int(result.get("tieba_threads") or 0),
                        comments=int(result.get("comments") or 0),
                        candidates=int(result.get("candidates") or 0),
                        imported=len(imported),
                        failed_writes=failed_writes,
                    )
                    record_history(
                        self._state,
                        {
                            "at": now_ts,
                            "trigger": trigger,
                            "status": status,
                            "videos": int(result.get("videos") or 0),
                            "tieba_threads": int(result.get("tieba_threads") or 0),
                            "comments": int(result.get("comments") or 0),
                            "candidates": int(result.get("candidates") or 0),
                            "imported": len(imported),
                            "failed_writes": failed_writes,
                        },
                    )
            except asyncio.CancelledError:
                if not preview_only:
                    self._state["last_status"] = "cancelled"
                    self._state["last_error"] = "插件卸载或重启中断"
                    self._state["next_retry_at"] = now_ts + 600
                    self._save_state()
                raise
            except Exception as exc:  # noqa: BLE001 - 单轮失败不拖垮插件
                logger.exception("学习轮次失败")
                summary = {
                    "status": "failed",
                    "trigger": trigger,
                    "dry_run": preview_only,
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                    "videos": 0,
                    "bili_videos": 0,
                    "tieba_threads": 0,
                    "comments": 0,
                    "candidates": 0,
                    "imported": [],
                    "skipped": 0,
                    "failed_writes": 0,
                    "warnings": [],
                    "preview": [],
                    "elapsed": 0.0,
                }
                if not preview_only:
                    self._state["last_status"] = "failed"
                    self._state["last_error"] = summary["error"]
                    self._state["failures_today"] = int(self._state.get("failures_today") or 0) + 1
                    self._state["next_retry_at"] = now_ts + int(cfg.retry_interval_minutes) * 60
                    bump_stats(self._state, runs=1)
                    record_history(
                        self._state,
                        {
                            "at": now_ts,
                            "trigger": trigger,
                            "status": "failed",
                            "error": summary["error"][:200],
                        },
                    )
            finally:
                if not preview_only:
                    self._save_state()
            return summary

    async def _run_once(self, cfg: PluginSection, *, dry_run: bool) -> dict[str, Any]:
        started = time.monotonic()
        warnings: list[str] = []
        now_ts = time.time()
        prune_seen_videos(self._state, now_ts=now_ts, ttl_days=cfg.seen_video_ttl_days)
        prune_seen_threads(self._state, now_ts=now_ts, ttl_days=cfg.seen_video_ttl_days)

        comments: list[dict[str, Any]] = []
        comments_by_id: dict[int, str] = {}
        processed_videos: list[str] = []
        processed_threads: list[str] = []
        bili_videos_count = 0
        next_id = 1

        if self._bilibili_enabled():
            bili_cfg = self.config.sources
            client = self._build_client(bili_cfg)
            videos, discover_warnings = await self._discover_videos(client, bili_cfg)
            warnings.extend(discover_warnings)
            if videos:
                bili_comments, bili_by_id, processed_videos, next_id, comment_warnings = await self._collect_comments(
                    client, videos, bili_cfg, start_id=next_id
                )
                bili_videos_count = len(processed_videos)
                warnings.extend(comment_warnings)
                comments.extend(bili_comments)
                comments_by_id.update(bili_by_id)
            else:
                warnings.append("B站没有取到可用视频")
        else:
            warnings.append("B站来源未启用")

        tieba_cfg = self.config.tieba
        if tieba_cfg.enabled:
            if tieba_cfg.bars:
                tieba_client = self._build_tieba_client(tieba_cfg)
                tieba_comments, tieba_by_id, processed_threads, next_id, tieba_warnings = await self._collect_tieba(
                    tieba_client, tieba_cfg, start_id=next_id
                )
                warnings.extend(tieba_warnings)
                comments.extend(tieba_comments)
                comments_by_id.update(tieba_by_id)
            else:
                warnings.append("贴吧来源已启用但 bars 为空")
        else:
            warnings.append("贴吧来源未启用")

        if not comments:
            raise RuntimeError(
                "B站和贴吧都没有取到可用文本（检查来源配置、过滤条件、每视频有效评论数或风控）"
            )

        # 高质量素材优先喂给模型：有个人经历/细节/观点的评论排前面。
        comments.sort(
            key=lambda item: (int(item.get("quality") or 0), int(item.get("like") or 0)),
            reverse=True,
        )

        candidates, extract_warnings = await self._extract_candidates(comments, comments_by_id, cfg)
        warnings.extend(extract_warnings)
        if not candidates:
            if not dry_run:
                mark_videos_seen(self._state, processed_videos, now_ts=now_ts)
                mark_threads_seen(self._state, processed_threads, now_ts=now_ts)
            return {
                "videos": bili_videos_count,
                "bili_videos": bili_videos_count,
                "tieba_threads": len(processed_threads),
                "comments": len(comments),
                "candidates": 0,
                "imported": [],
                "skipped": 0,
                "failed_writes": 0,
                "warnings": warnings,
                "preview": [],
                "elapsed": round(time.monotonic() - started, 1),
            }

        if cfg.review_enabled:
            candidates, review_warnings = await self._review_candidates(candidates, cfg)
            warnings.extend(review_warnings)

        existing = await self._load_global_expressions()
        selected, skipped, quota_warning = self._dedup_and_quota(candidates, existing, cfg)
        if quota_warning:
            warnings.append(quota_warning)

        imported: list[dict[str, Any]] = []
        failed_writes = 0
        if not dry_run and cfg.write_to_db:
            imported, failed_writes, write_warnings = await self._write_candidates(selected, cfg)
            warnings.extend(write_warnings)
            mark_videos_seen(self._state, processed_videos, now_ts=now_ts)
            mark_threads_seen(self._state, processed_threads, now_ts=now_ts)

        return {
            "videos": bili_videos_count,
            "bili_videos": bili_videos_count,
            "tieba_threads": len(processed_threads),
            "comments": len(comments),
            "candidates": len(candidates),
            "imported": imported,
            "skipped": skipped,
            "failed_writes": failed_writes,
            "warnings": warnings,
            "preview": selected if dry_run else [],
            "elapsed": round(time.monotonic() - started, 1),
        }

    def _bilibili_enabled(self) -> bool:
        sources = self.config.sources
        return bool(
            sources.popular_enabled
            or sources.zone_rids
            or sources.keywords
            or sources.up_uids
            or sources.bvids
        )

    def _build_client(self, cfg: SourcesSection) -> BiliClient:
        sessdata = (os.environ.get("BILI_SESSDATA") or cfg.sessdata or "").strip()
        return BiliClient(
            timeout=cfg.request_timeout,
            rate_limit=cfg.rate_limit_seconds,
            sessdata=sessdata,
            max_retries=cfg.max_request_retries,
        )

    # ---------------- 来源发现 ----------------

    async def _discover_videos(
        self, client: BiliClient, cfg: SourcesSection
    ) -> tuple[list[dict[str, Any]], list[str]]:
        sources = self.config.sources
        warnings: list[str] = []
        groups: list[list[dict[str, Any]]] = []
        now_ts = time.time()
        target_videos = max(1, int(cfg.max_videos_per_run))
        # 达标视频可能少于候选视频，热门列表多取一些作为补位池。
        pool_limit = max(target_videos * 3, target_videos + 5)

        def filtered(
            raw: Iterable[dict[str, Any]], source: str, *, limit: int | None = None
        ) -> list[dict[str, Any]]:
            result: list[dict[str, Any]] = []
            for video in raw:
                bvid = str(video.get("bvid") or "").strip()
                if not bvid:
                    continue
                if is_video_seen(
                    self._state,
                    bvid,
                    now_ts=now_ts,
                    ttl_days=self.config.plugin.seen_video_ttl_days,
                ):
                    continue
                if not self._reply_count_ok(video.get("reply"), cfg.min_reply_count):
                    continue
                if source.startswith(("search:", "up:")) and not self._age_ok(
                    int(video.get("pubdate") or 0), sources.max_video_age_days, now_ts
                ):
                    continue
                title = str(video.get("title") or "")
                if any(
                    word.lower() in title.lower()
                    for word in (self.config.plugin.banned_words or [])
                    if word.strip()
                ):
                    continue
                result.append(video)
            return result[: max(1, int(limit or sources.per_source_limit))]

        if sources.bvids:
            group: list[dict[str, Any]] = []
            for bvid in sources.bvids[: max(1, sources.per_source_limit)]:
                try:
                    info = await asyncio.to_thread(client.video_info, bvid)
                    group.append(info)
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"BV {bvid} 读取失败：{str(exc)[:80]}")
            groups.append(filtered(group, "view"))

        for uid in sources.up_uids[:10]:
            try:
                raw = await asyncio.to_thread(
                    client.up_uploads, int(uid), limit=max(5, sources.per_source_limit * 3)
                )
                groups.append(filtered(raw, f"up:{uid}"))
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"UP {uid} 读取失败：{str(exc)[:80]}")

        for keyword in sources.keywords[:10]:
            try:
                raw = await asyncio.to_thread(
                    client.search_videos, keyword, limit=max(5, sources.per_source_limit * 3)
                )
                groups.append(filtered(raw, f"search:{keyword}"))
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"关键词「{keyword}」搜索失败：{str(exc)[:80]}")

        for rid in sources.zone_rids[:10]:
            try:
                raw = await asyncio.to_thread(client.ranking, int(rid))
                groups.append(filtered(raw, f"ranking:{rid}"))
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"分区 {rid} 排行读取失败：{str(exc)[:80]}")

        if sources.popular_enabled:
            try:
                popular_pages = max(
                    int(sources.popular_pages), math.ceil(pool_limit / 20)
                )
                raw = await asyncio.to_thread(client.popular, pages=popular_pages)
                groups.append(
                    filtered(
                        raw,
                        "popular",
                        limit=max(int(sources.per_source_limit), pool_limit),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"热门列表读取失败：{str(exc)[:80]}")

        merged = merge_video_lists(groups, limit=pool_limit)
        return merged, warnings

    @staticmethod
    def _reply_count_ok(reply_count: Any, min_reply_count: int) -> bool:
        """视频评论数必须严格大于下限；50 表示只要 51 条及以上。"""
        try:
            count = int(reply_count or 0)
        except (TypeError, ValueError):
            count = 0
        return count > max(0, int(min_reply_count))

    @staticmethod
    def _age_ok(pubdate: int, max_age_days: int, now_ts: float) -> bool:
        if max_age_days <= 0 or pubdate <= 0:
            return True
        return pubdate >= now_ts - max_age_days * 86400

    # ---------------- 评论抓取 ----------------

    async def _collect_comments(
        self,
        client: BiliClient,
        videos: list[dict[str, Any]],
        cfg: SourcesSection,
        *,
        start_id: int = 1,
    ) -> tuple[list[dict[str, Any]], dict[int, str], list[str], int, list[str]]:
        comments: list[dict[str, Any]] = []
        comments_by_id: dict[int, str] = {}
        processed: list[str] = []
        warnings: list[str] = []
        seen_texts: set[str] = set()
        seen_norms: list[str] = []
        blocked_users: set[str] = set()
        filter_stats: dict[str, int] = {}
        next_id = int(start_id)
        target_videos = max(1, int(cfg.max_videos_per_run))
        target_comments = max(1, int(cfg.max_comments_per_video))
        pages = max(1, math.ceil(target_comments / 20))

        for video in videos:
            if len(processed) >= target_videos:
                break
            bvid = str(video.get("bvid") or "").strip()
            if not bvid:
                continue
            try:
                if not video.get("aid") or not video.get("reply"):
                    video = await asyncio.to_thread(client.video_info, bvid)
                aid = int(video.get("aid") or 0)
                if aid <= 0:
                    warnings.append(f"{bvid} 缺少 aid，跳过")
                    continue
                raw_comments = await asyncio.to_thread(
                    client.comments,
                    aid,
                    pages=pages,
                    page_size=20,
                    include_sub_replies=cfg.include_sub_replies,
                    sub_reply_roots=cfg.sub_reply_roots,
                    sub_replies_per_root=cfg.sub_replies_per_root,
                    owner_mid=int(video.get("owner_mid") or 0),
                )
                before_seen = set(seen_texts)
                before_norm_count = len(seen_norms)
                before_blocked = set(blocked_users)
                prepared, _next = prepare_comments(
                    raw_comments,
                    source=bvid,
                    start_id=next_id,
                    min_chars=cfg.min_comment_chars,
                    max_chars=cfg.max_comment_chars,
                    banned_words=self.config.plugin.banned_words,
                    seen_keys=seen_texts,
                    formulaic_filter=cfg.formulaic_filter,
                    reaction_keep_ratio=cfg.reaction_keep_ratio,
                    ai_bot_threshold=cfg.ai_bot_threshold,
                    near_dedup=cfg.formulaic_filter,
                    seen_norms=seen_norms,
                    blocked_users=blocked_users,
                    filter_stats=filter_stats,
                )
                prepared.sort(
                    key=lambda item: (
                        int(item.get("quality") or 0),
                        int(item.get("like") or 0),
                    ),
                    reverse=True,
                )
                prepared = prepared[:target_comments]
                if len(prepared) < target_comments:
                    # 这个视频不达标，不应把它的评论/去重记录带进后面的视频。
                    seen_texts.clear()
                    seen_texts.update(before_seen)
                    del seen_norms[before_norm_count:]
                    blocked_users.clear()
                    blocked_users.update(before_blocked)
                    warnings.append(
                        f"{bvid} 有效评论 {len(prepared)}/{target_comments}，跳过并补下一个"
                    )
                    continue
                next_id = max(int(item["id"]) for item in prepared) + 1
                for item in prepared:
                    comments.append(item)
                    comments_by_id[int(item["id"])] = str(item["text"])
                processed.append(bvid)
            except BiliError as exc:
                warnings.append(f"{bvid} 评论读取失败：{str(exc)[:100]}")
                if "风控" in str(exc):
                    break
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{bvid} 处理失败：{type(exc).__name__}: {str(exc)[:100]}")
        formulaic_dropped = sum(
            count
            for reason, count in filter_stats.items()
            if reason.startswith(
                (
                    "公式化",
                    "B站套话",
                    "纯捧场",
                    "泛化",
                    "纯语气词",
                    "纯反应",
                    "打卡/前排",
                    "无意义",
                    "梗词",
                    "纯数字",
                    "只有标点",
                    "近似复读",
                    "链式过滤",
                    "中文占比",
                    "广告/引流",
                    "辱骂/隐私",
                    "AI元讨论",
                    "AI/Bot",
                    "Bot账号",
                )
            )
        )
        if formulaic_dropped:
            warnings.append(f"B站质量过滤 {formulaic_dropped} 条（公式化/广告/AI/纯反应/复读等）")
        if len(processed) < target_videos:
            warnings.append(
                f"B站达标视频 {len(processed)}/{target_videos}（每视频需 {target_comments} 条有效评论）"
            )
        return comments, comments_by_id, processed, next_id, warnings

    # ---------------- 贴吧抓取 ----------------

    def _build_tieba_client(self, cfg: TiebaSection) -> TiebaClient:
        return TiebaClient(
            timeout=cfg.request_timeout,
            rate_limit=cfg.rate_limit_seconds,
            cookie=cfg.cookie,
            proxy=cfg.proxy,
            transport=cfg.transport,
            max_retries=cfg.max_request_retries,
        )

    async def _collect_tieba(
        self,
        client: TiebaClient,
        cfg: TiebaSection,
        *,
        start_id: int = 1,
    ) -> tuple[list[dict[str, Any]], dict[int, str], list[str], int, list[str]]:
        """扫吧帖列表 → 选帖 → 拉楼层 → 清洗成文本样本。"""
        comments: list[dict[str, Any]] = []
        comments_by_id: dict[int, str] = {}
        processed: list[str] = []
        warnings: list[str] = []
        seen_texts: set[str] = set()
        filter_stats: dict[str, int] = {}
        seen_tids = {
            str(tid)
            for tid in (self._state.get("seen_threads") or {})
            if str(tid)
        }
        next_id = int(start_id)
        bars = [str(item).strip() for item in cfg.bars if str(item).strip()][: max(1, int(cfg.max_bars))]
        per_bar = max(0, int(cfg.threads_per_bar))
        total_target = max(0, int(cfg.max_threads))
        if total_target <= 0:
            total_target = per_bar * len(bars) if per_bar > 0 else 4
        target_posts = max(1, int(cfg.max_posts_per_thread))
        if target_posts >= max(1, int(cfg.min_replies)):
            warnings.append(
                f"每帖有效楼层目标 {target_posts} ≥ 帖子回复数下限 {cfg.min_replies}，"
                "过滤后可能很难达标；建议目标比下限低 5-10 层"
            )
        # 目标 30 层时过滤损耗较大，至少翻 3 页；更大目标继续增加，最多 5 页。
        buffer_pages = 2 if target_posts >= 30 else 1
        thread_pages = max(
            int(cfg.thread_pages),
            min(5, math.ceil(target_posts / 30) + buffer_pages),
        )
        if per_bar > 0 and total_target < per_bar * len(bars):
            warnings.append(
                f"贴吧每吧目标 {per_bar} × {len(bars)} 个吧超过总目标 {total_target}，后面的吧可能取不到"
            )
        if total_target > 12:
            warnings.append(
                f"本轮目标 {total_target} 个达标帖子（每帖需 {target_posts} 条有效楼层），请求和模型调用较多；可用 max_threads 限制"
            )

        for bar in bars:
            remaining = total_target - len(processed)
            if remaining <= 0:
                break
            bar_limit = remaining if per_bar <= 0 else min(per_bar, remaining)
            if bar_limit <= 0:
                break
            try:
                threads = await asyncio.to_thread(
                    client.bar_threads,
                    bar,
                    pages=cfg.scan_pages,
                    limit=30,
                )
            except TiebaError as exc:
                warnings.append(f"贴吧「{bar}」读取失败：{str(exc)[:100]}")
                continue
            selected = select_threads(
                threads,
                seen=seen_tids,
                min_replies=cfg.min_replies,
                max_threads=max(bar_limit * 6, bar_limit + 6),
                keywords=cfg.keywords,
                exclude_pattern=cfg.exclude_title,
                bar=bar,
            )
            bar_processed = 0
            for thread in selected:
                if bar_processed >= bar_limit or len(processed) >= total_target:
                    break
                tid = str(thread.get("tid") or "").strip()
                if not tid:
                    continue
                before_seen = set(seen_texts)
                try:
                    data = await asyncio.to_thread(
                        client.thread, tid, pages=thread_pages, limit=30
                    )
                    posts = data.get("posts") or []
                    raw_posts = []
                    for post in posts:
                        if not isinstance(post, dict):
                            continue
                        if not cfg.include_op and post.get("is_op"):
                            continue
                        raw_posts.append(
                            {
                                "message": post.get("content") or "",
                                "like": post.get("agree") or 0,
                                "user": post.get("author") or "",
                            }
                        )
                    prepared, _next = prepare_comments(
                        raw_posts,
                        source=f"tieba:{tid}",
                        start_id=next_id,
                        min_chars=cfg.min_post_chars,
                        max_chars=cfg.max_post_chars,
                        banned_words=self.config.plugin.banned_words,
                        seen_keys=seen_texts,
                        formulaic_filter=cfg.quality_filter,
                        reaction_keep_ratio=0.08,
                        ai_bot_threshold=60,
                        filter_stats=filter_stats,
                    )
                    prepared.sort(
                        key=lambda item: (
                            int(item.get("quality") or 0),
                            int(item.get("like") or 0),
                        ),
                        reverse=True,
                    )
                    prepared = prepared[:target_posts]
                    if len(prepared) < target_posts:
                        seen_texts.clear()
                        seen_texts.update(before_seen)
                        warnings.append(
                            f"贴吧帖子 {tid} 有效楼层 {len(prepared)}/{target_posts}，跳过并补下一个"
                        )
                        continue
                    seen_tids.add(tid)
                    processed.append(tid)
                    bar_processed += 1
                    next_id = max(int(item["id"]) for item in prepared) + 1
                    for item in prepared:
                        comments.append(item)
                        comments_by_id[int(item["id"])] = str(item["text"])
                except TiebaError as exc:
                    warnings.append(f"贴吧帖子 {tid} 读取失败：{str(exc)[:100]}")
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"贴吧帖子 {tid} 处理失败：{type(exc).__name__}: {str(exc)[:100]}")
        tieba_filtered = sum(filter_stats.values())
        if tieba_filtered:
            warnings.append(
                f"贴吧质量过滤 {tieba_filtered} 条（长度/公式化/AI/广告等）"
            )
        if len(processed) < total_target:
            warnings.append(
                f"贴吧达标帖子 {len(processed)}/{total_target}（每帖需 {target_posts} 条有效楼层）"
            )
        if not processed:
            warnings.append("贴吧没有取到达标帖子（检查 bars、min_replies、max_posts_per_thread 或风控）")
        return comments, comments_by_id, processed, next_id, warnings

    # ---------------- 模型抽取/复核 ----------------

    async def _llm_generate(
        self,
        prompt: str,
        *,
        task_name: str,
        max_tokens: int,
        temperature: float,
        timeout_ms: int,
    ) -> str:
        payload = await self.ctx.call_capability(
            "llm.generate",
            timeout_ms=timeout_ms,
            prompt=prompt,
            task_name=task_name,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if not isinstance(payload, dict) or not payload.get("success"):
            error = payload.get("error") if isinstance(payload, dict) else payload
            raise RuntimeError(f"模型调用失败：{str(error)[:160]}")
        return str(payload.get("response") or "")

    @staticmethod
    def _candidate_list(payload: Any) -> list[Any] | None:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("candidates", "results", "data", "expressions", "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        return None

    async def _extract_candidates(
        self,
        comments: list[dict[str, Any]],
        comments_by_id: dict[int, str],
        cfg: PluginSection,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        candidates: list[dict[str, Any]] = []
        warnings: list[str] = []
        batch_size = max(10, int(cfg.llm_batch_size))
        valid_batches = 0
        for start in range(0, len(comments), batch_size):
            batch = comments[start : start + batch_size]
            prompt = build_extract_prompt(batch, max_candidates=cfg.max_candidates_per_call)
            raw = ""
            payload = None
            try:
                raw = await self._llm_generate(
                    prompt,
                    task_name=cfg.llm_task,
                    max_tokens=6000,
                    temperature=0.4,
                    timeout_ms=int(cfg.llm_timeout_seconds * 1000),
                )
                payload = self._candidate_list(parse_json_payload(raw))
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"抽取模型失败：{str(exc)[:120]}")
            if payload is None and cfg.llm_fallback_task and cfg.llm_fallback_task != cfg.llm_task:
                warnings.append("主任务失败或输出不是 JSON，换兜底任务重试")
                try:
                    raw = await self._llm_generate(
                        prompt,
                        task_name=cfg.llm_fallback_task,
                        max_tokens=5000,
                        temperature=0.3,
                        timeout_ms=int(cfg.llm_timeout_seconds * 1000),
                    )
                    payload = self._candidate_list(parse_json_payload(raw))
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"兜底任务失败：{str(exc)[:120]}")
            if payload is None and len(batch) > 20:
                # 超时或输出跑偏时，缩小批次再试一次。
                warnings.append("抽取失败，缩小批次重试")
                retry_batch = batch[:20]
                try:
                    raw = await self._llm_generate(
                        build_extract_prompt(retry_batch, max_candidates=3),
                        task_name=cfg.llm_fallback_task or cfg.llm_task,
                        max_tokens=4000,
                        temperature=0.3,
                        timeout_ms=int(cfg.llm_timeout_seconds * 1000),
                    )
                    payload = self._candidate_list(parse_json_payload(raw))
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"抽取重试失败：{str(exc)[:120]}")
                    continue
            if payload is None:
                logger.info("模型输出不是 JSON 数组：%s", raw[:300])
                warnings.append("模型没有返回 JSON 数组")
                continue
            valid_batches += 1
            batch_candidates = 0
            reasons: list[str] = []
            for item in payload[: cfg.max_candidates_per_call]:
                candidate, reason = normalize_candidate(
                    item,
                    comments_by_id=comments_by_id,
                    min_confidence=cfg.min_confidence,
                    max_situation_chars=30,
                    max_style_chars=60,
                    reject_taboo_strong=True,
                )
                if candidate:
                    candidates.append(candidate)
                    batch_candidates += 1
                elif reason:
                    reasons.append(reason)
                    logger.debug("候选被丢弃：%s", reason)
            if batch_candidates == 0:
                logger.info(
                    "抽取批次没有有效候选：原因=%s；输出片段=%s",
                    "；".join(reasons[:3]) or "无",
                    raw[:200],
                )
        if valid_batches == 0:
            detail = "；".join(warnings[:3]) if warnings else "模型输出无法解析"
            raise RuntimeError(f"模型没有返回任何有效候选（{detail}）")
        return candidates, warnings

    async def _review_candidates(
        self, candidates: list[dict[str, Any]], cfg: PluginSection
    ) -> tuple[list[dict[str, Any]], list[str]]:
        warnings: list[str] = []
        reviewed: list[dict[str, Any]] = []
        chunk_size = 20
        for start in range(0, len(candidates), chunk_size):
            chunk = candidates[start : start + chunk_size]
            try:
                raw = await self._llm_generate(
                    build_review_prompt(chunk),
                    task_name=cfg.llm_task,
                    max_tokens=3000,
                    temperature=0.2,
                    timeout_ms=int(cfg.llm_timeout_seconds * 1000),
                )
                parsed = parse_review_payload(raw, len(chunk))
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"复核模型失败：{str(exc)[:120]}")
                parsed = None
            if parsed is None:
                fallback_threshold = max(cfg.min_confidence, 0.75)
                kept = [item for item in chunk if float(item.get("confidence") or 0) >= fallback_threshold]
                warnings.append(
                    f"复核结果无效，按置信度 {fallback_threshold:.2f} 兜底保留 {len(kept)}/{len(chunk)} 条"
                )
                reviewed.extend(kept)
                continue
            for index, item in enumerate(chunk):
                result = parsed.get(index)
                if result is None:
                    if float(item.get("confidence") or 0) >= max(cfg.min_confidence, 0.75):
                        reviewed.append(item)
                    continue
                if not result.get("keep") or float(result.get("score") or 0) < cfg.review_min_score:
                    logger.debug(
                        "复核丢弃：%s → %s（%s）",
                        item.get("situation"),
                        item.get("style"),
                        result.get("reason"),
                    )
                    continue
                enriched = dict(item)
                enriched["review_score"] = result.get("score")
                enriched["review_reason"] = result.get("reason", "")
                reviewed.append(enriched)
        return reviewed, warnings

    # ---------------- 去重/配额/写库 ----------------

    async def _load_global_expressions(self) -> list[dict[str, Any]]:
        try:
            result = await self._database_query(
                model_name="Expression",
                query_type="get",
                filters={"session_id": None},
                limit=5000,
            )
            return self._rows_of(result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取现有表达失败，本轮只做轮内去重：%s", exc)
            return []

    def _dedup_and_quota(
        self,
        candidates: list[dict[str, Any]],
        existing: list[dict[str, Any]],
        cfg: PluginSection,
    ) -> tuple[list[dict[str, Any]], int, str]:
        remaining_total = int(cfg.max_total_imported) - imported_count(self._state)
        if remaining_total <= 0:
            return [], len(candidates), f"已达到累计导入上限 {cfg.max_total_imported} 条"
        day_start = datetime.now(SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        today_imported = imported_since(self._state, day_start)
        max_daily = int(cfg.max_expressions_per_day)
        if max_daily > 0:
            remaining_daily = max_daily - today_imported
            if remaining_daily <= 0:
                return [], len(candidates), f"今日配额已用完（{today_imported}/{max_daily}）"
            limit = min(remaining_daily, remaining_total)
        else:
            limit = remaining_total
        selected: list[dict[str, Any]] = []
        skipped = 0
        for candidate in candidates:
            if len(selected) >= limit:
                skipped += 1
                continue
            bad, reason = is_bad_style(str(candidate.get("style") or ""), max_chars=60)
            if bad:
                skipped += 1
                logger.debug("候选被丢弃：%s", reason)
                continue
            duplicate, reason = is_duplicate(
                str(candidate.get("situation") or ""),
                str(candidate.get("style") or ""),
                [*existing, *selected],
            )
            if duplicate:
                skipped += 1
                logger.debug("候选重复：%s", reason)
                continue
            selected.append(candidate)
        message = ""
        if len(selected) >= limit and len(candidates) > len(selected) + skipped:
            message = f"本轮候选超过配额，按 {limit} 条截断"
        return selected, skipped, message

    async def _write_candidates(
        self, candidates: list[dict[str, Any]], cfg: PluginSection
    ) -> tuple[list[dict[str, Any]], int, list[str]]:
        imported: list[dict[str, Any]] = []
        failed = 0
        warnings: list[str] = []
        for candidate in candidates:
            record = {
                "situation": candidate["situation"],
                "style": candidate["style"],
                "content_list": json.dumps([candidate["situation"]], ensure_ascii=False),
                "count": 1,
                "session_id": None,
                "checked": False,
            }
            try:
                result = await self._database_query(
                    model_name="Expression", query_type="create", data=record
                )
                created = self._created_record(result)
                if not created or created.get("id") is None:
                    failed += 1
                    warnings.append(f"写入失败：{candidate['style'][:40]}")
                    continue
                expression_id = created["id"]
                imported.append(
                    {
                        "id": expression_id,
                        "situation": candidate["situation"],
                        "style": candidate["style"],
                    }
                )
                record_imported(
                    self._state,
                    expression_id=expression_id,
                    situation=candidate["situation"],
                    style=candidate["style"],
                    now_ts=time.time(),
                )
                self._save_state()
            except Exception as exc:  # noqa: BLE001
                failed += 1
                warnings.append(f"写库异常：{type(exc).__name__}: {str(exc)[:100]}")
        return imported, failed, warnings

    # ---------------- 数据库能力 ----------------

    async def _database_query(self, **args: Any) -> Any:
        result = await self.ctx.call_host_method(
            "cap.call", payload={"capability": "database.query", "args": args}
        )
        self._check_db_result(result)
        if isinstance(result, dict) and "success" in result and "result" in result:
            return result["result"]
        return result

    @staticmethod
    def _check_db_result(result: Any) -> None:
        if result is False or (isinstance(result, dict) and result.get("success") is False):
            detail = str(result.get("error") or "请检查宿主日志")[:160] if isinstance(result, dict) else "请检查宿主日志"
            raise RuntimeError(f"数据库操作失败：{detail}")

    @staticmethod
    def _rows_of(result: Any) -> list[dict[str, Any]]:
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        if not isinstance(result, dict):
            return []
        for key in ("data", "result", "rows", "items"):
            value = result.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                for nested_key in ("data", "rows", "items"):
                    nested = value.get(nested_key)
                    if isinstance(nested, list):
                        return [item for item in nested if isinstance(item, dict)]
                return [value]
        return []

    @staticmethod
    def _created_record(result: Any) -> dict[str, Any] | None:
        if isinstance(result, dict):
            if result.get("id") is not None:
                return result
            for key in ("data", "result"):
                value = result.get(key)
                if isinstance(value, dict) and value.get("id") is not None:
                    return value
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    return value[0]
        if isinstance(result, list) and result and isinstance(result[0], dict):
            return result[0]
        return None

    # ---------------- 命令 ----------------

    @Command("学说话", description="立即从B站/贴吧学习一轮", pattern=r"^/学说话\s*$", timeout_ms=15000)
    async def handle_learn(self, **kwargs: Any) -> Any:
        cfg = self.config.plugin
        if not cfg.enabled:
            return None
        blocked = self._cooldown_reply(kwargs)
        if blocked:
            return await self._finish(kwargs, blocked)
        if self._run_task and not self._run_task.done():
            return await self._finish(kwargs, "⏳ 上一轮还在学习，请等它结束。")
        stream_id = str(kwargs.get("stream_id") or "")
        self._run_task = asyncio.create_task(self._manual_run(stream_id, dry_run=False))
        return await self._finish(kwargs, "🔍 开始从 B站/贴吧学说话，完成后把结果发到这里。")

    @Command("学说话预览", description="预览B站/贴吧学习候选，不写入表达库", pattern=r"^/学说话预览\s*$", timeout_ms=15000)
    async def handle_preview(self, **kwargs: Any) -> Any:
        cfg = self.config.plugin
        if not cfg.enabled:
            return None
        blocked = self._cooldown_reply(kwargs)
        if blocked:
            return await self._finish(kwargs, blocked)
        if self._run_task and not self._run_task.done():
            return await self._finish(kwargs, "⏳ 上一轮还在学习，请等它结束。")
        stream_id = str(kwargs.get("stream_id") or "")
        self._run_task = asyncio.create_task(self._manual_run(stream_id, dry_run=True))
        return await self._finish(kwargs, "👀 开始预览，不会写入表达库，完成后发结果。")

    @Command("学说话状态", description="查看B站/贴吧学习状态", pattern=r"^/学说话状态\s*$")
    async def handle_status(self, **kwargs: Any) -> Any:
        cfg = self.config.plugin
        if not cfg.enabled:
            return None
        return await self._finish(kwargs, self._status_text())

    @Command("学说话库", description="查看本插件最近导入的表达", pattern=r"^/学说话库\s*$")
    async def handle_library(self, **kwargs: Any) -> Any:
        cfg = self.config.plugin
        if not cfg.enabled:
            return None
        return await self._finish(kwargs, self._library_text())

    @Command(
        "学说话回滚",
        description="删除本插件导入的表达（仅管理员）",
        pattern=r"^/学说话回滚\s*(?P<confirm>.*)$",
        timeout_ms=60000,
    )
    async def handle_rollback(self, confirm: str = "", **kwargs: Any) -> Any:
        cfg = self.config.plugin
        if not cfg.enabled:
            return None
        text = await self._rollback(str(confirm or ""), kwargs)
        return await self._finish(kwargs, text)

    # ---------------- 命令辅助 ----------------

    def _cooldown_reply(self, kwargs: dict[str, Any]) -> str:
        cfg = self.config.plugin
        seconds = int(cfg.command_cooldown_seconds)
        if seconds <= 0:
            return ""
        now = time.monotonic()
        key = (
            str(kwargs.get("platform") or ""),
            str(kwargs.get("user_id") or kwargs.get("stream_id") or "anonymous"),
        )
        self._cooldowns = {item: expiry for item, expiry in self._cooldowns.items() if expiry > now}
        remaining = self._cooldowns.get(key, 0) - now
        if remaining > 0:
            return f"⏳ 冷却中，{int(remaining) + 1} 秒后再试。"
        self._cooldowns[key] = now + seconds
        return ""

    async def _manual_run(self, stream_id: str, *, dry_run: bool) -> None:
        try:
            result = await self._run_guarded(trigger="manual_preview" if dry_run else "manual", dry_run=dry_run)
            await self._notify(
                stream_id,
                self._format_summary(result, dry_run=bool(result.get("dry_run", dry_run))),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("手动学习失败")
            await self._notify(stream_id, f"❌ 学习失败：{type(exc).__name__}: {exc}")

    async def _notify(self, stream_id: str, text: str) -> None:
        if not stream_id:
            return
        try:
            await self.ctx.call_capability(
                "send.text",
                text=text,
                stream_id=stream_id,
                sync_to_maisaka_history=False,
            )
        except Exception:  # noqa: BLE001
            logger.exception("发送学习结果失败")

    async def _finish(self, kwargs: dict[str, Any], text: str) -> tuple[bool, str, bool]:
        stream_id = str(kwargs.get("stream_id") or "")
        if not stream_id:
            return True, text, False
        try:
            result = await self.ctx.call_capability(
                "send.text",
                text=text,
                stream_id=stream_id,
                sync_to_maisaka_history=False,
            )
        except Exception:  # noqa: BLE001
            logger.exception("发送命令回复失败")
            return True, text, False
        if result is False or (isinstance(result, dict) and result.get("success") is False):
            return True, text, False
        return True, text, True

    def _format_summary(self, result: dict[str, Any], *, dry_run: bool) -> str:
        status = str(result.get("status") or "")
        if status == "busy":
            return "⏳ 已有学习任务在运行，请稍后再试。"
        if status in {"quota_exhausted", "limit_reached"}:
            detail = (result.get("warnings") or [result.get("error") or ""])[0]
            return f"⏸️ {detail}。明天额度会自动重置，或在「插件」里调大每日新增上限。"
        if status == "failed":
            return f"❌ 本轮学习失败：{str(result.get('error') or '未知错误')[:300]}"

        head = "👀 预览完成（未写入）" if dry_run else ("✅ 学习完成" if status == "success" else "⚠️ 学习部分完成")
        lines = [
            head,
            f"B站 {result.get('bili_videos', result.get('videos', 0))} 视频｜"
            f"贴吧 {result.get('tieba_threads', 0)} 帖｜样本 {result.get('comments', 0)}｜"
            f"候选 {result.get('candidates', 0)}｜写入 {len(result.get('imported') or [])}｜"
            f"跳过 {result.get('skipped', 0)}｜耗时 {result.get('elapsed', 0)}s",
        ]
        if dry_run:
            preview = result.get("preview") or []
            for index, item in enumerate(preview[:8], 1):
                lines.append(
                    f"{index}. {item.get('situation')} → {item.get('style')}"
                    f"（{float(item.get('confidence') or 0):.2f}）"
                )
                examples = item.get("examples") or []
                if examples:
                    lines.append(f"   例：{str(examples[0])[:60]}")
            if not preview:
                lines.append("这轮没有通过筛选的新候选。")
        else:
            imported = result.get("imported") or []
            for item in imported[:10]:
                lines.append(f"• {item.get('situation')} → {item.get('style')}")
            if not imported:
                lines.append("这轮没有新增表达（候选都被配额、去重或复核过滤）。")
        warnings = result.get("warnings") or []
        if warnings:
            ordered = sorted(
                warnings,
                key=lambda item: 0
                if ("配额" in str(item) or "上限" in str(item) or "达标视频" in str(item))
                else 1,
            )
            lines.append("⚠️ " + "；".join(str(item)[:80] for item in ordered[:3]))
        text = "\n".join(lines)
        return text[:1800]

    def _status_text(self) -> str:
        cfg = self.config.plugin
        state = self._state
        now = datetime.now(SHANGHAI)
        today = day_key(now)
        imported_total = imported_count(state)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        today_imported = imported_since(state, day_start)
        lines = [
            "📊 B站+贴吧学说话",
            f"开关：{'开' if cfg.enabled else '关'}｜自动：{'开' if cfg.daily_enabled else '关'}｜"
            f"写库：{'开' if cfg.write_to_db else '预览'}",
            f"B站过滤：{'开' if self.config.sources.formulaic_filter else '关'}"
            f"｜纯反应 {float(self.config.sources.reaction_keep_ratio):.0%}"
            f"｜AI/Bot≥{int(self.config.sources.ai_bot_threshold)}分",
            f"B站目标：{int(self.config.sources.max_videos_per_run)} 个达标视频"
            f" × 每视频 {int(self.config.sources.max_comments_per_video)} 条有效评论",
            f"今日：{today}｜状态：{state.get('last_status') or '暂无'}｜"
            f"失败次数：{state.get('failures_today') or 0}",
        ]
        tieba_cfg = self.config.tieba
        tieba_bars = min(len(tieba_cfg.bars), int(tieba_cfg.max_bars))
        tieba_target = int(tieba_cfg.max_threads) or (
            int(tieba_cfg.threads_per_bar) * tieba_bars
            if int(tieba_cfg.threads_per_bar) > 0
            else 4
        )
        lines.append(
            f"贴吧目标：{tieba_target} 个达标帖子"
            f" × 每帖 {int(tieba_cfg.max_posts_per_thread)} 条有效楼层"
        )
        if cfg.daily_enabled:
            lines.append(f"下次计划：{format_dt(next_run_at(now, state, cfg))}")
        daily_limit = int(cfg.max_expressions_per_day)
        daily_label = "不限" if daily_limit <= 0 else str(daily_limit)
        lines.append(
            f"配额：今日 {today_imported}/{daily_label}｜"
            f"累计 {imported_total}/{cfg.max_total_imported}"
        )
        lines.append(
            f"统计：运行 {int((state.get('stats') or {}).get('runs') or 0)} 次｜"
            f"已看 B站视频 {len(state.get('seen_videos') or {})} 个｜"
            f"已看贴吧帖子 {len(state.get('seen_threads') or {})} 个｜"
            f"失败写入 {int((state.get('stats') or {}).get('failed_writes') or 0)} 条"
        )
        if state.get("last_error"):
            lines.append(f"最近错误：{str(state.get('last_error'))[:180]}")
        if not cfg.admin_users:
            lines.append("回滚：未配置 admin_users，已禁用")
        return "\n".join(lines)

    def _library_text(self) -> str:
        imported = self._state.get("imported")
        if not isinstance(imported, dict) or not imported:
            return "📚 还没有导入记录。先跑一次 /学说话 或 /学说话预览 看看。"
        items = [item for item in imported.items() if isinstance(item[1], dict)]
        items.sort(key=lambda pair: float(pair[1].get("at") or 0), reverse=True)
        lines = [f"📚 本插件已导入 {len(items)} 条，最近 {min(10, len(items))} 条："]
        for expression_id, item in items[:10]:
            stamp = float(item.get("at") or 0)
            when = datetime.fromtimestamp(stamp, SHANGHAI).strftime("%m-%d") if stamp else "?"
            lines.append(f"#{expression_id} [{when}] {item.get('situation')} → {item.get('style')}")
        return "\n".join(lines)

    async def _rollback(self, confirm: str, kwargs: dict[str, Any]) -> str:
        cfg = self.config.plugin
        if not cfg.admin_users:
            return "⚠️ 未配置 admin_users，回滚命令已禁用。"
        user_id = str(kwargs.get("user_id") or "").strip()
        if not user_id or user_id not in {str(item) for item in cfg.admin_users}:
            return "⛔ 只有 admin_users 中的用户可以回滚。"
        if confirm.strip() != "确认":
            return "⚠️ 这会删除本插件导入的表达，确认请发送：/学说话回滚 确认"

        imported = self._state.get("imported")
        if not isinstance(imported, dict) or not imported:
            return "没有可回滚的记录。"

        try:
            result = await self._database_query(
                model_name="Expression",
                query_type="get",
                filters={"session_id": None},
                limit=5000,
            )
            rows = {str(item.get("id")): item for item in self._rows_of(result) if item.get("id") is not None}
        except Exception as exc:  # noqa: BLE001
            return f"❌ 读取表达库失败：{str(exc)[:160]}"

        deleted = 0
        missing = 0
        kept = 0
        for expression_id, snapshot in list(imported.items()):
            row = rows.get(str(expression_id))
            if not row:
                missing += 1
                imported.pop(expression_id, None)
                continue
            same_style = str(row.get("style") or "").strip() == str(snapshot.get("style") or "").strip()
            same_situation = str(row.get("situation") or "").strip() == str(snapshot.get("situation") or "").strip()
            if not (same_style and same_situation):
                kept += 1
                continue
            try:
                removed = await self._database_query(
                    model_name="Expression", query_type="delete", filters={"id": int(expression_id)}
                )
                if removed is False or removed is None or removed == 0:
                    kept += 1
                    continue
                deleted += 1
                imported.pop(expression_id, None)
            except Exception:  # noqa: BLE001
                logger.exception("回滚表达 %s 失败", expression_id)
                kept += 1
        self._save_state()
        return (
            f"🧹 回滚完成：删除 {deleted} 条，保留 {kept} 条（人工修改或内容已变化），"
            f"库中已不存在 {missing} 条。"
        )


def create_plugin() -> BilibiliCommentLearner:
    """插件工厂。"""
    return BilibiliCommentLearner()
