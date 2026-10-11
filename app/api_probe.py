"""只读的后端接口探查：用观察到的接口**拿数据**，供用例做断言（2026-10-09）。

## 它和 `api_discovery` 的分工

* `api_discovery`：**观察**页面发出的流量，学出"这个系统有哪些后端接口"。
  它不发任何请求。
* 本模块：**重放**其中**只读**的那些，把返回数据交给用例/判定器当证据。
  它是唯一会主动发 HTTP 的地方，所以安全线全部集中在这里。

## 用户选的安全边界（原话）

> 「探索只读，写操作必须显式声明」

具体落到代码上是三道闸，缺一不可：

1. **方法白名单**：只允许 GET / HEAD / OPTIONS。POST / PUT / PATCH / DELETE
   一律拒绝 —— 不管调用方多想要。理由很直接：探索阶段的目的是"看懂这个系统"，
   而写请求一旦发出去就可能在你测试环境里建/删真数据，那代价不是"少拿到一点信息"
   能比的。
2. **同源**：只允许打向项目里已经配好的那个后端 Origin。
   不让它成为"用你的凭据去打任意地址"的通用 SSRF 跳板。
3. **有上限**：单次请求有超时，单轮有次数上限。没有上限的话，一个用例可以
   对着后端刷几千次请求 —— 在内网里这是很容易被当成攻击的行为。

## 为什么把 URL 拼装也做成纯函数

"路径 + 参数 → 完整 URL" 和 "方法是否允许" 是这个模块里最容易被绕过的地方
（一个编码技巧就能让 `/api/x` 变成 `//evil.com/x`）。它们必须能脱离网络单测。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import quote, urlparse, urlunparse

import httpx

from app.api_discovery import READ_METHODS, normalize_path

log = logging.getLogger("potato-test.api_probe")

# 单次请求超时（秒）。后端起不来时不该把用例拖死。
PROBE_TIMEOUT_S = 20.0

# 一轮用例里最多重放多少次。够覆盖"拿菜单 + 拿两三个列表"，
# 又不至于让一个用例变成压测。
MAX_PROBES_PER_RUN = 40

# 返回值截断：我们只要"够判定"的那部分，不是要同步整个库。
MAX_RESPONSE_CHARS = 20_000


class ProbeRefused(Exception):
    """请求被安全闸拒绝。**不是错误**，是设计行为 —— 调用方应当把原因说给用户听。"""


@dataclass
class ProbeResult:
    ok: bool
    status: int = 0
    # 解析出来的 JSON（能解析时），否则 None
    json: object = None
    # 原样文本（截断）。判定器/助手看这个。
    text: str = ""
    content_type: str = ""
    error: str = ""
    url: str = ""
    elapsed_ms: int = 0

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "status": self.status,
            "json": self.json,
            "text": self.text,
            "content_type": self.content_type,
            "error": self.error,
            "url": self.url,
            "elapsed_ms": self.elapsed_ms,
        }


def method_allowed(method: str) -> bool:
    """写操作一律拒绝。这是"探索只读"这条边界的**唯一**执行点。"""
    return (method or "").upper() in READ_METHODS


def build_url(origin: str, path: str, params: dict | None = None) -> str:
    """把 (origin, path, params) 拼成绝对 URL，并保证**不逃出 origin**。

    为什么单独一个函数：这是最容易被绕过的一步。攻击形态是让 path 里带
    编码过的 `//evil.com` 或 `..`，于是拼出来的 URL 指向另一个主机，
    而请求就带着项目的凭据打过去了。

    做法是"先拼、再验"：拼完之后重新解析，**确认 scheme+host 和 origin 一致**，
    不一致就拒。只靠字符串前缀判断会被 `https://host@evil.com` 这种形式骗过去。
    """
    base = (origin or "").strip().rstrip("/")
    if not base:
        raise ProbeRefused("没有可用的后端地址（origin 为空）")
    try:
        bp = urlparse(base)
    except ValueError as exc:
        raise ProbeRefused(f"后端地址无法解析：{base}") from exc
    if not (bp.scheme and bp.netloc):
        raise ProbeRefused(f"后端地址缺少 scheme/host：{base}")

    p = path if (path or "").startswith("/") else "/" + (path or "")

    # ★ 先拒掉"看起来是完整 URL"或"协议相对"的输入，而不是把它们当路径拼接。
    #
    # 拼接并不会真的打出去（`//evil.com/x` 会变成 `<origin>/evil.com/x`），
    # 但那是一种**隐式的语义转换**：调用方写了一个绝对地址、系统悄悄当路径用了。
    # 这种"猜用户意思"的行为在安全相关的边界上最危险 —— 今天它恰好安全，
    # 明天换一处拼接方式就未必。所以宁可明确拒绝，让调用方知道它给错了。
    if "://" in p or p.startswith("//"):
        raise ProbeRefused(
            f"拒绝：只接受项目后端下的**路径**（以 / 开头、不含 scheme 与 //），收到 {path[:120]!r}"
        )
    q = ""
    if params:
        parts = []
        for k, v in params.items():
            if v is None:
                continue
            parts.append(f"{quote(str(k), safe='')}={quote(str(v), safe='')}")
        q = "&".join(parts)

    full = f"{bp.scheme}://{bp.netloc}{p}" + (f"?{q}" if q else "")
    try:
        fp = urlparse(full)
    except ValueError as exc:
        raise ProbeRefused(f"拼出的 URL 无法解析：{full[:200]}") from exc

    # ★ 关键校验：拼出来的东西必须还在同一个 origin 上。
    if (fp.scheme, fp.netloc) != (bp.scheme, bp.netloc):
        raise ProbeRefused(
            f"拒绝：拼出的 URL 指向了别的主机（{fp.scheme}://{fp.netloc} != "
            f"{bp.scheme}://{bp.netloc}）—— 只允许访问项目自己的后端"
        )
    # 路径穿越也要拦：`/api/../../admin` 归一化之后不该越出根
    if ".." in (fp.path or ""):
        raise ProbeRefused(f"拒绝：路径里含 ..（{fp.path[:120]}）")
    return urlunparse((fp.scheme, fp.netloc, fp.path, "", fp.query, ""))


def _same_origin(a: str, b: str) -> bool:
    try:
        pa, pb = urlparse(a), urlparse(b)
    except ValueError:
        return False
    return (pa.scheme, pa.netloc) == (pb.scheme, pb.netloc)


class Prober:
    """一个用例（或一次探查）期间使用的只读客户端。

    用**项目自己的凭据**发请求 —— 这是关键：菜单接口通常需要登录态，
    而登录态就是浏览器里那份 cookie / Authorization。没有它，
    "拿菜单"只会拿到 401，也就失去了探索的意义。
    """

    def __init__(self, origin: str, headers: dict[str, str] | None = None) -> None:
        self.origin = (origin or "").rstrip("/")
        # 请求头由调用方从**浏览器当前会话**里取（cookie / bearer）。
        # 本模块不自己去读凭据存储 —— 那是凭据模块的职责，重复实现只会两边不一致。
        self.headers = dict(headers or {})
        self.used = 0

    async def get(self, path: str, params: dict | None = None) -> ProbeResult:
        """对项目自己的后端发一个**只读**请求。"""
        if self.used >= MAX_PROBES_PER_RUN:
            return ProbeResult(
                ok=False,
                error=f"已达单轮探查上限（{MAX_PROBES_PER_RUN} 次），不再发送请求",
            )
        try:
            url = build_url(self.origin, path, params)
        except ProbeRefused as exc:
            return ProbeResult(ok=False, error=str(exc))

        self.used += 1
        import time

        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(
                timeout=PROBE_TIMEOUT_S,
                follow_redirects=False,  # 跟随跳转可能离开 origin，自己判
                headers=self.headers,
            ) as c:
                r = await c.get(url)
        except Exception as exc:  # noqa: BLE001 — 探查失败要变成结果，不是异常
            return ProbeResult(
                ok=False, url=url, error=f"{type(exc).__name__}: {exc}"[:300]
            )

        ct = r.headers.get("content-type", "")
        text = r.text[:MAX_RESPONSE_CHARS] if r.text else ""
        data = None
        if "json" in ct.lower():
            try:
                data = r.json()
            except Exception:  # noqa: BLE001 — 声明 JSON 但不是：保留文本就够
                data = None
        return ProbeResult(
            ok=200 <= r.status_code < 300,
            status=r.status_code,
            json=data,
            text=text,
            content_type=ct,
            url=url,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )


def summarize_for_prompt(endpoints: list, limit: int = 25) -> str:
    """把学到的接口清单压成给模型看的一段文本。

    为什么要专门做这件事：直接把整张表丢进提示词，一个活跃系统能有几百个接口，
    提示词会被撑爆而且重点全无。这里按"被调用次数"排序取前 N 个 ——
    次数最多的是菜单、列表这类主干接口，正是生成用例时最该知道的。
    """
    if not endpoints:
        return ""
    rows = []
    for e in endpoints[:limit]:
        m = getattr(e, "method", "GET")
        p = getattr(e, "path", "")
        q = getattr(e, "query", None) or {}
        hits = getattr(e, "hits", 0)
        w = getattr(e, "writable", False)
        qs = ", ".join(f"{k}:{v}" for k, v in list(q.items())[:6])
        flags = " [写接口，用例不可直接调用]" if w else ""
        rows.append(f"- {m} {p}{('?' + qs) if qs else ''}  (被调用 {hits} 次){flags}")
    return (
        "以下后端接口是从**页面实际发出的流量**里观察到的（不是猜的）：\n"
        + "\n".join(rows)
        + "\n这些接口可用于核对数据（例如菜单、列表的真实来源）。"
        "写接口只做了解，不要直接调用。"
    )


def normalize_for_storage(url: str) -> str:
    """落库前统一归一化，避免同一条路径出现两种写法。"""
    try:
        p = urlparse(url)
    except ValueError:
        return url[:500]
    return urlunparse((p.scheme, p.netloc, normalize_path(p.path), "", "", ""))[:500]
