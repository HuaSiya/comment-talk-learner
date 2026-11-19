"""极简贴吧只读客户端（纯标准库）。

走 ``c.tieba.baidu.com`` 的客户端 JSON 接口，免登录、按 ``tiebaclient!!!``
规则签名。只做本插件需要的吧帖列表、帖子楼层，不做网页搜索。
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Iterable

logger = logging.getLogger("bilibili_comment_learner.tieba")

CLIENT_UA = "bdtb/10.0.6.8 Android/29"
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36"
)
_CLIENT_HOST = "https://c.tieba.baidu.com"
_IMAGE_TYPES = {1, 3, 5, 8, 10, 11, 12, 13}
_SPACE_RE = re.compile(r"[ \t\u3000]+")
_NEWLINE_RE = re.compile(r"\n{3,}")

# 全进程共享的请求节流，避免多个实例同时打到贴吧。
_REQUEST_LOCK = threading.Lock()
_FETCH_LOCK = threading.Lock()
_LAST_REQUEST = 0.0


class TiebaError(RuntimeError):
    """贴吧接口异常（含风控/安全验证）。"""


def _valid_tid(tid: str) -> bool:
    return bool(tid) and tid.isdigit() and 4 <= len(tid) <= 15


def thread_url(tid: str | int) -> str:
    return f"https://tieba.baidu.com/p/{tid}"


def normalize_bar(bar: str) -> str:
    """把用户输入规范成贴吧 kw（默认补“吧”）。"""
    value = str(bar or "").strip().strip("#").strip()
    if not value:
        return ""
    if value.endswith("吧"):
        return value
    return value + "吧"


def clean_text(value: str, *, limit: int = 0) -> str:
    """清洗贴吧文本：去零宽、统一空白、保守截断。"""
    text = html.unescape(str(value or ""))
    text = text.replace("\u200b", "").replace("\xa0", " ").replace("\ufeff", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _SPACE_RE.sub(" ", text)
    text = _NEWLINE_RE.sub("\n\n", text)
    text = text.strip()
    if limit and len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _author_name(author: Any) -> str:
    if not isinstance(author, dict):
        return "匿名吧友"
    name = str(author.get("name_show") or author.get("name") or "").strip()
    return name or "匿名吧友"


def _to_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _content_text(content: Any) -> str:
    """把 content（列表/字典/字符串）转成纯文本；图片标记为 [图片]。"""
    if isinstance(content, str):
        return clean_text(content)
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if text:
            parts.append(str(text))
            continue
        if item.get("src") or item.get("bimg_url") or item.get("type") in _IMAGE_TYPES:
            parts.append("[图片]")
    return clean_text("\n".join(parts))


def _normalize_forum(forum: Any) -> dict[str, Any]:
    if not isinstance(forum, dict):
        return {}
    name = ""
    for key in ("name", "forum_name", "kw"):
        name = str(forum.get(key) or "").strip()
        if name:
            break
    return {
        "name": name,
        "id": str(forum.get("id") or forum.get("forum_id") or ""),
        "first_class": str(forum.get("first_class") or ""),
        "second_class": str(forum.get("second_class") or ""),
    }


def _thread_abstract(thread: dict[str, Any]) -> str:
    for key in ("abstract", "first_post_content"):
        value = thread.get(key)
        if value:
            text = _content_text(value)
            if text:
                return text
    return ""


def normalize_thread(raw: Any) -> dict[str, Any]:
    """把吧帖列表 / 帖子页里的 thread 对象统一成内部结构。"""
    if not isinstance(raw, dict):
        return {}
    tid = str(raw.get("id") or raw.get("tid") or "").strip()
    if not _valid_tid(tid):
        return {}
    title = clean_text(raw.get("title"), limit=180)
    abstract = _thread_abstract(raw)[:300]
    if not title:
        title = abstract[:80] or f"帖子 {tid}"
    return {
        "tid": tid,
        "title": title,
        "abstract": abstract,
        "author": _author_name(raw.get("author")),
        "reply_num": _to_int(raw.get("reply_num")),
        "view_num": _to_int(raw.get("view_num")),
        "agree_num": _to_int(raw.get("agree_num")),
        "create_time": _to_int(raw.get("create_time")),
        "last_time": _to_int(raw.get("last_time_int") or raw.get("last_time")),
        "is_good": bool(raw.get("is_good")),
        "is_top": bool(raw.get("is_top")),
        "forum": _normalize_forum(raw.get("forum_info") or raw.get("forum")),
        "url": thread_url(tid),
        "post_id": str(raw.get("post_id") or ""),
    }


def normalize_post(raw: Any, *, thread: dict[str, Any] | None = None) -> dict[str, Any]:
    """把楼层对象统一成内部结构。"""
    if not isinstance(raw, dict):
        return {}
    post_id = str(raw.get("id") or "").strip()
    floor = _to_int(raw.get("floor"))
    content = _content_text(raw.get("content"))
    if not content:
        content = clean_text(raw.get("title"))
    if not content and not post_id:
        return {}
    agree = raw.get("agree")
    if isinstance(agree, dict):
        agree_num = _to_int(agree.get("agree_num"))
    else:
        agree_num = _to_int(agree)
    is_op = floor == 1 or bool(thread and thread.get("post_id") and post_id == thread["post_id"])
    return {
        "id": post_id,
        "floor": floor,
        "time": _to_int(raw.get("time")),
        "author": _author_name(raw.get("author")),
        "agree": agree_num,
        "content": content,
        "sub_posts": _to_int(raw.get("sub_post_number")),
        "is_op": bool(is_op),
        "is_fold": bool(raw.get("is_fold")),
    }


def _strip_html(value: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", str(value or ""))
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return clean_text(html.unescape(text))


class _PostContentParser(HTMLParser):
    """提取移动网页楼层里的 ``<div class="content" lz="0|1">`` 文本。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.posts: list[tuple[str, str]] = []
        self._depth = 0
        self._lz: str | None = None
        self._buf: list[str] = []
        self._skip_until: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_map = {str(key).lower(): str(value or "") for key, value in attrs}
        if tag == "div":
            if self._lz is None:
                classes = attrs_map.get("class", "").split()
                if "content" in classes and "lz" in attrs_map:
                    self._lz = attrs_map.get("lz") or "0"
                    self._depth = 1
                    self._buf = []
                    self._skip_until = None
                return
            self._depth += 1
            ident = attrs_map.get("id", "")
            classes = attrs_map.get("class", "")
            if self._skip_until is None and (
                ident in {"pb_less_imgs", "diversBanner"}
                or "pb_less_imgs" in classes
                or "diversBanner" in classes
            ):
                self._skip_until = self._depth
            return
        if self._lz is None or self._skip_until is not None:
            return
        if tag == "br":
            self._buf.append("\n")
        elif tag in {"p", "li"}:
            self._buf.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if self._lz is not None and self._skip_until is None and tag.lower() == "br":
            self._buf.append("\n")

    def handle_data(self, data: str) -> None:
        if self._lz is not None and self._skip_until is None:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._lz is None or tag.lower() != "div":
            return
        if self._skip_until is not None and self._depth <= self._skip_until:
            self._skip_until = None
        self._depth -= 1
        if self._depth <= 0:
            text = clean_text("".join(self._buf))
            if text:
                self.posts.append((self._lz, text))
            self._lz = None
            self._buf = []
            self._skip_until = None


