"""One-off check: what does the dedicated browser actually show on the intranet?

Run standalone: `.venv/Scripts/python.exe app/_check_browser_visible.py`
It drives the ALREADY-RUNNING window over CDP rather than launching a second one, so
whatever window the user has on screen is what gets measured.
"""

from __future__ import annotations

import asyncio
import os


async def main() -> int:
    import httpx

    # Find the debug port of OUR profile. Distinguishing matters: the human may also
    # have Edge open on 9222, and reporting Edge's page as ours would be wrong.
    profile = os.path.expandvars(r"%LOCALAPPDATA%\potato-browser-profile")
    port = int(os.environ.get("PB_CDP_PORT", "9333"))

    async with httpx.AsyncClient(timeout=5, trust_env=False) as c:
        try:
            r = await c.get(f"http://127.0.0.1:{port}/json/list")
            targets = r.json()
        except Exception as exc:  # noqa: BLE001
            print(f"[iife] {port} 端口无响应（{type(exc).__name__}）——浏览器不是带调试端口启动的。")
            print("     这不影响使用：手动打开只能看页面，attach 才需要调试端口。")
            print(f"     profile: {profile}")
            return 0

    pages = [t for t in targets if t.get("type") == "page"]
    print(f"[iife] {len(pages)} 个页面")
    for t in pages:
        print(f"  - {t.get('title','')[:40]!r} {t.get('url','')[:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))