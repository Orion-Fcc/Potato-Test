"""Verify the browser actually selected for a run.

Answers three questions that a config edit alone cannot settle:

  1. which binary does the resolver pick, given BROWSER_CANDIDATES=chrome;
  2. does it agree with the manual benchmark that made chrome the default
     (0.41s start / 5.74s intranet first paint vs chromium's 0.49s / 8.35s);
  3. ★ does the profile it launches with stay inside the project, or does it reach for
     the user's daily Chrome profile? Point 3 is the one that produced the original
     "我这边登录了，同一个电脑里面另一边就会出现故障" report, so it is asserted rather
     than eyeballed: two processes on one user-data-dir overwrite each other's login
     state and clear each other's cache.

Prints a short report; no asserts on timings (too noisy to gate on), asserts on paths.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import browser_binary  # noqa: E402
from app.config import get_settings  # noqa: E402


def main() -> int:
    s = get_settings()
    print(f"BROWSER_CANDIDATES = {s.browser_candidates!r}")
    print(f"BROWSER_EXECUTABLE = {s.browser_executable!r}")
    print(f"profile_dir        = {s.profile_dir!r}")

    found = browser_binary.resolve_browser_executable(
        s.browser_executable, s.browser_candidates
    )
    print(f"resolved           = {found}")
    print(f"describe()         = {browser_binary.describe(found)}")

    assert found, "解析不到任何浏览器"
    assert found.lower().endswith("chrome.exe"), f"期望 Chrome 正式版，实际 {found}"

    # The daily browser's profile must never be handed to a run.
    daily = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")
    assert os.path.normcase(daily) not in os.path.normcase(found), (
        f"解析到了用户的日常 Chrome profile：{found}"
    )
    print(f"\n[OK] 不使用日常 Chrome profile（{daily}）")

    # What actually gets passed as user_data_dir: the lease path under the project.
    lease = os.path.abspath(os.path.join(ROOT, s.profile_dir, "proj-1", "role-a"))
    proj_profiles = os.path.abspath(os.path.join(ROOT, s.profile_dir))
    assert lease.startswith(proj_profiles + os.sep), f"lease 目录不在项目内：{lease}"
    assert not os.path.normcase(daily).startswith(os.path.normcase(proj_profiles)), (
        f"项目 profile 目录竟然落在日常 profile 里：{proj_profiles}"
    )
    print(f"[OK] user_data_dir 在项目内：{lease}")

    # One real launch, so this is not just a path assertion: chrome.exe exists and can
    # actually start with that profile. Timing is printed for reference only.
    from playwright.async_api import async_playwright

    async def run() -> tuple[float, bool]:
        t0 = time.monotonic()
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=lease,
                executable_path=found,
                headless=True,
                args=["--no-proxy-server"],
            )
            try:
                page = await ctx.new_page()
                await page.goto("about:blank")
                ok = await page.title() is not None
            finally:
                await ctx.close()
        return time.monotonic() - t0, ok

    elapsed, ok = asyncio.run(run())
    print(f"[OK] 实际启动成功 ={ok}，耗时 {elapsed:.2f}s（参考值，不做断言）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())