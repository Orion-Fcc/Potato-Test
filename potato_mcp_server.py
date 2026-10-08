"""Potato Test MCP server —— 给 WorkBuddy agent 调用的接口层。

不侵入 FastAPI 本体：本进程用 httpx 调 Potato 的 REST API（默认 127.0.0.1:18081），
把「查失败清单 / 跑运行 / 改用例 / 改判」包成 MCP 工具。

运行方式（在项目 venv 里）：
    .venv/Scripts/python.exe potato_mcp_server.py
MCP 客户端（如 WorkBuddy 连接器）用 stdio 拉起本进程即可。

鉴权：本机 AUTH_ENABLED=false，直连免 token。若开启登录，用环境变量
POTATO_COOKIE 传 tp_session 值；BASE 地址用 POTATO_BASE_URL 覆盖。

设计取舍：
- 工具返回 JSON 字符串（MCP 2.x 对 str 返回最稳，auto-detect 不出错）。
- 用例编辑 / 改判自动带审计头 x-change-source: assistant，让改动留痕进 case_change。
- 读操作零副作用；写操作（跑运行/改用例/改判）在描述里标清，agent 可见。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

import httpx
from mcp.server.mcpserver import MCPServer

# httpx 默认往 stderr 打每个请求的 INFO，MCP 子进程里会刷屏；压到 WARNING。
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

BASE_URL = os.environ.get("POTATO_BASE_URL", "http://127.0.0.1:18081").rstrip("/")

# trust_env=False：本进程是「本地 MCP → 本地 Potato 服务」的桥接，绝不能走上游代理。
# 宿主机（WorkBuddy）会注入 HTTP_PROXY/HTTPS_PROXY（透明代理，端口每次变）；httpx 默认
# trust_env=True 会读它们，把对 127.0.0.1:18081 的请求也送进代理 → 502 "upstream connect
# failed"。关掉后直连本地，才可能拿到干净的连接结果。
_client = httpx.Client(timeout=60.0, trust_env=False)


def _base_headers() -> dict:
    """本机关鉴权时返回空；开启后可用 POTATO_COOKIE 传 tp_session。"""
    h: dict = {"Accept": "application/json"}
    cookie = os.environ.get("POTATO_COOKIE", "").strip()
    if cookie:
        h["Cookie"] = f"tp_session={cookie}"
    return h


def call(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
    extra_headers: dict | None = None,
    raw: bool = False,
) -> object:
    """调一次 Potato REST。失败抛 RuntimeError（带状态码 + 前 500 字响应体）。"""
    url = BASE_URL + path
    headers = _base_headers()
    if extra_headers:
        headers.update(extra_headers)
    try:
        r = _client.request(
            method, url, params=params or None, json=body, headers=headers, timeout=60.0
        )
    except httpx.HTTPError as e:
        # 连不上后端（没起/端口错/超时）时，httpx 抛的不是 400 响应而是异常。
        # 不接住会冒成原始 traceback 给 agent，无法定位；收成一条可操作的报错。
        raise RuntimeError(
            f"Potato 后端连不上（{BASE_URL}）：{type(e).__name__}: {e}。"
            f"先确认服务已起（桌面 PotatoTest.bat → 1），或设 POTATO_BASE_URL 指向正确地址。"
        ) from e
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} {method} {path}: {r.text[:500]}")
    if raw:
        return r.text
    try:
        return r.json()
    except ValueError:
        return r.text


def jstr(data: object) -> str:
    """统一以 JSON 文本返回给 agent，保证 MCP 侧解析稳定。"""
    if isinstance(data, str):
        return data
    return json.dumps(data, ensure_ascii=False, default=str)


server = MCPServer("potato-test", "1.0", description="Potato Test 平台操作接口")

# ── 第 1 组：只读查询（零副作用）──────────────────────────────────────


@server.tool()
def list_projects() -> str:
    """列出所有项目。返回 id / name 等，后续工具大多需要 project_id。"""
    return jstr(call("GET", "/api/projects"))


@server.tool()
def list_testcases(project_id: int) -> str:
    """列出项目内全部用例（含最近一次结果状态）。"""
    return jstr(call("GET", f"/api/projects/{project_id}/testcases"))


@server.tool()
def get_testcases_by_status(project_id: int, status: str | None = None) -> str:
    """按最近一次结果状态筛用例。

    status 传 passed / failed / error / never（从没跑过），或留空取全部。
    筛的是 last_status（最近结果），不是用例本身的启用状态。"""
    data = json.loads(list_testcases(project_id))
    if not status:
        return jstr(data)
    if status == "never":
        return jstr([c for c in data if c.get("last_status") is None])
    return jstr([c for c in data if c.get("last_status") == status])


@server.tool()
def get_failure_digest(project_id: int, backfill: bool = False, limit: int = 200) -> str:
    """取该项目当前失败清单（按相同原因合并，不合并错）。

    backfill=True 会调 LLM 给历史未分类结果补根因（落库缓存，非首次不重算）；
    离线/省额度就保持 False。返回分组、每组涉及用例数、动作建议。
    """
    return jstr(
        call(
            "GET",
            f"/api/projects/{project_id}/failure-digest",
            params={"backfill": "true" if backfill else "false", "limit": limit},
        )
    )


@server.tool()
def get_case_changes(project_id: int, limit: int = 100) -> str:
    """查该项目的用例改动审计（谁改了什么字段、改前后、来自哪条清单）。"""
    return jstr(call("GET", f"/api/projects/{project_id}/case-changes", params={"limit": limit}))


@server.tool()
def list_runs(project_id: int) -> str:
    """列出项目的运行历史（新→旧），含状态/进度/耗时。"""
    return jstr(call("GET", f"/api/projects/{project_id}/runs"))


@server.tool()
def get_run(run_id: int) -> str:
    """查某次运行详情（含 drift_signals）。"""
    return jstr(call("GET", f"/api/runs/{run_id}"))


@server.tool()
def get_run_results(run_id: int) -> str:
    """取某次运行下全部用例的判定结果。"""
    return jstr(call("GET", f"/api/runs/{run_id}/results"))


@server.tool()
def get_case_results(project_id: int, case_id: int) -> str:
    """取某条用例的历史结果。"""
    return jstr(call("GET", f"/api/testcases/{case_id}/results"))


@server.tool()
def get_project_stats(project_id: int) -> str:
    """项目 KPI（通过率、失败数、最近运行等）。"""
    return jstr(call("GET", f"/api/projects/{project_id}/stats"))


# ── 第 2 组：运行控制（写操作，会真的起浏览器跑）─────────────────────


@server.tool()
def start_run(
    project_id: int,
    name: str = "run",
    case_ids: list[int] | None = None,
    tags: list[str] | None = None,
    retries: int | None = None,
    environment_id: int | None = None,
) -> str:
    """发起一次运行（会真的起浏览器跑 agent，耗时数分钟起）。

    case_ids=None 且 tags=None 时跑项目里**全部启用**的用例；
    前置条件引用的依赖用例会**自动提到前面先跑**（本系统行为，不用你管）。
    本机并发硬约束为 1（内网单通道），不要指望并发提速。
    retries：本条运行级重试次数（0-3），不传沿用全局。
    返回新建的 run 对象（含 id），配合 get_run / get_run_results 轮询。
    """
    body: dict = {"name": name}
    if case_ids is not None:
        body["case_ids"] = case_ids
    if tags is not None:
        body["tags"] = tags
    if retries is not None:
        body["retries"] = retries
    if environment_id is not None:
        body["environment_id"] = environment_id
    return jstr(call("POST", f"/api/projects/{project_id}/runs", body=body))


@server.tool()
def rerun_run(run_id: int, only: str | None = None) -> str:
    """重跑某次运行的用例。only=failing 只重跑没通过的；only=error 只重跑跑挂的；
    None=全量重跑。改完东西验证时用 failing 最省（别把通过的再烧一小时）。"""
    params = {"only": only} if only else None
    return jstr(call("POST", f"/api/runs/{run_id}/rerun", params=params))


@server.tool()
def cancel_run(run_id: int) -> str:
    """取消一次正在跑 / 待跑的运行。返回更新后的 run（status=cancelled）。"""
    return jstr(call("POST", f"/api/runs/{run_id}/cancel"))


# ── 第 3 组：用例编辑（写操作，改动自动进审计表 case_change）─────────

# 审计头：所有用例改动都打 assistant 标记，便于和"人改的"区分，可追溯。
_AUDIT_HEADERS = {"x-change-source": "assistant", "x-change-by": "workbuddy-agent"}


@server.tool()
def create_case(
    project_id: int,
    name: str,
    prompt: str,
    expected: str = "",
    case_key: str | None = None,
    module: str | None = None,
    priority: str = "P2",
    type: str = "functional",
    status: str = "active",
    owner: str | None = None,
    role: str | None = None,
    roles: list[str] | None = None,
    references: str = "",
    preconditions: str = "",
    test_data: str = "",
    start_url: str | None = None,
    tags: list[str] | None = None,
    enabled: bool = True,
) -> str:
    """新建一条用例。prompt 是用自然语言写的操作要求（真正被执行的部分），
    expected 是判定依据。case_key 留空自动分配编号（TC-NNN，项目内唯一）。
    多角色把角色填进 roles（有序列表）；单角色填 role 即可。"""
    body: dict = {
        "name": name,
        "prompt": prompt,
        "expected": expected,
    }
    for k, v in {
        "case_key": case_key, "module": module, "priority": priority, "type": type,
        "status": status, "owner": owner, "role": role, "roles": roles,
        "references": references, "preconditions": preconditions,
        "test_data": test_data, "start_url": start_url, "tags": tags, "enabled": enabled,
    }.items():
        if v is not None:
            body[k] = v
    return jstr(call("POST", f"/api/projects/{project_id}/testcases", body=body, extra_headers=_AUDIT_HEADERS))


@server.tool()
def update_case(
    case_id: int,
    name: str | None = None,
    prompt: str | None = None,
    expected: str | None = None,
    case_key: str | None = None,
    module: str | None = None,
    priority: str | None = None,
    type: str | None = None,
    status: str | None = None,
    owner: str | None = None,
    role: str | None = None,
    roles: list[str] | None = None,
    references: str | None = None,
    preconditions: str | None = None,
    test_data: str | None = None,
    data_files: object | None = None,
    data_hygiene: str | None = None,
    start_url: str | None = None,
    tags: list[str] | None = None,
    enabled: bool | None = None,
) -> str:
    """改一条用例，**只传要改的字段**（不传=不动）。改动自动写审计表。

    roles 语义要小心：显式传了 roles 就以它为准（空列表=清回单角色），
    只传 role 是单角色更新，两者都不传则不碰角色列。
    data_files 可传 dict / 数组 / JSON 字符串，保存时校验，坏了当场 422。"""
    body: dict = {}
    for k, v in {
        "name": name, "prompt": prompt, "expected": expected, "case_key": case_key,
        "module": module, "priority": priority, "type": type, "status": status,
        "owner": owner, "role": role, "roles": roles, "references": references,
        "preconditions": preconditions, "test_data": test_data, "data_files": data_files,
        "data_hygiene": data_hygiene, "start_url": start_url, "tags": tags, "enabled": enabled,
    }.items():
        if v is not None:
            body[k] = v
    return jstr(call("PUT", f"/api/testcases/{case_id}", body=body, extra_headers=_AUDIT_HEADERS))


@server.tool()
def delete_case(case_id: int, project_id: int | None = None, dry_run: bool = False) -> str:
    """删一条用例，并级联清掉它的全部历史结果（run_result）。删了没挽回余地。

    强烈建议两步走：
    1. 先 dry_run=True（可选带 project_id 让预览带出用例名/编号）——**不删任何东西**，
       只回「这条用例是什么 + 会连带清掉多少条历史结果」，供你确认 case_id 没拿错。
    2. 确认无误再 dry_run=False 真删。

    不新增后端端点：预览用现有只读接口组合（/testcases/{cid}/results 数历史结果，
    /projects/{pid}/testcases 取用例名）。真删走 DELETE /testcases/{cid}（后端已做
    404/权限校验 + 级联）。"""
    if dry_run:
        preview: dict = {"dry_run": True, "case_id": case_id}
        # 先探这条用例有没有历史结果；端点 404/异常 → 多半是 case_id 拿错或不存在。
        try:
            results = call("GET", f"/api/testcases/{case_id}/results")
            preview["case_exists"] = True
            preview["history_results_will_be_deleted"] = (
                len(results) if isinstance(results, list) else 0
            )
        except RuntimeError as e:
            preview["case_exists"] = False
            preview["note"] = f"未找到该用例或其历史结果：{e}"
            return jstr(preview)
        # 给了项目就顺手把这条用例本身的信息带出来（name/case_key/…），预览更好认。
        if project_id is not None:
            for c in call("GET", f"/api/projects/{project_id}/testcases"):
                if c.get("id") == case_id:
                    preview.update(
                        {k: c.get(k) for k in ("name", "case_key", "module", "priority", "enabled")}
                    )
                    break
        preview["hint"] = (
            "以上为将被删除/级联清掉的内容。确认 case_id 无误后，再调一次 dry_run=False 真删。"
        )
        return jstr(preview)
    return jstr(call("DELETE", f"/api/testcases/{case_id}", extra_headers=_AUDIT_HEADERS))


# ── 第 4 组：人工改判（覆盖 AI 结论，写审计字段）─────────────────────


@server.tool()
def override_result(result_id: int, status: str | None = None, reason: str | None = None) -> str:
    """人工改判一条结果的结论。status 只接受 "passed"/"failed"。

    改判直接覆盖 AI 判定，且会存下 AI 原判供撤销（original_status）。
    报表 / KPI / 重跑逻辑都会跟着改后的结论走。"""
    body: dict = {}
    if status is not None:
        if status not in ("passed", "failed"):
            raise ValueError('status 只允许 "passed" / "failed"（撤销改判请传 clear=True）')
        body["status"] = status
    if reason:
        body["reason"] = reason
    return jstr(call("PATCH", f"/api/results/{result_id}", body=body, extra_headers=_AUDIT_HEADERS))


@server.tool()
def revert_result_override(result_id: int) -> str:
    """撤销一次人工改判，恢复 AI 的原始结论（original_status）。"""
    return jstr(call("PATCH", f"/api/results/{result_id}", body={"clear": True}, extra_headers=_AUDIT_HEADERS))


def main() -> None:
    asyncio.run(server.run_stdio_async())


if __name__ == "__main__":
    main()