def parse_bar_threads_html(page: str, bar: str) -> list[dict[str, Any]]:
    """解析移动网页吧帖列表。"""
    threads: list[dict[str, Any]] = []
    pattern = re.compile(r'<li\s+class="([^"]*)"[^>]*data-tid="(\d+)"[^>]*>(.*?)</li>', re.S)
    for match in pattern.finditer(str(page or "")):
        classes, tid, block = match.group(1), match.group(2), match.group(3)
        if "tl_top" in classes:
            continue
        title_match = re.search(r'<div\s+class="[^"]*ti_title[^"]*"[^>]*>(.*?)</div>', block, re.S)
        title = _strip_html(title_match.group(1)) if title_match else ""
        title = re.sub(r"^置顶\s*", "", title).strip()
        if not title:
            continue
        reply_match = re.search(r'btn_reply.*?<span\s+class="btn_icon"[^>]*>(\d+)</span>', block, re.S)
        reply_num = _to_int(reply_match.group(1)) if reply_match else 0
        threads.append(
            {
                "tid": tid,
                "title": title,
                "abstract": "",
                "author": "",
                "reply_num": reply_num,
                "view_num": 0,
                "agree_num": 0,
                "create_time": 0,
                "last_time": 0,
                "is_good": False,
                "is_top": False,
                "forum": {"name": bar},
                "url": thread_url(tid),
                "bar": bar,
            }
        )
    return threads


