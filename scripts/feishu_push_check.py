"""飞书推送 —— 一键自检脚本。

用途：在真的跑一遍测试之前，先确认「App 凭证 → 群会话 → 卡片」这条链路是通的。

用法（在 D:\\<WORK_DIR>\\Potato_Test 下）：
    .venv\\Scripts\\python.exe scripts\\feishu_push_check.py

它会依次做 5 件事，任何一步失败都会明确告诉你卡在哪：
    1. 读配置，确认 ENABLE_FEISHU 与 App ID / Secret 都在，并打印推送节奏
    2. 用 app_id/app_secret 换取 tenant_access_token（凭证对不对）
    3. 往目标 chat_id 发一张测试卡片（机器人是否在群里、chat_id 对不对）
    4. 打印项目当前的绑定状态
    5. 读群消息权限（主动查「状态」的前提；只做推送时无关紧要）
    6. 检查群机器人进程在不在（只有它在跑，群里 @机器人 问「状态」才有回应）

chat_id 可以临时用命令行参数覆盖，方便先试通再写进配置：
    .venv\\Scripts\\python.exe scripts\\feishu_push_check.py oc_xxxxxxxx
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _is_network_error(exc: BaseException) -> bool:
    """True when the failure is reaching Feishu, not Feishu rejecting our request.

    A credentials problem comes back as HTTP 200 with code!=0 (raised as RuntimeError
    carrying the JSON). A network problem never gets a response at all, so we recognise
    it by exception type / message instead of guessing from the wording alone.
    """
    if "All connection attempts failed" in str(exc):
        return True
    return any(
        kind in type(exc).__name__
        for kind in ("Connect", "Timeout", "Read", "Network", "RemoteProtocol", "Proxy")
    )


async def main() -> int:
    from app.db import db_session
    from app.feishu import FeishuClient, resolve_config
    from app.models import Project

    override_chat = sys.argv[1] if len(sys.argv) > 1 else ""

    print("=" * 62)
    print("飞书推送自检")
    print("=" * 62)

    # ---- 1. 配置 ----------------------------------------------------------
    async with db_session() as s:
        cfg = await resolve_config(s)
        projects = (await s.execute(__import__("sqlalchemy").select(Project))).scalars().all()

    from app.config import get_settings

    st = get_settings()

    print(f"\n[1/6] 配置")
    print(f"      ENABLE_FEISHU     = {st.enable_feishu}")
    print(f"      app_id            = {cfg['app_id'] or '(空)'}")
    print(f"      app_secret        = {'已设置' if cfg['app_secret'] else '(空)'}")
    print(f"      api_base          = {cfg['api_base']}")
    print("      —— 推送节奏 ——")
    print(f"      里程碑             = {st.feishu_milestones or '(关)'}  %")
    print(f"      定时心跳           = {st.feishu_progress_interval_sec or 0} 秒（0=关）")
    print(f"      首次失败告警       = {st.feishu_push_first_failure}")
    print(f"      开始卡             = {st.feishu_push_start}")

    if not st.enable_feishu:
        print("\n  ✗ 卡住了：ENABLE_FEISHU 还是 false。")
        print("    改成 ENABLE_FEISHU=true 后重启服务（或去 系统设置 → 飞书 里配）。")
        return 1
    if not (cfg["app_id"] and cfg["app_secret"]):
        print("\n  ✗ 卡住了：App ID 或 App Secret 没配。")
        print("    去 系统设置 → 飞书机器人 填，或写进 .env 的 FEISHU_APP_ID / FEISHU_APP_SECRET。")
        return 1
    print("      ✓ 配置齐了")

    # ---- 2. 取 token ------------------------------------------------------
    print(f"\n[2/6] 用 app_id / app_secret 换 tenant_access_token")
    client = FeishuClient(cfg["app_id"], cfg["app_secret"], cfg["api_base"])
    try:
        token = await client._tenant_token()
    except Exception as exc:
        # Two very different failures arrive here, and blending the advice is what made
        # someone re-check perfectly good credentials: this machine's HTTP(S)_PROXY port
        # rotates and goes dead, which surfaces as "All connection attempts failed" -- a
        # network failure that reads like a credentials rejection.
        print(f"\n  ✗ 卡住了：取 token 失败 → {type(exc).__name__}: {exc}")
        if _is_network_error(exc):
            print("    这是【网络】问题，不是凭证对错的问题：")
            print("    - 浏览器打开 https://open.feishu.cn 看能不能通")
            print("    - 本机设了 HTTP(S)_PROXY 且端口会变化/失效，代码已默认忽略它")
            print("      （FEISHU_IGNORE_PROXY=true）；确需代理时改成 false")
            print("    - 公司网络屏蔽时换个网络再试")
        else:
            print("    多半是 App ID / Secret 抄错，或应用没发布/没开权限。")
        return 1
    if not token:
        print("\n  ✗ 卡住了：token 为空（凭证被飞书拒绝）。")
        return 1
    print(f"      ✓ 拿到 token（{token[:10]}…）")

    # ---- 3. 发测试卡片 ----------------------------------------------------
    print(f"\n[3/6] 往群里发一张测试卡片")
    target = override_chat or (projects[0].feishu_chat_id if projects else None)
    if not target:
        print("\n  ✗ 卡住了：没有目标群 chat_id。")
        print("    两种给法：")
        print("      a) 命令行临时指定：python scripts\\feishu_push_check.py oc_xxxxxxxx")
        print("      b) 写进项目：网页 → 项目设置 → 飞书群 chat_id，或群里发「@机器人 绑定 项目名」")
        return 1
    print(f"      目标 chat_id = {target}")

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "✅ 飞书推送自检成功"},
            "template": "green",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        "**链路已打通**\n"
                        "以后这个群只在这些时刻收到卡片：\n"
                        f"进度到 {'/'.join(str(m) + '%' for m in st.feishu_milestones) or '(未设)'} · "
                        "第一次出现未通过 · 跑完的结果卡\n"
                        "中间想看进度？@机器人 问「状态」即可"
                    ),
                },
            }
        ],
    }
    try:
        await client.send_card(target, card)
    except Exception as exc:
        print(f"\n  ✗ 卡住了：发卡片失败 → {type(exc).__name__}: {exc}")
        if _is_network_error(exc):
            print("    这是网络连不上飞书，不是群里或权限的问题（见上一步的排查）。")
        else:
            print("    常见原因：机器人不在这个群里；chat_id 不是这个群的；")
            print("    或应用权限里没勾「发送消息」(im:message:send_as_bot)。")
        return 1
    print("      ✓ 已发出 —— 去看手机飞书，应该有一条绿色卡片")

    # ---- 4. 项目绑定状态 --------------------------------------------------
    print(f"\n[4/6] 项目绑定状态")
    for p in projects:
        mark = "✓" if p.feishu_chat_id else "✗"
        print(f"      [{mark}] #{p.id} {p.name}  chat_id={p.feishu_chat_id or '(未绑定)'}")
    if projects and not projects[0].feishu_chat_id and not override_chat:
        print("\n  提示：项目还没绑 chat_id，跑完的卡片不知道该发去哪。")
        print("        网页 → 项目设置 → 填「飞书群 chat_id」保存即可。")

    # ---- 5. 读消息权限（主动查「状态」的前提） -----------------------------
    # 只做推送时这一步无关紧要；要在群里问「状态」，机器人必须能读到群消息。
    # 缺权限时飞书会把"需要哪个 scope"写在响应里，直接把它打印出来，
    # 免得去猜该勾哪个。
    print(f"\n[5/6] 读取群消息的权限（主动查「状态」需要）")
    from app.feishu import feishu_http

    can_read_group = False
    try:
        tok = await client._tenant_token()
        url = f"{cfg['api_base']}/open-apis/im/v1/messages"
        params = {
            "container_id_type": "chat",
            "container_id": target,
            "page_size": 1,
            "sort_type": "ByCreateTimeAsc",
        }
        async with feishu_http(10) as http:
            resp = (
                await http.get(url, headers={"Authorization": f"Bearer {tok}"}, params=params)
            ).json()
    except Exception as exc:
        print(f"      ✗ 请求没发出去 → {type(exc).__name__}: {exc}")
        resp = None

    if resp is not None:
        if resp.get("code") == 0:
            can_read_group = True
            print("      ✓ 能读到群消息 —— 主动查可用")
        else:
            can_read_group = False
            print(f"      ✗ 读不到群消息（code={resp.get('code')}）")
            scopes = [
                v.get("subject")
                for v in ((resp.get("error") or {}).get("permission_violations") or [])
                if v.get("subject")
            ]
            code = resp.get("code")
            if scopes:
                print(f"        缺权限，下面任选一个开通即可：{', '.join(scopes)}")
            elif code == 230027:
                # 230027 = Lack of necessary permissions。它和 99991672 的区别很重要：
                # 99991672 是「接口级权限没开」，会把缺的 scope 列在响应里；
                # 230027 通常是接口权限已开、但少了「群组消息」这一层 ——
                # 飞书文档写明「获取会话历史消息」默认只能读单聊(p2p)，
                # 读群消息还必须额外拥有「获取群组中所有消息」。
                print("        权限还不够，但缺的不是基础读消息权限（那个已经开了）。")
                print("        「获取会话历史消息」默认只能读单聊，读群消息必须额外开通：")
                print("          · 获取群组中所有消息  im:message.group_msg  ← 就是这个")
                print("        也可能同时需要 获取群组中所有消息 之类的群级别 scope，")
                print("        在开放平台权限管理里搜「群」逐个确认即可。")
            else:
                print("        （响应里没给出具体 scope，多半是应用没重新发版本/审批没过）")
            print("        飞书开放平台 → 权限管理 → 开通后要【创建版本 → 申请发布 → 等审批】才生效。")
            print("        没开这个权限不影响自动推送，只是群里问「状态」没人回。")

    # ---- 6. 群机器人进程 --------------------------------------------------
    # 主动查「状态」依赖这个进程常驻；它不跑不影响上面的自动推送，
    # 所以这里只提示、不返回失败码。
    print(f"\n[6/6] 群机器人进程（主动查「状态」靠它）")
    pid_file = ROOT / ".feishu_ws.pid"
    pid = pid_file.read_text(encoding="utf-8").strip() if pid_file.exists() else ""
    alive = False
    if pid.isdigit():
        import subprocess

        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FI", "IMAGENAME eq pythonw.exe", "/NH"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            alive = "pythonw.exe" in (out.stdout or "").lower()
        except Exception:
            alive = False
    if alive:
        if can_read_group:
            print(f"      ✓ 在跑（PID {pid}）—— 群里 @机器人 问「状态」会有回应")
        else:
            # 进程活着但读不到群消息 —— 此时说"会有回应"是错的（实测误导过一次）：
            # 机器人收不到那条 @ 消息，所以它根本不知道有人在问，表现就是"问了一点反应都没有"。
            print(f"      ✓ 在跑（PID {pid}），但**问「状态」不会有回应**")
            print("        原因不在这个进程，而是上一步的读群消息权限没开（230027）。")
            print("        机器人收不到你们 @它的消息，所以它不知道有人在问 —— 不是卡死。")
            print("        自动推送不受影响（那条链路是主动发，不需要读权限）。")
    else:
        print("      ✗ 没在跑（自动推送不受影响，但问「状态」没人回）")
        print("        启动：桌面 PotatoTest.bat 选「1 启动」会一并拉起它；")
        print("        或手动 .venv\\Scripts\\pythonw.exe -m app.feishu_poll")
        print("        看日志：set POTATO_LOG_LEVEL=INFO 后用 python.exe（不是 pythonw）跑。")
        print("        注意：第 5 步的读消息权限没开的话，光把进程跑起来照样收不到消息。")

    print("\n" + "=" * 62)
    print("检查完毕。可以跑测试了。")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
