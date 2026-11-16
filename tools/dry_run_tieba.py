"""只抓贴吧、不写库的调试脚本。

用法（在 MaiBot 容器里跑）：
    docker exec -i maim-bot-core python /MaiMBot/plugins/comment-talk-learner/tools/dry_run_tieba.py \
        --bar 生活吧 --threads 2 --posts 20
"""
from __future__ import annotations

import argparse
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = "comment_talk_learner"


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
    parser = argparse.ArgumentParser(description="贴吧学说话：抓取/清洗预览")
    parser.add_argument("--bar", default="生活吧", help="吧名")
    parser.add_argument("--pages", type=int, default=2, help="扫几页帖子列表")
    parser.add_argument("--threads", type=int, default=2, help="读几个帖子")
    parser.add_argument("--posts", type=int, default=20, help="每个帖子最多打印多少层")
    parser.add_argument("--rate-limit", type=float, default=None)
    parser.add_argument("--cookie", default="", help="可选贴吧 Cookie")
    parser.add_argument("--proxy", default="", help="可选 HTTP 代理")
    parser.add_argument("--config", default=str(ROOT / "config.toml"))
    args = parser.parse_args()

    load_package()
    from comment_talk_learner.extract import prepare_comments
    from comment_talk_learner.tieba import TiebaClient, TiebaError, select_threads

    config = read_config(pathlib.Path(args.config))
    tieba_cfg = config.get("tieba", {}) if isinstance(config, dict) else {}
    rate_limit = args.rate_limit if args.rate_limit is not None else float(tieba_cfg.get("rate_limit_seconds", 2.0))
    client = TiebaClient(
        rate_limit=rate_limit,
        cookie=args.cookie or str(tieba_cfg.get("cookie") or ""),
        proxy=args.proxy or str(tieba_cfg.get("proxy") or ""),
        timeout=float(tieba_cfg.get("request_timeout", 20.0)),
        max_retries=int(tieba_cfg.get("max_request_retries", 2)),
    )

    try:
        threads = client.bar_threads(args.bar, pages=max(1, args.pages), limit=30)
    except TiebaError as exc:
        print(f"贴吧读取失败：{exc}")
        if "110001" in str(exc) or "403" in str(exc):
            print("提示：当前 IP 可能被贴吧风控。可在 config.toml 的 [tieba] 里填 cookie 或 proxy 后重试。")
        return 1
    selected = select_threads(
        threads,
        seen=set(),
        min_replies=int(tieba_cfg.get("min_replies", 30)),
        max_threads=max(1, args.threads),
        keywords=tieba_cfg.get("keywords") or [],
        exclude_pattern=str(tieba_cfg.get("exclude_title") or ""),
        bar=args.bar,
    )
    print(f"吧帖 {len(threads)} 个，选中 {len(selected)} 个。")
    next_id = 1
    for thread in selected:
        print(f"\n=== {thread['tid']} | {thread['title'][:60]} | 回复 {thread['reply_num']} ===")
        try:
            data = client.thread(thread["tid"], pages=1, limit=30)
        except TiebaError as exc:
            print(f"  读取失败：{exc}")
            continue
        raw_posts = [
            {"message": post.get("content") or "", "like": post.get("agree") or 0}
            for post in data.get("posts") or []
            if not post.get("is_op")
        ]
        prepared, next_id = prepare_comments(
            raw_posts,
            source=f"tieba:{thread['tid']}",
            start_id=next_id,
            min_chars=int(tieba_cfg.get("min_post_chars", 6)),
            max_chars=int(tieba_cfg.get("max_post_chars", 300)),
            banned_words=config.get("plugin", {}).get("banned_words") or [],
        )
        prepared = prepared[: args.posts]
        for item in prepared:
            print(f"  [{item['id']}] {item['text']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
