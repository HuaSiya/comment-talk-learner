"""极简 B站只读客户端（纯标准库）。

只做本插件需要的事：取热门/排行/搜索/UP 投稿、视频信息、热门评论。
所有请求串行并限速；遇到风控直接抛出 BiliError，由上层停止本轮。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable

logger = logging.getLogger("bilibili_comment_learner.bili")

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
}

# WBI 用的 64 位重排表（B站前端常量）
_MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
]

_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_EM_TAG_RE = re.compile(r"</?em[^>]*>")


class BiliError(RuntimeError):
    """B站接口异常（含风控 -352 / HTTP 412）。"""


def extract_bvid(text: str) -> str:
    """从文本里提取 BV 号；没有则返回空字符串。"""
    match = _BV_RE.search(str(text or ""))
    return match.group(1) if match else ""


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


class BiliClient:
    """串行、限速、带有限重试的 B站只读客户端。"""

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        rate_limit: float = 1.5,
        sessdata: str = "",
        max_retries: int = 2,
        retry_backoff: float = 3.0,
    ) -> None:
        self.timeout = max(5.0, float(timeout))
        self.rate_limit = max(0.0, float(rate_limit))
        self.max_retries = max(0, min(5, int(max_retries)))
        self.retry_backoff = max(0.0, float(retry_backoff))
        self._sessdata = (sessdata or "").strip()
        self._last_request = 0.0
        self._cookie = ""
        self._mixin_key: str | None = None
        self._mixin_at = 0.0

    # ---------------- 基础请求 ----------------

    def _throttle(self) -> None:
        gap = self.rate_limit - (time.monotonic() - self._last_request)
        if gap > 0:
            time.sleep(gap)
        self._last_request = time.monotonic()

    def _headers(self) -> dict[str, str]:
        headers = dict(UA)
        if self._cookie:
            headers["Cookie"] = self._cookie
        return headers

    def _fetch(self, url: str) -> bytes:
        """带有限重试的 GET；412/429/5xx/网络错误会退避重试。"""
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            request = urllib.request.Request(url, headers=self._headers())
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code in {412, 429, 500, 502, 503, 504} and attempt < self.max_retries:
                    time.sleep(self.retry_backoff * (attempt + 1))
                    continue
                raise BiliError(f"HTTP {exc.code}: {url[:120]}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(self.retry_backoff * (attempt + 1))
                    continue
                raise BiliError(f"网络错误: {exc}") from exc
        raise BiliError(f"请求失败: {last_error}")

    def _raw_json(self, url: str) -> dict[str, Any]:
        """不校验 code 的原始 JSON（nav 未登录时 code=-101，但 wbi_img 仍可用）。"""
        raw = self._fetch(url)
        try:
            payload = json.loads(raw.decode("utf-8", "ignore"))
        except json.JSONDecodeError as exc:
            raise BiliError(f"响应不是 JSON: {raw[:80]!r}") from exc
        if not isinstance(payload, dict):
            raise BiliError("响应结构异常")
        return payload

    def _get_json(self, url: str) -> dict[str, Any]:
        if not self._cookie:
            # 先拿匿名 buvid，部分接口缺它会直接 412/-352。
            self._ensure_cookie()
        payload = self._raw_json(url)
        code = payload.get("code")
        if code in {-352, -412}:
            raise BiliError(f"触发风控({code})，请降低频率或稍后再试")
        if code != 0:
            raise BiliError(f"接口返回 code={code} msg={payload.get('message')}")
        return payload

    def _ensure_cookie(self) -> str:
        """匿名指纹 cookie（buvid3/buvid4）；缺了它部分接口会 -101/-352。"""
        if self._cookie:
            return self._cookie
        try:
            spi = self._raw_json("https://api.bilibili.com/x/frontend/finger/spi")
            data = spi.get("data") or {}
            b3 = _as_str(data.get("b_3"))
            b4 = _as_str(data.get("b_4"))
            if b3:
                self._cookie = f"buvid3={b3}; buvid4={b4}"
                if self._sessdata:
                    self._cookie += f"; SESSDATA={self._sessdata}"
        except BiliError as exc:
            logger.debug("取 buvid 失败，继续裸请求: %s", exc)
        return self._cookie

    # ---------------- WBI 签名 ----------------

    def _get_mixin_key(self, ttl: float = 3600.0) -> str:
        if self._mixin_key and (time.time() - self._mixin_at) < ttl:
            return self._mixin_key
        self._ensure_cookie()
        payload = self._raw_json("https://api.bilibili.com/x/web-interface/nav")
        wbi = (payload.get("data") or {}).get("wbi_img") or {}
        img = _as_str(wbi.get("img_url")).rsplit("/", 1)[-1].split(".")[0]
        sub = _as_str(wbi.get("sub_url")).rsplit("/", 1)[-1].split(".")[0]
        if not img or not sub:
            raise BiliError(f"拿不到 WBI 密钥（nav code={payload.get('code')}）")
        raw = img + sub
        self._mixin_key = "".join(raw[i] for i in _MIXIN_TAB if i < len(raw))[:32]
        self._mixin_at = time.time()
        return self._mixin_key

    def _sign(self, params: dict[str, Any]) -> dict[str, Any]:
        signed = {key: value for key, value in params.items() if value is not None}
        signed["wts"] = int(time.time())
        query = urllib.parse.urlencode(sorted(signed.items()))
        query = "".join(char for char in query if char not in "!'()*")
        signed["w_rid"] = hashlib.md5((query + self._get_mixin_key()).encode()).hexdigest()
        return signed

    def _wbi_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"https://api.bilibili.com{path}?" + urllib.parse.urlencode(self._sign(params))
        return self._get_json(url)

    # ---------------- 视频列表 ----------------

    @staticmethod
    def _parse_video(item: Any, *, source: str) -> dict[str, Any] | None:
        """兼容热门/排行/搜索/UP投稿几种返回结构。"""
        if not isinstance(item, dict):
            return None
        bvid = _as_str(item.get("bvid")).strip() or extract_bvid(_as_str(item.get("arcurl")))
        if not bvid:
            return None

        owner = item.get("owner")
        if isinstance(owner, dict):
            owner_name = _as_str(owner.get("name"))
            owner_mid = _as_int(owner.get("mid"))
        else:
            owner_name = _as_str(item.get("author") or item.get("owner"))
            owner_mid = 0

        stat = item.get("stat") if isinstance(item.get("stat"), dict) else {}
        title = _EM_TAG_RE.sub("", _as_str(item.get("title"))).strip()

        duration_raw = item.get("duration")
        duration = _as_int(duration_raw)
        if duration <= 0 and isinstance(duration_raw, str) and ":" in duration_raw:
            parts = [_as_int(part) for part in duration_raw.split(":")]
            if len(parts) == 2:
                duration = parts[0] * 60 + parts[1]
            elif len(parts) == 3:
                duration = parts[0] * 3600 + parts[1] * 60 + parts[2]

        return {
            "bvid": bvid,
            "aid": _as_int(item.get("aid")),
            "title": title,
            "owner": owner_name,
            "owner_mid": owner_mid,
            "view": _as_int(stat.get("view") or item.get("play")),
            "reply": _as_int(stat.get("reply") or item.get("review") or item.get("video_review")),
            "pubdate": _as_int(item.get("pubdate") or item.get("created")),
            "duration": duration,
            "tname": _as_str(item.get("tname")),
            "source": source,
        }

    @classmethod
    def _parse_video_list(cls, raw: Any, *, source: str) -> list[dict[str, Any]]:
        if not isinstance(raw, list):
            return []
        videos: list[dict[str, Any]] = []
        for item in raw:
            parsed = cls._parse_video(item, source=source)
            if parsed:
                videos.append(parsed)
        return videos

    def popular(self, *, pages: int = 1, page_size: int = 20) -> list[dict[str, Any]]:
        """热门视频列表（无需签名）。"""
        videos: list[dict[str, Any]] = []
        for page in range(1, max(1, pages) + 1):
            payload = self._get_json(
                "https://api.bilibili.com/x/web-interface/popular?"
                + urllib.parse.urlencode({"ps": max(1, min(50, page_size)), "pn": page})
            )
            videos.extend(self._parse_video_list((payload.get("data") or {}).get("list"), source="popular"))
        return videos

    def ranking(self, rid: int = 0) -> list[dict[str, Any]]:
        """分区排行榜（rid=0 为全站）。"""
        payload = self._wbi_get("/x/web-interface/ranking/v2", {"rid": int(rid), "type": "all"})
        return self._parse_video_list((payload.get("data") or {}).get("list"), source=f"ranking:{rid}")

    def search_videos(self, keyword: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """关键词搜索视频（WBI 签名）。"""
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        payload = self._wbi_get(
            "/x/web-interface/wbi/search/type",
            {"search_type": "video", "keyword": keyword, "page": 1},
        )
        result = (payload.get("data") or {}).get("result") or []
        videos = self._parse_video_list(result, source=f"search:{keyword}")
        return videos[: max(1, limit)]

    def up_uploads(self, uid: int, *, limit: int = 10) -> list[dict[str, Any]]:
        """UP 主最新投稿（WBI 签名）。"""
        if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
            raise ValueError("UP 主 UID 必须为正整数")
        payload = self._wbi_get(
            "/x/space/wbi/arc/search",
            {"mid": uid, "pn": 1, "ps": max(1, min(50, limit)), "order": "pubdate"},
        )
        body = payload.get("data")
        if not isinstance(body, dict):
            raise BiliError("UP 主投稿列表响应结构异常")
        listing = body.get("list")
        if not isinstance(listing, dict):
            raise BiliError("UP 主投稿列表缺少 list")
        raw = listing.get("vlist")
        videos = self._parse_video_list(raw, source=f"up:{uid}")
        for video in videos:
            video["author_uid"] = uid
        return videos[: max(1, limit)]

    def video_info(self, bvid: str) -> dict[str, Any]:
        """视频元信息（aid/标题/UP/播放/评论数等）。"""
        bvid = extract_bvid(bvid) or (bvid or "").strip()
        if not bvid:
            raise ValueError("缺少 BV 号")
        payload = self._get_json(
            "https://api.bilibili.com/x/web-interface/view?" + urllib.parse.urlencode({"bvid": bvid})
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise BiliError("视频信息响应结构异常")
        stat = data.get("stat") if isinstance(data.get("stat"), dict) else {}
        owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
        return {
            "bvid": _as_str(data.get("bvid") or bvid),
            "aid": _as_int(data.get("aid")),
            "title": _as_str(data.get("title")),
            "owner": _as_str(owner.get("name")),
            "owner_mid": _as_int(owner.get("mid")),
            "view": _as_int(stat.get("view")),
            "reply": _as_int(stat.get("reply")),
            "pubdate": _as_int(data.get("pubdate")),
            "duration": _as_int(data.get("duration")),
            "tname": _as_str(data.get("tname")),
            "source": "view",
        }

    # ---------------- 评论 ----------------

    @staticmethod
    def _parse_comment(item: Any, *, owner_mid: int = 0) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        content = item.get("content") if isinstance(item.get("content"), dict) else {}
        member = item.get("member") if isinstance(item.get("member"), dict) else {}
        message = _as_str(content.get("message")).strip()
        if not message:
            return None
        level_info = member.get("level_info") if isinstance(member.get("level_info"), dict) else {}
        vip = member.get("vip") if isinstance(member.get("vip"), dict) else {}
        mid = _as_int(member.get("mid"))
        return {
            "rpid": _as_int(item.get("rpid")),
            "root": _as_int(item.get("root")),
            "parent": _as_int(item.get("parent")),
            "message": message,
            "like": _as_int(item.get("like")),
            "ctime": _as_int(item.get("ctime")),
            "user": _as_str(member.get("uname")),
            "mid": mid,
            "level": _as_int(level_info.get("current_level")),
            "sign": _as_str(member.get("sign")),
            "avatar": _as_str(member.get("avatar")),
            "vip": _as_int(vip.get("vipStatus")),
            "is_up": bool(owner_mid and mid == int(owner_mid)),
            "rcount": _as_int(item.get("rcount")),
        }

    def comments(
        self,
        aid: int,
        *,
        pages: int = 2,
        page_size: int = 20,
        include_sub_replies: bool = True,
        sub_reply_roots: int = 3,
        sub_replies_per_root: int = 20,
        owner_mid: int = 0,
    ) -> list[dict[str, Any]]:
        """按热度取评论；可选附带楼中楼。

        未登录时 B站 对热门评论列表只给 3 条左右，但楼中楼接口仍能返回
        20 条/根评论，且对话性更强，适合学“人怎么接话”。
        """
        aid = _as_int(aid)
        if aid <= 0:
            return []
        size = max(1, min(50, int(page_size)))
        comments: list[dict[str, Any]] = []
        seen: set[int] = set()
        cursor = 0
        for _ in range(max(1, int(pages))):
            payload = self._get_json(
                "https://api.bilibili.com/x/v2/reply/main?"
                + urllib.parse.urlencode(
                    {"oid": aid, "type": 1, "mode": 3, "ps": size, "next": cursor}
                )
            )
            body = payload.get("data") or {}
            replies = body.get("replies")
            if not isinstance(replies, list):
                break
            for raw in replies:
                parsed = self._parse_comment(raw, owner_mid=owner_mid)
                if not parsed:
                    continue
                rpid = parsed["rpid"]
                if rpid and rpid in seen:
                    continue
                if rpid:
                    seen.add(rpid)
                comments.append(parsed)
            cursor_data = body.get("cursor") if isinstance(body.get("cursor"), dict) else {}
            cursor = _as_int(cursor_data.get("next"))
            if not cursor or cursor_data.get("is_end"):
                break

        if include_sub_replies and sub_reply_roots > 0 and sub_replies_per_root > 0:
            roots = [item for item in comments if int(item.get("rcount") or 0) > 0][: int(sub_reply_roots)]
            for root in roots:
                root_rpid = _as_int(root.get("rpid"))
                if root_rpid <= 0:
                    continue
                try:
                    payload = self._get_json(
                        "https://api.bilibili.com/x/v2/reply/reply?"
                        + urllib.parse.urlencode(
                            {
                                "oid": aid,
                                "type": 1,
                                "root": root_rpid,
                                "ps": max(1, min(50, int(sub_replies_per_root))),
                                "pn": 1,
                            }
                        )
                    )
                except BiliError as exc:
                    if "风控" in str(exc):
                        raise
                    logger.debug("楼中楼读取失败(忽略): %s", exc)
                    continue
                body = payload.get("data") or {}
                replies = body.get("replies")
                if not isinstance(replies, list):
                    continue
                for raw in replies[: int(sub_replies_per_root)]:
                    parsed = self._parse_comment(raw, owner_mid=owner_mid)
                    if not parsed:
                        continue
                    rpid = parsed["rpid"]
                    if rpid and rpid in seen:
                        continue
                    if rpid:
                        seen.add(rpid)
                    comments.append(parsed)
        return comments


def merge_video_lists(
    groups: Iterable[Iterable[dict[str, Any]]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """按来源轮转合并视频列表，避免热门把指定来源挤掉。"""
    queues = [list(group) for group in groups if group]
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    while queues and len(merged) < max(0, limit):
        next_queues: list[list[dict[str, Any]]] = []
        for queue in queues:
            while queue:
                video = queue.pop(0)
                bvid = _as_str(video.get("bvid"))
                if not bvid or bvid in seen:
                    continue
                seen.add(bvid)
                merged.append(video)
                break
            if queue:
                next_queues.append(queue)
            if len(merged) >= max(0, limit):
                break
        queues = next_queues
    return merged
