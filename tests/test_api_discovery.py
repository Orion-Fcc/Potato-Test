"""后端接口探索：发现 + 只读重放（2026-10-09）。

用户的需求：
  「我希望这个系统也具有探索后端的能力……他需要有探索后端的能力去拿到这个菜单或者
   相关的数据。」
选定的边界：
  「从页面流量里自学接口」+「探索只读，写操作必须显式声明」。

所以这组测试守两类东西，缺一不可：

**A. 能不能学到真接口** —— 菜单、列表这些必须被识别出来，静态资源和埋点必须被排除。
一张混着 200 个 `.js` 的接口清单，用户看一眼就不想再打开。

**B. 安全线不能破** —— 这是重点。探索会真的朝后端发请求，所以：
   * 写方法（POST/PUT/DELETE）**必须**被拒；
   * 不许打出项目自己的 origin（否则就是拿项目凭据当 SSRF 跳板）；
   * 参数**只存形状不存值**（token、手机号这类绝不能落库）；
   * 请求头一律丢弃。
"""

from __future__ import annotations

import pytest

from app.api_discovery import (
    READ_METHODS,
    Endpoint,
    Recorder,
    looks_like_api,
    normalize_path,
    parse_post_data,
    parse_url,
    value_shape,
)
from app.api_probe import (
    MAX_PROBES_PER_RUN,
    ProbeRefused,
    Prober,
    build_url,
    method_allowed,
    summarize_for_prompt,
)

ORIGIN = "http://10.0.0.5:8600"


# ---------------------------------------------------------------------------
# A. 接口识别
# ---------------------------------------------------------------------------


def test_recognises_api_paths_and_xhr() -> None:
    assert looks_like_api(f"{ORIGIN}/api/menu", ORIGIN)
    assert looks_like_api(f"{ORIGIN}/rest/user/list", ORIGIN)
    assert looks_like_api(f"{ORIGIN}/graphql", ORIGIN)
    assert looks_like_api(f"{ORIGIN}/anything", ORIGIN, resource_type="XHR")
    assert looks_like_api(f"{ORIGIN}/system/user/list", ORIGIN)  # multi-segment fallback


def test_excludes_static_assets_and_noise() -> None:
    """★ 这条守的是"清单能不能用"。

    页面会发几百个 .js/.css/字体请求。它们混进接口表之后，用户看到的就是
    "这个系统有 400 个接口"，而真正的业务接口（菜单、列表）被淹在里面。
    """
    for url in (
        f"{ORIGIN}/static/js/app.js",
        f"{ORIGIN}/assets/index-2hFq4OPG.css",
        f"{ORIGIN}/fonts/x.woff2",
        f"{ORIGIN}/img/logo.png",
        f"{ORIGIN}/favicon.ico",
        f"{ORIGIN}/sockjs/123/abc/websocket",
        f"{ORIGIN}/service-worker.js",
        f"{ORIGIN}/hm.baidu.com/hm.js",
        "data:image/png;base64,AAAA",
        "",
    ):
        assert not looks_like_api(url, ORIGIN), f"不该收进来：{url}"


def test_ids_are_normalised_so_one_endpoint_is_one_row() -> None:
    """★ 不归一化的后果很具体：翻两页、点开三条详情，就能造出十几条"不同"接口。"""
    assert normalize_path("/api/user/1024/detail") == "/api/user/{id}/detail"
    assert normalize_path("/api/user/2048/detail") == "/api/user/{id}/detail"
    assert normalize_path("/api/order/550e8400-e29b-41d4-a716-446655440000") == "/api/order/{id}"
    assert normalize_path("/api/menu") == "/api/menu"


def test_identical_endpoints_accumulate_hits_instead_of_duplicating() -> None:
    r = Recorder(ORIGIN)
    r.observe(f"{ORIGIN}/api/user/1/detail", "GET", "xhr")
    r.observe(f"{ORIGIN}/api/user/2/detail", "GET", "xhr")
    r.observe(f"{ORIGIN}/api/user/3/detail?tab=a", "GET", "xhr")
    eps = r.endpoints()
    assert len(eps) == 1, [e.path for e in eps]
    assert eps[0].hits == 3
    assert eps[0].query == {"tab": "str"}, "后续观察要补齐新出现的参数"


