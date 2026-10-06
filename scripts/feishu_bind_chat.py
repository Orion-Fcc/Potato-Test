"""飞书群 chat_id 的查询与绑定助手。

为什么要有这个脚本：项目里原本拿 chat_id 的办法是「在群里 @机器人 绑定」，
但那需要单独跑 `python -m app.feishu_ws`（交互机器人进程），桌面 PotatoTest.bat
并不启动它。只做推送的话，拿 chat_id 就断在这儿了，所以用接口直接查。

用法（在 <PROJECT_DIR> 下）：

    .venv\\Scripts\\python.exe scripts\\feishu_bind_chat.py            # 列出机器人所在的群
    .venv\\Scripts\\python.exe scripts\\feishu_bind_chat.py oc_xxxxxxxx # 绑定到第一个项目
    .venv\\Scripts\\python.exe scripts\\feishu_bind_chat.py oc_xxxxxxxx "培训资源管理"

前提：已经配好 FEISHU_APP_ID / FEISHU_APP_SECRET（.env 或 系统设置 → 飞书），
且机器人已经被拉进目标群。绑定写的是数据库，立即生效，不需要重启服务。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _is_network_error(exc: BaseException) -> bool:
    """网络层失败 vs 飞书返回的业务错误 —— 两者的处理完全不同。"""
    if "All connection attempts failed" in str(exc):
        return True
    return any(
        kind in type(exc).__name__
        for kind in ("Connect", "Timeout", "Read", "Network", "RemoteProtocol", "Proxy")
    )


async def _list_chats(client) -> list[dict]:
    from app.feishu import feishu_http

    token = await client._tenant_token()
    url = f"{client._base}/open-apis/im/v1/chats?page_size=50"
    # MUST go through feishu_http(), not a bare httpx.AsyncClient: the bare client defaults
    # to trust_env=True and picks up the machine's HTTP(S)_PROXY, which on this box is a
    # rotating local port that is usually already dead by the time the script runs. The
    # result was "ConnectError: All connection attempts failed", which reads like a
    # credentials problem but is purely the proxy. feishu_http() decides this in one place
    # (FEISHU_IGNORE_PROXY, default true).
    async with feishu_http(15) as c:
        data = (
            await c.get(url, headers={"Authorization": f"Bearer {token}"})
        ).json()

    if data.get("code") != 0:
        raise RuntimeError(str(data))
    return data.get("data", {}).get("items", []) or []


def _hint(err_text: str) -> str:
    """把报错翻译成「下一步做什么」。"""
    if "230002" in err_text or "230098" in err_text:
        return "机器人不在任何群里：先去飞书里建群 → 群设置 → 群机器人 → 添加机器人。"
    if "99991672" in err_text or "permission" in err_text.lower() or "19002" in err_text:
        return (
            "缺权限：开放平台后台 → 权限管理，勾 im:chat:readonly（获取群组信息）"
            "和 im:message:send_as_bot（发送消息），然后创建新版本并发布。"
        )
    return "多半是 App Secret 抄错，或应用还没发布（版本管理 → 创建版本 → 发布）。"


def _network_hint() -> str:
    import os

    proxies = {k: v for k, v in os.environ.items() if k.lower().endswith("_proxy")}
    lines = [
        "这是网络连不到飞书，和凭证无关。本机 HTTP(S)_PROXY 是会自动失效的本地端口，",
        "        该脚本已按 FEISHU_IGNORE_PROXY 处理，所以先按下面顺序查：",
        "        1) 在浏览器里打开 https://open.feishu.cn —— 打不开就是本机网络/热点的问题；",
        "        2) 确认 .env 里 FEISHU_IGNORE_PROXY=true（默认就是 true，改了才需要看）；",
    ]
    if proxies:
        lines.append(f"        3) 当前环境里存在代理变量：{proxies}")
        lines.append(
            "           若第 1 步能打开、这里却连不上，临时去掉再试一次："
        )
        lines.append("           PowerShell:  $env:HTTP_PROXY=''; $env:HTTPS_PROXY=''")
    else:
        lines.append("        3) 当前环境没有代理变量，问题在本机到外网这一段。")
    return "\n        ".join(lines)


async def main() -> int:
    from sqlalchemy import select

    from app.db import db_session
    from app.feishu import FeishuClient, resolve_config
    from app.models import Project

    print("=" * 62)
    print("飞书群 chat_id —— 查询 / 绑定")
    print("=" * 62)

    async with db_session() as s:
        cfg = await resolve_config(s)
        projects = (await s.execute(select(Project))).scalars().all()

    if not (cfg["app_id"] and cfg["app_secret"]):
        print("\n  ✗ 卡住了：还没有 App ID / App Secret。")
        print("    去 网页 → 系统设置 → 飞书里填，或写进 .env 的 "
              "FEISHU_APP_ID / FEISHU_APP_SECRET。")
        return 1

    client = FeishuClient(cfg["app_id"], cfg["app_secret"], cfg["api_base"])

    # ---- 只列群 ----------------------------------------------------------
    if len(sys.argv) == 1:
        print("\n机器人所在的群：")
        try:
            items = await _list_chats(client)
        except Exception as exc:
            print(f"  ✗ 查询失败 → {type(exc).__name__}: {exc}")
            print(f"    {_network_hint() if _is_network_error(exc) else _hint(str(exc))}")
            return 1

        if not items:
            print("  （空）机器人还没被拉进任何群。")
            print("    去飞书：建群 → 群设置 → 群机器人 → 添加机器人，再跑一次。")
            return 1

        for i, it in enumerate(items, 1):
            current = ""
            if any(p.feishu_chat_id == it["chat_id"] for p in projects):
                current = "  ← 已绑定"
            print(f"  {i}. {it.get('name') or '(无群名)'}{current}")
            print(f"     {it['chat_id']}")

        print("\n绑定命令：")
        print(f"    .venv\\Scripts\\python.exe scripts\\feishu_bind_chat.py {items[0]['chat_id']}")
        return 0

    # ---- 绑定 ------------------------------------------------------------
    chat_id = sys.argv[1]
    if not chat_id.startswith("oc_") and not chat_id.startswith("ou_"):
        print(f"\n  ⚠ chat_id 通常以 oc_ 开头，你给的是 {chat_id!r}，确认没粘错？")

    want = sys.argv[2] if len(sys.argv) > 2 else ""
    target = None
    for p in projects:
        if want and (str(p.id) == want or p.name == want):
            target = p
            break
    if target is None and not want:
        target = projects[0] if projects else None

    if target is None:
        print(f"\n  ✗ 没找到项目 {want!r}。现有的：{[p.name for p in projects]}")
        return 1

    try:
        items = await _list_chats(client)
    except Exception as exc:
        print(f"\n  ✗ 拿不到群列表 → {type(exc).__name__}: {exc}")
        print(f"    {_network_hint() if _is_network_error(exc) else _hint(str(exc))}")
        print("    列表拿不到不影响绑定，但你得先确认这个 chat_id 是对的。")
        items = []

    name = next((i.get("name") for i in items if i["chat_id"] == chat_id), None)
    if items and name is None:
        print(f"\n  ⚠ 这个 chat_id 不在机器人所在的群里，机器人进去之前发不了消息。")

    async with db_session() as s:
        proj = await s.get(Project, target.id)
        proj.feishu_chat_id = chat_id
        await s.commit()

    print(f"\n  ✓ 已把项目「{target.name}」绑定到群「{name or chat_id}」")
    print("    写的是数据库，立即生效，不需要重启服务。")
    print("\n下一步跑自检，确认真的能发：")
    print("    .venv\\Scripts\\python.exe scripts\\feishu_push_check.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
