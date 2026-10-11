"""后端接口发现：从**页面实际发出的流量**里学出后端 API（2026-10-09）。

## 用户的需求

> 「我希望这个系统也具有探索后端的能力……不只是前端页面，然后后端也加一个进去，
> 然后他需要有探索后端的能力去拿到这个菜单或者相关的数据。」

也就是说：测试不该只会"点界面看像素"。一个内部管理系统的**菜单本身就是后端给的**
（前端拿 `/api/menu` 渲染侧边栏），而"进了列表页看到数据"其实是 `GET /api/xxx/list`
的返回。只知道点界面的话，这两个事实都只能靠猜。

## 怎么拿到这些接口：观察，不是探测

用户选的是「从页面流量里自学接口」，这也正是最安全、最贴实际的做法：

* 页面为了渲染**必然**会调后端 —— 那些请求就是权威答案，不用猜路径、不用撞目录；
* 不会像主动扫描那样朝服务器发一堆它没被设计来处理的请求；
* 学到的接口一定是**真实在用**的（前端不会调用一个不存在的接口）。

实现上挂在已有的 CDP `Fetch.requestPaused` 上 —— `app/browser_hygiene.py` 已经为了
拦第三方 beacon 拦截了**全部**请求，我们只是在这个既有钩子上多看两眼，不新增任何开销。

## 三条必须守住的安全线

1. **只观察，不主动发请求。** 本模块没有任何"去试试这个路径"的代码。
   学习阶段的 HTTP 全部由页面自己发起。真正的重放（用接口拿数据、做断言）
   在 `app/api_probe.py`，那边有独立的只读白名单。
2. **不落库敏感值。** 只记 `参数名 + 值的形状`（如 `page` → `int`），
   绝不记 token / 密码 / 具体值。请求头一律丢弃。
3. **写操作只登记、不重放。** POST/PUT/DELETE 会被记下来（用户需要知道"这个系统的
   写接口长什么样"），但标记 `writable=True`，重放层默认拒绝。

## 为什么纯粹在这里做判断（不碰浏览器）

`_should_record` / `normalize_path` / `parse_url` 都是纯函数：
接口识别的规则最容易"看着对、实际把静态资源也收进来"，那是要反复调的，
必须能不带浏览器、不带网络地单测。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urlparse

log = logging.getLogger("potato-test.api_discovery")

# 只读方法 —— 重放层允许的集合。HEAD 也算读。
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# 这些响应类型不可能是我们要找的业务接口。
_STATIC_EXT = re.compile(
    r"\.(?:js|mjs|css|map|png|jpe?g|gif|webp|svg|ico|woff2?|ttf|eot|mp4|webm|pdf|zip|xlsx?|docx?)$",
    re.I,
)

# 明显的基础设施/埋点路径，不是业务接口。
_NOISE = re.compile(
    r"/(?:sockjs|websocket|ws|hot-update|__webpack|hm\.baidu|google-analytics|sentry|"
    r"favicon|manifest\.webmanifest|service-worker|sw\.js)",
    re.I,
)

# 路径段里"看起来像 ID"的：纯数字、UUID、长十六进制。
# 归一化的目的：`/api/user/1024` 和 `/api/user/2048` 是**同一个接口**，
# 不归一化的话每换一个 id 就会在清单里多出一条，接口表会爆炸且没有信息量。
_SEG_ID = re.compile(
    r"^(?:\d+|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{16,})$"
)


@dataclass
class Endpoint:
    """一个被观察到的后端接口。"""

    method: str
    # 归一化后的路径（ID 换成 {id}），作为"同一个接口"的身份
    path: str
    # 原始路径样本（保留一条，便于人工核对归一化有没有做错）
    sample: str = ""
    # 查询参数名 → 值的形状（不存值）
    query: dict[str, str] = field(default_factory=dict)
    # 请求体顶层字段名（只有写请求才有；同样不存值）
    body_fields: list[str] = field(default_factory=list)
    # 见到的次数 —— 次数多的接口更可能是核心业务接口
    hits: int = 1
    # 是否是写操作。写操作会被登记，但重放层默认拒绝。
    writable: bool = False

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"

    def as_dict(self) -> dict:
        return {
            "method": self.method,
            "path": self.path,
            "sample": self.sample,
            "query": self.query,
            "body_fields": self.body_fields,
            "hits": self.hits,
            "writable": self.writable,
        }


def value_shape(v: str) -> str:
    """值的"形状"，不是值本身。

    这是**不落库敏感值**的实现点：`?token=abc123` 只记成 `token: str`，
    `?page=2` 记成 `page: int`。形状足以让模型知道"这个参数要传什么类型"，
    而泄漏不了任何真实数据。
    """
    s = (v or "").strip()
    if not s:
        return "empty"
    if re.fullmatch(r"-?\d+", s):
        return "int"
    if re.fullmatch(r"-?\d+\.\d+", s):
        return "float"
    if re.fullmatch(r"(?:true|false)", s, re.I):
        return "bool"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ].*)?", s):
        return "date"
    # 长随机串通常是 token / 签名 —— 连形状都别细说，只报"opaque"
    if len(s) >= 24 and re.fullmatch(r"[A-Za-z0-9_\-./+=]+", s):
        return "opaque"
    return "str"


def normalize_path(path: str) -> str:
    """把路径里的具体 ID 换成 `{id}`，让"同一个接口"归成一条。

    `/api/user/1024/profile` 与 `/api/user/2048/profile` → `/api/user/{id}/profile`

    为什么不归一的后果很实际：一个列表页翻两页、点开三条详情，
    就能在接口清单里造出十几条"不同"的接口，用户看到的是一张没法用的表。
    """
    segs = []
    for seg in (path or "/").split("/"):
        if not seg:
            continue
        if _SEG_ID.match(seg):
            segs.append("{id}")
        else:
            segs.append(seg)
    return "/" + "/".join(segs) if segs else "/"


def parse_url(url: str) -> tuple[str, str, dict[str, str]]:
    """`url` → (origin, 归一化路径, 查询参数名→形状)。

    查询参数在**这里**就被压成形状：后面所有环节（落库、进提示词、回给前端）
    拿到的都只有形状，敏感值根本没有机会流出去。
    """
    try:
        p = urlparse(url)
    except ValueError:
        return "", "/", {}
    origin = f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""
    path = normalize_path(unquote(p.path or "/"))
    q: dict[str, str] = {}
    for k, v in parse_qsl(p.query or "", keep_blank_values=True):
        if k and k not in q:
            q[k] = value_shape(v)
    return origin, path, q


def looks_like_api(url: str, page_origin: str, resource_type: str = "") -> bool:
    """这条请求是不是"业务接口"。

    判据刻意保守：宁可漏掉几个，也不要把静态资源和埋点灌进接口表 ——
    一张混着 200 个 `.js` 的接口清单，用户看一眼就不想再打开了。

    收进来的信号（满足任一）：
      * 路径里带 `/api`、`/rest`、`/graphql`、`/v1`~`/v3` 这类后端前缀；
      * 响应是 XHR/fetch（`resource_type` 说了算）；
      * 路径超过两段且不含文件扩展名（内部系统常见的 `/system/user/list` 风格）。
    """
    if not url or url.startswith(("data:", "blob:", "chrome", "about:", "devtools")):
        return False
    if _STATIC_EXT.search(url) or _NOISE.search(url):
        return False

    low = url.lower()
    if any(t in low for t in ("/api/", "/api?", "/rest/", "/graphql", "/v1/", "/v2/", "/v3/")):
        return True
    if resource_type.upper() in ("XHR", "FETCH"):
        return True

    # 最后一条兜底：多段路径且不像静态资源
    try:
        path = urlparse(url).path or ""
    except ValueError:
        return False
    segs = [s for s in path.split("/") if s]
    return len(segs) >= 2 and not _STATIC_EXT.search(path)


def parse_post_data(raw, content_type: str = "") -> list[str]:
    """请求体的**顶层字段名**（不存值）。

    只取字段名。写接口的字段名对"这个用例该填什么"很有用，
    而字段值几乎一定含真实数据（客户名、手机号），一律不要。
    """
    if not raw:
        return []
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return []
    s = str(raw).strip()
    if not s or len(s) > 200_000:
        return []
    ct = (content_type or "").lower()
    if "json" in ct or s.startswith(("{", "[")):
        try:
            import json

            data = json.loads(s)
        except (ValueError, TypeError):
            return []
        if isinstance(data, dict):
            return sorted(str(k) for k in data)[:40]
        return []
    if "form-urlencoded" in ct:
        try:
            return sorted({k for k, _ in parse_qsl(s, keep_blank_values=True)})[:40]
        except Exception:  # noqa: BLE001
            return []
    return []


class Recorder:
    """在一轮用例执行期间累积观察到的接口。

    生命周期跟着**一次用例执行**：`executor` 建一个，跑完把 `endpoints()` 交出去。
    不做全局单例 —— 并发跑用例时全局状态会互相串，
    而"哪个接口是哪条用例跑出来的"这个信息也就丢了。
    """

    def __init__(self, page_origin: str = "") -> None:
        self.page_origin = page_origin
        self._by_key: dict[str, Endpoint] = {}
        # 统计用：看有多少请求是页面自己发的（用来判断"这次探索有没有东西"）
        self.seen = 0

    def observe(
        self,
        url: str,
        method: str = "GET",
        resource_type: str = "",
        post_data=None,
        content_type: str = "",
    ) -> None:
        """记一条请求。**永不抛异常** —— 它挂在浏览器的请求回调上，
        抛出去会把整个用例带崩，而它只是个附加功能。"""
        try:
            self.seen += 1
            if not looks_like_api(url, self.page_origin, resource_type):
                return
            origin, path, query = parse_url(url)
            m = (method or "GET").upper()
            key = f"{m} {path}"
            ep = self._by_key.get(key)
            if ep is None:
                ep = Endpoint(
                    method=m,
                    path=path,
                    sample=url[:500],
                    query=query,
                    writable=m not in READ_METHODS,
                )
                self._by_key[key] = ep
            else:
                ep.hits += 1
                # 后续观察补齐新出现的查询参数（同一接口不同调用会带不同参数）
                for k, shape in query.items():
                    ep.query.setdefault(k, shape)
            if ep.writable and post_data is not None:
                fields = parse_post_data(post_data, content_type)
                if fields:
                    ep.body_fields = sorted(set(ep.body_fields) | set(fields))
        except Exception:  # noqa: BLE001 — 观察失败绝不能影响用例
            log.debug("api_discovery: observe failed for %s", url[:120], exc_info=True)

    def endpoints(self) -> list[Endpoint]:
        """按"被调用次数"降序 —— 次数多的更可能是核心业务接口，
        用户/模型都应该先看它们。"""
        return sorted(self._by_key.values(), key=lambda e: (-e.hits, e.path))

    def is_empty(self) -> bool:
        return not self._by_key