def parse_thread_posts_html(page: str, tid: str) -> tuple[str, list[dict[str, Any]]]:
    """解析移动网页帖子标题和楼层。"""
    title_match = re.search(r"<title>(.*?)</title>", str(page or ""), re.S | re.I)
    title = _strip_html(title_match.group(1)) if title_match else ""
    parser = _PostContentParser()
    try:
        parser.feed(str(page or ""))
        parser.close()
    except Exception:  # noqa: BLE001 - 第三方 HTML 不可信
        logger.debug("贴吧移动页解析失败", exc_info=True)
    posts: list[dict[str, Any]] = []
    for index, (lz, text) in enumerate(parser.posts, 1):
        content = clean_text(text)
        if not content:
            continue
        posts.append(
            {
                "id": f"{tid}:{index}",
                "floor": index,
                "time": 0,
                "author": "",
                "agree": 0,
                "content": content,
                "sub_posts": 0,
                "is_op": lz == "1",
                "is_fold": False,
            }
        )
    return title, posts


class TiebaClient:
    """串行、限速、带有限重试的贴吧只读客户端。"""

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        rate_limit: float = 2.0,
        cookie: str = "",
        proxy: str = "",
        transport: str = "auto",
        max_retries: int = 2,
        retry_backoff: float = 3.0,
    ) -> None:
        self.timeout = max(5.0, float(timeout))
        self.rate_limit = max(0.0, float(rate_limit))
        self.cookie = str(cookie or "").strip()
        self.proxy = str(proxy or "").strip()
        self.transport = transport if transport in {"auto", "client", "web"} else "auto"
        self._web_mode = False
        self.max_retries = max(0, min(5, int(max_retries)))
        self.retry_backoff = max(0.0, float(retry_backoff))
        self._opener = None
        if self.proxy:
            handler = urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy})
            self._opener = urllib.request.build_opener(handler)

    def _throttle(self) -> None:
        global _LAST_REQUEST
        with _REQUEST_LOCK:
            gap = self.rate_limit - (time.monotonic() - _LAST_REQUEST)
            if gap > 0:
                time.sleep(gap)
            _LAST_REQUEST = time.monotonic()

    @staticmethod
    def sign_params(params: dict[str, Any]) -> str:
        """按贴吧客户端规则计算 sign。"""
        normalized = {str(key): str(value) for key, value in params.items()}
        raw = "&".join(f"{key}={normalized[key]}" for key in sorted(normalized))
        return hashlib.md5((raw + "tiebaclient!!!").encode("utf-8")).hexdigest()

    def _client_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        normalized = {str(key): str(value) for key, value in params.items()}
        signed = dict(normalized)
        signed["sign"] = self.sign_params(normalized)
        url = _CLIENT_HOST + path + "?" + urllib.parse.urlencode(signed)
        headers = {
            "User-Agent": CLIENT_UA,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            request = urllib.request.Request(url, headers=headers)
            with _FETCH_LOCK:
                self._throttle()
                try:
                    opener = self._opener or urllib.request
                    with opener.urlopen(request, timeout=self.timeout) as response:
                        raw = response.read()
                except urllib.error.HTTPError as exc:
                    last_error = exc
                    if exc.code in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                        time.sleep(self.retry_backoff * (attempt + 1))
                        continue
                    raise TiebaError(f"贴吧接口 HTTP {exc.code}（{path}）") from exc
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    last_error = exc
                    if attempt < self.max_retries:
                        time.sleep(self.retry_backoff * (attempt + 1))
                        continue
                    raise TiebaError(f"贴吧接口网络错误：{exc}") from exc

            try:
                data = json.loads(raw.decode("utf-8", "ignore"))
            except json.JSONDecodeError as exc:
                raise TiebaError(f"贴吧接口返回不是 JSON：{raw[:80]!r}") from exc
            if not isinstance(data, dict):
                raise TiebaError("贴吧接口响应结构异常")
            code = data.get("error_code")
            if code is not None and str(code) != "0":
                message = str(data.get("error_msg") or "未知错误")
                if str(code) == "110001":
                    last_error = TiebaError("贴吧接口繁忙或触发风控（110001）")
                    if attempt < self.max_retries:
                        time.sleep(self.retry_backoff * (attempt + 1))
                        continue
                    raise last_error
                raise TiebaError(f"贴吧接口错误 {code}：{message}")
            return data
        raise TiebaError(f"贴吧请求失败：{last_error}")

    def _web_get(self, url: str) -> str:
        """移动网页版请求；带 Cookie 时数据中心 IP 也能拿到吧帖/楼层。"""
        headers = {
            "User-Agent": MOBILE_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "identity",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        request = urllib.request.Request(url, headers=headers)
        with _FETCH_LOCK:
            self._throttle()
            try:
                opener = self._opener or urllib.request
                with opener.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read()
            except urllib.error.HTTPError as exc:
                if exc.code in {403, 412, 429}:
                    raise TiebaError(f"贴吧网页触发百度安全验证/HTTP {exc.code}，请配置 cookie") from exc
                raise TiebaError(f"贴吧网页 HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise TiebaError(f"贴吧网页网络错误：{exc}") from exc
        page = raw.decode("utf-8", "ignore")
        if "百度安全验证" in page:
            raise TiebaError("贴吧网页触发百度安全验证，请配置 cookie")
        return page

    def _bar_threads_web(
        self,
        bar: str,
        *,
        pages: int = 2,
        limit: int = 30,
        keyword: str = "",
    ) -> list[dict[str, Any]]:
        kw = normalize_bar(bar)
        if not kw:
            raise TiebaError("吧名不能为空")
        pages = max(1, min(8, int(pages)))
        keyword = str(keyword or "").strip()
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for pn in range(pages):
            url = "https://tieba.baidu.com/mo/q/m?" + urllib.parse.urlencode({"kw": kw, "pn": pn})
            page = self._web_get(url)
            items = parse_bar_threads_html(page, kw)
            for item in items:
                tid = item.get("tid", "")
                if not tid or tid in seen:
                    continue
                if keyword and keyword not in item.get("title", "") and keyword not in item.get("abstract", ""):
                    continue
                seen.add(tid)
                out.append(item)
            if len(items) < 10:
                break
        return out

    def _thread_web(self, tid: str, *, pages: int = 2, limit: int = 30) -> dict[str, Any]:
        tid = str(tid or "").strip()
        if not _valid_tid(tid):
            raise TiebaError("帖子 ID 必须是 4-15 位数字")
        pages = max(1, min(8, int(pages)))
        title = ""
        posts: list[dict[str, Any]] = []
        seen: set[str] = set()
        for pn in range(pages):
            url = "https://tieba.baidu.com/mo/q/m?" + urllib.parse.urlencode({"kz": tid, "pn": pn})
            page = self._web_get(url)
            page_title, page_posts = parse_thread_posts_html(page, tid)
            if not title:
                title = page_title
            if not page_posts:
                break
            for post in page_posts:
                content = str(post.get("content") or "")
                if not content or content in seen:
                    continue
                seen.add(content)
                posts.append(post)
        thread = {
            "tid": tid,
            "title": title or f"帖子 {tid}",
            "abstract": "",
            "url": thread_url(tid),
            "reply_num": len(posts),
            "forum": {},
        }
        return {"thread": thread, "posts": posts, "forum": {}, "page": {}}

    @staticmethod
    def _is_block_error(exc: Exception) -> bool:
        text = str(exc)
        return "110001" in text or "403" in text or "安全验证" in text

    def _use_web(self) -> bool:
        return self.transport == "web" or (self.transport == "auto" and self._web_mode)

    # ---------------- 吧 / 帖子 ----------------

    def forum_threads(
        self,
        bar: str,
        *,
        page: int = 0,
        limit: int = 30,
        sort_type: int = 0,
    ) -> dict[str, Any]:
        """拉取一个吧的帖子列表。"""
        kw = normalize_bar(bar)
        if not kw:
            raise TiebaError("吧名不能为空")
        page = max(0, int(page))
        limit = max(1, min(50, int(limit)))
        data = self._client_get(
            "/c/f/frs/page",
            {"kw": kw, "pn": page, "rn": limit, "sort_type": max(0, int(sort_type))},
        )
        raw_threads = data.get("thread_list")
        if raw_threads is None:
            raw_threads = (data.get("forum_list") or {}).get("thread_list") or []
        if not isinstance(raw_threads, list):
            raise TiebaError("贴吧帖子列表结构异常")
        threads = [thread for raw in raw_threads if (thread := normalize_thread(raw))]
        forum = _normalize_forum(data.get("forum") or data.get("forum_list") or {})
        return {
            "bar": kw,
            "forum": forum,
            "threads": threads,
            "page": data.get("page") if isinstance(data.get("page"), dict) else {},
        }

    def thread_posts(
        self,
        tid: str,
        *,
        page: int = 0,
        limit: int = 30,
    ) -> dict[str, Any]:
        """拉取帖子某一页的楼层。"""
        tid = str(tid or "").strip()
        if not _valid_tid(tid):
            raise TiebaError("帖子 ID 必须是 4-15 位数字")
        page = max(0, int(page))
        limit = max(1, min(50, int(limit)))
        data = self._client_get(
            "/c/f/pb/page",
            {"kz": tid, "pn": page, "rn": limit, "with_floor": 1},
        )
        thread = normalize_thread(data.get("thread") or {})
        if not thread:
            thread = {"tid": tid, "title": f"帖子 {tid}", "url": thread_url(tid)}
        thread["tid"] = tid
        raw_posts = data.get("post_list")
        if raw_posts is None:
            raw_posts = []
        if not isinstance(raw_posts, list):
            raise TiebaError("贴吧楼层结构异常")
        posts = [post for raw in raw_posts if (post := normalize_post(raw, thread=thread))]
        return {
            "thread": thread,
            "posts": posts,
            "forum": _normalize_forum(data.get("forum") or {}),
            "page": data.get("page") if isinstance(data.get("page"), dict) else {},
        }

    def _thread_client(
        self,
        tid: str,
        *,
        pages: int = 2,
        limit: int = 30,
    ) -> dict[str, Any]:
        """拉取帖子正文，最多 pages 页，按楼层去重。"""
        pages = max(1, min(8, int(pages)))
        first = self.thread_posts(tid, page=0, limit=limit)
        thread = first["thread"]
        forum = first["forum"]
        page_meta = first["page"] or {}
        total_page = max(1, _to_int(page_meta.get("total_page") or 1))
        posts: list[dict[str, Any]] = list(first["posts"])
        seen = {post.get("id") for post in posts if post.get("id")}
        for pn in range(1, min(pages, total_page)):
            try:
                part = self.thread_posts(tid, page=pn, limit=limit)
            except TiebaError as exc:
                logger.debug("帖子 %s 第 %d 页失败(忽略): %s", tid, pn + 1, exc)
                break
            if not part["posts"]:
                break
            for post in part["posts"]:
                if post.get("id") in seen:
                    continue
                if post.get("id"):
                    seen.add(post["id"])
                posts.append(post)
            if not part["thread"].get("title") and thread.get("title"):
                part["thread"]["title"] = thread["title"]
            thread.update({k: v for k, v in part["thread"].items() if v not in (None, "", 0, [])})
            if part["forum"] and not forum:
                forum = part["forum"]
        posts.sort(key=lambda post: (post.get("floor") or 0, post.get("time") or 0))
        thread["tid"] = str(tid)
        thread["forum"] = forum or thread.get("forum") or {}
        thread["url"] = thread_url(tid)
        thread["reply_num"] = max(thread.get("reply_num") or 0, len(posts))
        return {"thread": thread, "posts": posts, "forum": forum, "page": page_meta}

    def _bar_threads_client(
        self,
        bar: str,
        *,
        pages: int = 2,
        limit: int = 30,
        keyword: str = "",
    ) -> list[dict[str, Any]]:
        """扫一个吧的前几页，可按关键词过滤标题/摘要。"""
        pages = max(1, min(8, int(pages)))
        keyword = str(keyword or "").strip()
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for pn in range(pages):
            page = self.forum_threads(bar, page=pn, limit=limit)
            threads = page["threads"]
            for thread in threads:
                tid = thread.get("tid")
                if not tid or tid in seen:
                    continue
                if keyword and keyword not in thread.get("title", "") and keyword not in thread.get("abstract", ""):
                    continue
                seen.add(tid)
                out.append(thread)
            if len(threads) < limit:
                break
        return out

    def thread(
        self,
        tid: str,
        *,
        pages: int = 2,
        limit: int = 30,
    ) -> dict[str, Any]:
        """拉取帖子正文；客户端接口被风控时自动切移动网页。"""
        if self._use_web():
            return self._thread_web(tid, pages=pages, limit=limit)
        try:
            return self._thread_client(tid, pages=pages, limit=limit)
        except TiebaError as exc:
            if self.transport == "auto" and self._is_block_error(exc):
                self._web_mode = True
                logger.info("贴吧客户端接口被风控，切换移动网页模式")
                return self._thread_web(tid, pages=pages, limit=limit)
            raise

    def bar_threads(
        self,
        bar: str,
        *,
        pages: int = 2,
        limit: int = 30,
        keyword: str = "",
    ) -> list[dict[str, Any]]:
        """扫吧帖列表；客户端接口被风控时自动切移动网页。"""
        if self._use_web():
            return self._bar_threads_web(bar, pages=pages, limit=limit, keyword=keyword)
        try:
            return self._bar_threads_client(bar, pages=pages, limit=limit, keyword=keyword)
        except TiebaError as exc:
            if self.transport == "auto" and self._is_block_error(exc):
                self._web_mode = True
                logger.info("贴吧客户端接口被风控，切换移动网页模式")
                return self._bar_threads_web(bar, pages=pages, limit=limit, keyword=keyword)
            raise


def select_threads(
    threads: Iterable[dict[str, Any]],
    *,
    seen: set[str],
    min_replies: int,
    max_threads: int,
    keywords: Iterable[str] = (),
    exclude_pattern: str = "",
    bar: str = "",
) -> list[dict[str, Any]]:
    """从吧帖列表里挑出值得读的帖子。"""
    keywords = [str(item).strip() for item in keywords if str(item).strip()]
    exclude = re.compile(exclude_pattern, re.IGNORECASE) if exclude_pattern else None
    candidates: list[dict[str, Any]] = []
    for thread in threads:
        tid = str(thread.get("tid") or "")
        if not tid or tid in seen:
            continue
        if int(thread.get("reply_num") or 0) <= max(0, int(min_replies)):
            continue
        title = str(thread.get("title") or "")
        abstract = str(thread.get("abstract") or "")
        if exclude and exclude.search(title + " " + abstract):
            continue
        if keywords and not any(keyword in title or keyword in abstract for keyword in keywords):
            continue
        if not title:
            continue
        item = dict(thread)
        item["bar"] = bar
        candidates.append(item)
    candidates.sort(key=lambda item: (int(item.get("reply_num") or 0), int(item.get("agree_num") or 0)), reverse=True)
    return candidates[: max(0, int(max_threads))]
