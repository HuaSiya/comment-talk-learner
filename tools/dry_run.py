"""只抓取、不写库的调试脚本。

用法（在 MaiBot 运行环境/容器里跑，避免宿主机 Python 版本过旧）：
    python tools/dry_run.py --videos 2 --comments 20

默认取全站热门；也可以指定 BV：
    ... --bvids BV1xx411c7mD BV1yy411c7mE
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = "local_bilibili_comment_learner"


def load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载插件包")
    package = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = package
    spec.loader.exec_module(package)
    return package


def read_config(path: pathlib.Path) -> dict:
    if not path.exists():
        return {}
    try:
        import tomllib
    except ModuleNotFoundError:
        print("当前 Python 没有 tomllib，跳过配置文件；请用容器里的 Python 运行。")
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def main() -> int:
    parser = argparse.ArgumentParser(description="B站+贴吧学说话：B站抓取/清洗预览")
    parser.add_argument("--videos", type=int, default=2, help="最多处理几个热门视频")
    parser.add_argument("--comments", type=int, default=20, help="每个视频最多看多少条评论")
    parser.add_argument("--pages", type=int, default=1, help="热门列表取几页")
    parser.add_argument("--bvids", nargs="*", default=[], help="直接指定 BV 号")
    parser.add_argument("--sessdata", default="", help="可选 SESSDATA；环境变量 BILI_SESSDATA 优先")
    parser.add_argument("--rate-limit", type=float, default=None, help="请求间隔秒数，默认读配置或 1.5")
    parser.add_argument("--show-prompt", action="store_true", help="打印第一段抽取 prompt")
    parser.add_argument("--config", default=str(ROOT / "config.toml"), help="配置文件路径")
    args = parser.parse_args()

    load_package()
    from local_bilibili_comment_learner.bili import BiliClient, BiliError  # noqa: E402
    from local_bilibili_comment_learner.extract import (  # noqa: E402
        build_extract_prompt,
        prepare_comments,
    )

    config = read_config(pathlib.Path(args.config))
    plugin_cfg = config.get("plugin", {}) if isinstance(config, dict) else {}
    sources_cfg = config.get("sources", {}) if isinstance(config, dict) else {}
    rate_limit = (
        args.rate_limit
        if args.rate_limit is not None
        else float(sources_cfg.get("rate_limit_seconds", plugin_cfg.get("rate_limit_seconds", 1.5)))
    )
    sessdata = args.sessdata or str(sources_cfg.get("sessdata") or plugin_cfg.get("sessdata") or "")
    client = BiliClient(
        rate_limit=rate_limit,
        sessdata=sessdata,
        timeout=float(sources_cfg.get("request_timeout", plugin_cfg.get("request_timeout", 20.0))),
        max_retries=int(sources_cfg.get("max_request_retries", plugin_cfg.get("max_request_retries", 2))),
    )

    videos = []
    try:
        if args.bvids:
            for bvid in args.bvids[: max(1, args.videos)]:
                videos.append(client.video_info(bvid))
        else:
            videos = client.popular(pages=max(1, args.pages))[: max(1, args.videos)]
    except BiliError as exc:
        print(f"B站请求失败：{exc}")
        return 1

    if not videos:
        print("没有取到视频。")
        return 1

    comments = []
    comments_by_id = {}
    seen = set()
    seen_norms: list[str] = []
    blocked_users: set[str] = set()
    next_id = 1
    pages = max(1, (args.comments + 19) // 20)
    for video in videos:
        bvid = str(video.get("bvid") or "")
        print(f"\n=== {bvid} | {video.get('title', '')[:50]} | 评论 {video.get('reply', 0)} ===")
        try:
            info = client.video_info(bvid)
            raw = client.comments(
                int(info.get("aid") or 0),
                pages=pages,
                page_size=20,
                include_sub_replies=bool(sources_cfg.get("include_sub_replies", True)),
                sub_reply_roots=int(sources_cfg.get("sub_reply_roots", 3)),
                sub_replies_per_root=int(sources_cfg.get("sub_replies_per_root", 20)),
            )
        except BiliError as exc:
            print(f"  读取失败：{exc}")
            continue
        stats: dict[str, int] = {}
        prepared, next_id = prepare_comments(
            raw,
            source=bvid,
            start_id=next_id,
            min_chars=int(sources_cfg.get("min_comment_chars", 4)),
            max_chars=int(sources_cfg.get("max_comment_chars", 120)),
            banned_words=plugin_cfg.get("banned_words") or [],
            seen_keys=seen,
            formulaic_filter=bool(sources_cfg.get("formulaic_filter", True)),
            reaction_keep_ratio=float(sources_cfg.get("reaction_keep_ratio", 0.08)),
            ai_bot_threshold=int(sources_cfg.get("ai_bot_threshold", 60)),
            near_dedup=bool(sources_cfg.get("formulaic_filter", True)),
            seen_norms=seen_norms,
            blocked_users=blocked_users,
            filter_stats=stats,
        )
        prepared = prepared[: args.comments]
        next_id = (prepared[-1]["id"] + 1) if prepared else next_id
        if stats:
            summary = "；".join(f"{reason} {count}" for reason, count in sorted(stats.items(), key=lambda pair: -pair[1])[:5])
            print(f"  过滤统计：{summary}")
        for item in prepared:
            comments.append(item)
            comments_by_id[item["id"]] = item["text"]
            print(f"  [{item['id']}] (q={item.get('quality', 0)}) {item['text']}")
        time.sleep(min(rate_limit, 1.5))

    print(f"\n共 {len(videos)} 个视频，保留 {len(comments)} 条评论。")
    if args.show_prompt and comments:
        prompt = build_extract_prompt(comments[:80], max_candidates=6)
        print("\n--- 抽取 prompt 预览 ---")
        print(prompt[:6000])
    if comments:
        print("\n评论数据 JSON：")
        print(json.dumps([{"id": item["id"], "text": item["text"]} for item in comments[:80]], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
