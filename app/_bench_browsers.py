"""实测两三个浏览器的启动与内网访问耗时，回答"哪个更快"。

为什么用脚本而不是凭印象
------------------------
「更快」在这条链路上有两个完全不同的含义，量出来可能相反：

* **进程启动**：决定每条用例的固定开销。但 Potato Test 有浏览器复用池
  （browser_reuse），浏览器长驻不关，所以这一项**一轮只付一次**，不是每条用例付。
* **页面渲染**：决定每条用例的每一步。实测这台机器上内网 SPA 首屏要 5-8 秒，
  这才是真正的耗时大头，而它取决于 Blink 版本与 CPU，不是浏览器包多大。

所以只看"启动 0.6 秒还是 1.4 秒"就下结论，等于拿一个一轮付一次的数字去解释
每条用例都在付的那个数字。

测法
----
* 走 `launch_persistent_context`，因为 browser-use 0.13 的 Browser 底层也是
  Playwright 的 persistent context（给 user_data_dir），用别的 API 量出来的数
  不能代表实际执行路径。
* headless=True，与 Potato Test 一致。
* 每个浏览器跑 N 次取中位数：首次启动要为共享库、字体缓存、沙箱初始化付费，
  单次结果会被这些一次性成本主导。
* 每个浏览器用**自己的空 profile 目录**，否则第二个测的就是热缓存，
  数字会漂亮得不真实。
"""

from __future__ import annotations

import asyncio
import os
import statistics
import tempfile
import time

from playwright.async_api import async_playwright

TARGET = os.environ.get("PB_TARGET")
if not TARGET:
    # 刻意不写默认值：这里原本硬编码了一个内网地址当默认目标，而本仓库是**公开**的。
    # 基准测试要测的永远是"你正在用的那个站点"，必须显式传进来。
    raise SystemExit(
        "请设置目标站点：PB_TARGET=http://<host:port> python -m app._bench_browsers"
    )
ROUNDS = int(os.environ.get("PB_ROUNDS", "3"))

# 候选：name -> 可执行文件路径的环境变量名
CANDIDATES = [
    ("Playwright Chromium", r"%LOCALAPPDATA%\ms-playwright\chromium-1243\chrome-win64\chrome.exe"),
    ("Chrome (正式安装)", r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
    ("Edge (正式安装)", r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
    ("Edge Dev", r"%LOCALAPPDATA%\Microsoft\EdgeDev\Application\msedge.exe"),
]

# 与 Potato Test 的 make_browser 保持一致：不继承宿主机透明代理
ARGS = ["--no-proxy-server", "--no-sandbox", "--disable-blink-features=AutomationControlled"]


async def bench(name: str, exe: str) -> dict | None:
    if not exe or not os.path.isfile(exe):
        print(f"  [skip] {name:<22} 未安装")
        return None
    launches: list[float] = []
    gotos: list[float] = []
    for _ in range(ROUNDS):
        with tempfile.TemporaryDirectory(prefix="pb_bench_") as prof:
            try:
                async with async_playwright() as p:
                    t0 = time.monotonic()
                    ctx = await p.chromium.launch_persistent_context(
                        prof,
                        executable_path=exe,
                        headless=True,
                        args=ARGS,
                    )
                    t1 = time.monotonic()
                    page = await ctx.new_page()
                    await page.goto(TARGET, wait_until="domcontentloaded", timeout=45000)
                    t2 = time.monotonic()
                    await ctx.close()
                    launches.append(t1 - t0)
                    gotos.append(t2 - t1)
            except Exception as exc:  # noqa: BLE001
                print(f"  [fail] {name:<22} {type(exc).__name__}: {str(exc)[:80]}")
                return None
    return {
        "name": name,
        "exe": exe,
        "launch_med": statistics.median(launches),
        "goto_med": statistics.median(gotos),
        "launch_all": launches,
        "goto_all": gotos,
    }


async def main() -> int:
    print(f"目标: {TARGET}   每项跑 {ROUNDS} 次取中位数   (headless)")
    print()
    rows = []
    for name, raw in CANDIDATES:
        exe = os.path.expandvars(raw)
        r = await bench(name, exe)
        if r:
            rows.append(r)
            print(
                f"  {r['name']:<22} launch={r['launch_med']:.2f}s   goto={r['goto_med']:.2f}s"
                f"   launch_all={[round(x,2) for x in r['launch_all']]}"
            )
        print()

    if not rows:
        print("没有可测的浏览器")
        return 1

    print("=" * 78)
    print(f"{'浏览器':<22}{'启动':>8}{'内网首屏':>12}{'一轮N条用例(复用)':>22}")
    print("-" * 78)
    for r in rows:
        # 复用模式下启动只付一次；页面渲染每条用例都要付（按每条 6 步估）
        total = r["launch_med"] + r["goto_med"] * 6
        print(f"{r['name']:<22}{r['launch_med']:>7.2f}s{r['goto_med']:>11.2f}s{total:>19.0f}s")
    print("=" * 78)
    print("注: 最后一列按「启动只付 1 次 + 每条用例 6 个页面操作」估算，用于看量级差异。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))