def test_endpoints_are_ordered_by_how_often_they_are_used() -> None:
    """次数多的更可能是主干接口（菜单、列表），用户/模型都该先看到它们。"""
    r = Recorder(ORIGIN)
    r.observe(f"{ORIGIN}/api/rare", "GET", "xhr")
    for _ in range(5):
        r.observe(f"{ORIGIN}/api/menu", "GET", "xhr")
    assert [e.path for e in r.endpoints()][0] == "/api/menu"


# ---------------------------------------------------------------------------
# B. 不落敏感值
# ---------------------------------------------------------------------------


def test_query_values_are_stored_as_shapes_not_values() -> None:
    """★ 这是"不落库敏感值"的实现点。

    `?token=eyJhbGciOi...` 只记成 `token: opaque`。形状足够让模型知道要传什么类型，
    而真实值（凭据、手机号、客户名）一个字都进不了库。
    """
    _, _, q = parse_url(f"{ORIGIN}/api/list?page=2&size=20&kw=张三&active=true&token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abcdefghij")
    assert q["page"] == "int"
    assert q["size"] == "int"
    assert q["active"] == "bool"
    assert q["kw"] == "str"
    assert q["token"] == "opaque", "长随机串必须只报 opaque，不能细说形状"
    assert "张三" not in str(q), "参数值泄漏进库里了"


def test_value_shape_classification() -> None:
    assert value_shape("42") == "int"
    assert value_shape("-3") == "int"
    assert value_shape("1.5") == "float"
    assert value_shape("true") == "bool"
    assert value_shape("2026-10-09") == "date"
    assert value_shape("") == "empty"
    assert value_shape("2026-10-09T10:00:00Z") == "date"
    assert value_shape("a" * 40) == "opaque"


def test_post_body_records_field_names_only() -> None:
    """写接口的字段名很有用（知道该填什么），字段值几乎一定含真实数据。"""
    fields = parse_post_data('{"customerName":"张三","phone":"13800138000","qty":2}', "application/json")
    assert fields == ["customerName", "phone", "qty"]
    assert "张三" not in str(fields)
    assert "13800138000" not in str(fields)


def test_post_body_handles_form_and_garbage() -> None:
    assert parse_post_data("a=1&b=2", "application/x-www-form-urlencoded") == ["a", "b"]
    assert parse_post_data("not json", "application/json") == []
    assert parse_post_data(None) == []
    assert parse_post_data("{bad", "application/json") == []


def test_headers_are_never_recorded() -> None:
    """Endpoint 上根本没有 headers 字段 —— 这条测试把它钉住，防止后人"顺手"加上。"""
    e = Endpoint(method="GET", path="/api/menu")
    assert not hasattr(e, "headers"), "接口表里不该有请求头（会带上 Cookie/Authorization）"
    assert "cookie" not in str(e.as_dict()).lower()


# ---------------------------------------------------------------------------
# B2. 只读边界（重放层）
# ---------------------------------------------------------------------------


def test_write_methods_are_refused() -> None:
    """★ 用户选的边界：「探索只读，写操作必须显式声明」。"""
    for m in ("GET", "HEAD", "OPTIONS", "get", "head"):
        assert method_allowed(m), m
    for m in ("POST", "PUT", "PATCH", "DELETE", "post", "delete"):
        assert not method_allowed(m), f"{m} 必须被拒"


def test_discovered_write_endpoints_are_registered_but_flagged() -> None:
    """写接口要**登记**（用户需要知道系统有哪些写接口），但标记 writable。

    只登记不重放：探索阶段的目的是看懂系统，而写请求一旦发出去就可能
    在测试环境里建/删真数据。
    """
    r = Recorder(ORIGIN)
    r.observe(f"{ORIGIN}/api/order/create", "POST", "xhr", post_data='{"qty":1}')
    r.observe(f"{ORIGIN}/api/menu", "GET", "xhr")
    by_path = {e.path: e for e in r.endpoints()}
    assert by_path["/api/order/create"].writable is True
    assert by_path["/api/order/create"].body_fields == ["qty"]
    assert by_path["/api/menu"].writable is False


