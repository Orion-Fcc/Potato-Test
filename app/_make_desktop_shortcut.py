"""在桌面创建 Potato Test 专用浏览器的快捷方式。

为什么需要这个文件
------------------
Potato Test 默认拉起的浏览器可能是一个**免安装绿色版**（Playwright 自带的 Chromium：
没有开始菜单项、没有桌面图标、没注册成默认浏览器）。它只被 `launch()` 拉起来、用完
就关，所以从外面完全看不到它—— 用户会以为「配置里写的浏览器不存在」。

给一个手动入口的价值：用户可以**自己先打开它登一次内网系统**，之后 Potato Test
跑用例时登录态就在 profile 里，13 个角色切换那条路径的固定成本直接省掉。

★ 目标可执行文件**问browser_binary 要**，不写死路径
----------------------------------------------------
原来这里硬编码 `%LOCALAPPDATA%\\ms-playwright\\chromium-1243\\...`，于是 2026-10-07
把候选从 chromium 换成 chrome 正式版之后，这个快捷方式还指着旧的 Chromium ——
用户打开的是一个执行层根本不再用的程序，而"我明明改了配置"这种事最难查。
路径必须来自同一个 `resolve_browser_executable`，否则两个地方迟早分叉。

参数里三件事值得说明
--------------------
* `--user-data-dir` 指向一个**专属**目录（不是日常浏览器的 User Data）。这是"两个
  浏览器同时开不打架"的全部原因 —— 共用 UserData 就会出现「这边登录了、另一边故障」。
* `--no-proxy-server`：宿主机由外部工具注入透明代理（HTTP_PROXY 端口每次都不同），
  浏览器会读它，于是**内网 10.x 的请求也被送去代理**，一失效整页就挂在
  「正在加载中请稍后」。被测系统在内网，本就该直连。
* `--remote-debugging-port`：开了这个，Potato Test 才能 attach 上来**继承这里的登录态**。
  只监听 127.0.0.1，外部机器连不上 —— `cdp_endpoint` 也硬性只允许回环地址。

不用 COM 的 `WScript.Shell` 之外的手段：Windows 的 .lnk 就是标准文件格式，
这里用 pywin32 写入是常规做法；但**若沙箱禁止 COM**（本机沙箱确实拦了
`New-Object -ComObject`），请用 `powershell` 里的等价脚本或手工拖一个快捷方式。
"""

from __future__ import annotations

import os
import sys

import win32com.client as win32

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PROFILE = os.path.expandvars(r"%LOCALAPPDATA%\potato-browser-profile")
CDP_PORT = 9333


def main() -> int:
    from app import browser_binary
    from app.config import get_settings

    s = get_settings()
    exe = browser_binary.resolve_browser_executable(
        s.browser_executable, s.browser_candidates
    )
    if not exe or not os.path.isfile(exe):
        print(f"[FAIL] 执行层当前解析不到可用的浏览器（{s.browser_candidates!r}）")
        print("       浏览器没装，或 app/config.py 里的候选名写错了。")
        return 1
    print(f"执行层当前使用的浏览器：{browser_binary.describe(exe)}")
    print(f"  {exe}")

    os.makedirs(PROFILE, exist_ok=True)
    desktop = os.path.join(os.path.expandvars(r"%USERPROFILE%"), "Desktop")
    os.makedirs(desktop, exist_ok=True)
    lnk = os.path.join(desktop, "Potato Test 浏览器.lnk")

    shell = win32.Dispatch("WScript.Shell")
    lnk_obj = shell.CreateShortcut(lnk)
    lnk_obj.TargetPath = exe
    lnk_obj.Arguments = (
        f'--user-data-dir="{PROFILE}" '
        f"--remote-debugging-port={CDP_PORT} "
        "--no-proxy-server "
        "--no-first-run "
        "--no-default-browser-check"
    )
    lnk_obj.WorkingDirectory = os.path.dirname(exe)
    lnk_obj.IconLocation = exe + ",0"
    lnk_obj.Description = (
        "Potato Test 专用浏览器（独立 profile，与日常浏览器互不影响；直连内网不走代理）"
    )
    lnk_obj.WindowStyle = 1
    lnk_obj.Save()

    # 回读校验：CreateShortcut 返回的是**内存里的新对象**，写没写成功只有重开才知道。
    v = shell.CreateShortcut(lnk)
    ok = (
        os.path.exists(lnk)
        and os.path.normcase(v.TargetPath) == os.path.normcase(exe)
        and "potato-browser-profile" in v.Arguments
        and f"remote-debugging-port={CDP_PORT}" in v.Arguments
        and "--no-proxy-server" in v.Arguments
    )
    print(f"\n[{'OK' if ok else 'FAIL'}] {lnk}")
    print(f"  target  : {v.TargetPath}")
    print(f"  args    : {v.Arguments}")
    print(f"  profile : {PROFILE}")
    print(f"  size    : {os.path.getsize(lnk)} bytes")
    if not ok:
        return 1
    print()
    print("  下一步：双击打开它，登录内网系统一次。之后在 .env 里写")
    print(f"        BROWSER_CDP_ENDPOINT={CDP_PORT}")
    print("  Potato Test 就会 attach 到这个窗口并继承登录态。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())