@pytest.mark.parametrize(
    "bad",
    [
        "//evil.com/x",
        "http://evil.com/x",
        "https://evil.com/steal",
        "/api/../../admin",
        "/api/ok/../../../etc/passwd",
    ],
)
def test_url_builder_refuses_to_leave_the_project_origin(bad: str) -> None:
    """★ 这是防 SSRF 的那道闸：只允许打在项目自己的后端上。

    `//evil.com/x` 和 `http://evil.com/x` 被**显式拒绝**而不是"当成路径拼接"。
    拼接本身在这里恰好不会打出去，但那是一种隐式的语义转换 ——
    在安全边界上"猜调用方的意思"是最危险的做法。
    """
    with pytest.raises(ProbeRefused):
        build_url(ORIGIN, bad)


def test_url_builder_accepts_normal_paths() -> None:
    assert build_url(ORIGIN, "/api/menu") == f"{ORIGIN}/api/menu"
    assert build_url(ORIGIN, "api/menu") == f"{ORIGIN}/api/menu"
    assert build_url(ORIGIN, "/api/list", {"page": 2}) == f"{ORIGIN}/api/list?page=2"


def test_url_builder_quotes_parameter_values() -> None:
    """参数值要转义，否则一个 `&` 就能塞进额外参数。"""
    u = build_url(ORIGIN, "/api/list", {"kw": "a&b=c"})
    assert u == f"{ORIGIN}/api/list?kw=a%26b%3Dc"


def test_url_builder_rejects_an_empty_origin() -> None:
    with pytest.raises(ProbeRefused):
        build_url("", "/api/menu")


# ---------------------------------------------------------------------------
# 给模型看的摘要
# ---------------------------------------------------------------------------


def test_prompt_summary_is_bounded_and_marks_writes() -> None:
    """整张表丢进提示词会撑爆它（活跃系统几百个接口）。摘要必须有界并标注写接口。"""
    eps = [
        Endpoint(method="GET", path=f"/api/list/{i}", query={"page": "int"}, hits=10 - i)
        for i in range(40)
    ]
    eps.append(Endpoint(method="POST", path="/api/order", writable=True, hits=1))
    s = summarize_for_prompt(eps, limit=5)
    lines = [ln for ln in s.splitlines() if ln.startswith("- ")]
    assert len(lines) == 5, f"摘要没有按 limit 截断：{len(lines)} 行"
    assert "写接口" in summarize_for_prompt(eps, limit=40), "写接口必须被标注出来"


def test_prompt_summary_empty_when_nothing_discovered() -> None:
    assert summarize_for_prompt([]) == ""


def test_recorder_never_raises_on_bad_input() -> None:
    """它挂在浏览器的请求回调上 —— 抛出去会把整个用例带崩。

    而它只是一个附加能力：观察失败可以什么都没有，但绝不能让用例失败。
    """
    r = Recorder(ORIGIN)
    for junk in (None, "", "http://[bad", "javascript:alert(1)"):
        r.observe(junk)  # type: ignore[arg-type]
    assert r.seen >= 0  # 没抛就算过


def test_prober_caps_requests_per_run() -> None:
    """没有上限的话，一个用例可以对着后端刷几千次 —— 内网里很容易被当成攻击。"""
    p = Prober(ORIGIN)
    p.used = MAX_PROBES_PER_RUN
    import asyncio

    res = asyncio.run(p.get("/api/menu"))
    assert res.ok is False
    assert "上限" in res.error


def test_prober_refuses_a_write_path_without_sending() -> None:
    """路径层面也拦一道：即使有人绕过 method_allowed，非 / 开头的逃逸写法也发不出去。"""
    import asyncio

    p = Prober(ORIGIN)
    res = asyncio.run(p.get("//evil.com/x"))
    assert res.ok is False
    assert "拒绝" in res.error
    assert p.used == 0, "被拒的请求不该计入使用次数"


def test_read_methods_constant_is_the_single_source_of_truth() -> None:
    """白名单只有一处定义 —— 两处就会漂，而漂的那次一定是安全那侧。"""
    assert READ_METHODS == frozenset({"GET", "HEAD", "OPTIONS"})
