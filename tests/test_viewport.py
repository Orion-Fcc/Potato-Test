"""视口与用户手动测试环境对齐。

背景（本机2026-10-06 实测）：
  屏幕物理 2880×1800、Windows 缩放 200%（DPI 192）。
  browser-use 的 headless 分支把 viewport 设成 `screen`，screeninfo 返回物理像素
  → 视口 2880×1800 CSS 像素；用户手动用 Edge（最大化）测的真实 CSS 视口是 1434×825。
  两者宽高都差约 2 倍，响应式布局（断点、表格列折叠、侧边栏收起）在两种宽度下
  根本不是同一个页面。

本文件守两件事：
  1. _viewport_kwargs() 的形状与"必须成对"的校验；
  2. 两处 Browser 构造（execute_case 的 make_browser、capture_session）都真的接上了。
     ★ 接线测试必须打桩browser_use源包 —— executor 在函数体内 from browser_use import
     Browser，名字在调用时才从源包解析。打 executor.Browser 会得到空跑通过的假测试
     （这是本项目踩过的坑，见记忆"打桩要打在源包上"）。
"""

from __future__ import annotations

import asyncio

import pytest


# ---------- 默认值 ----------

def test_viewport_default_matches_local_edge():
    """默认视口 = 本机 Edge 最大化窗口的真实 CSS 视口（实测 innerW/innerH）。"""
    from app.config import get_settings

    s = get_settings()
    assert (s.browser_viewport_width, s.browser_viewport_height) == (1434, 825)


# ---------- _viewport_kwargs 形状 ----------

def test_viewport_kwargs_passes_pair_through(monkeypatch):
    from app import executor

    monkeypatch.setattr(executor, "get_settings", lambda: type(
        "S", (), {"browser_viewport_width": 1200, "browser_viewport_height": 700})())
    assert executor._viewport_kwargs() == {"viewport": {"width": 1200, "height": 700}}


def test_viewport_kwargs_zero_pair_means_delegate(monkeypatch):
    """两个都0 → 返回 {}，交回browser-use 自己决定（老行为=屏幕物理分辨率）。"""
    from app import executor

    monkeypatch.setattr(executor, "get_settings", lambda: type(
        "S", (), {"browser_viewport_width": 0, "browser_viewport_height": 0})())
    assert executor._viewport_kwargs() == {}


@pytest.mark.parametrize("w,h", [(1440, 0), (0, 900)])
def test_viewport_kwargs_rejects_half_pair(monkeypatch, w, h):
    """只填一个必须报错：半边视口让比例失真，看起来像"页面渲染坏了"。"""
    from app import executor

    monkeypatch.setattr(executor, "get_settings", lambda: type(
        "S", (), {"browser_viewport_width": w, "browser_viewport_height": h})())
    with pytest.raises(ValueError, match="必须成对设置"):
        executor._viewport_kwargs()


# ---------- 接线 ----------



class _Wired(Exception):
    """构造已发生。抛出它来中断capture_session 后续的真实浏览器逻辑。"""


def test_capture_session_wires_viewport(monkeypatch, tmp_path):
    """capture_session 单独new 了一个 Browser，必须也接上视口。

    只接一处会出现"捕获登录态时是 2880 宽布局、真正执行时是 1434 宽"，
    这种不一致只在特定页面偶发，排查成本极高。

    注意断言方式：捕获到参数后立刻抛哨兵中断后续流程，所以**不**去校验
    captured —— capture_session 里真正构造 Browser 之前还有一堆准备步骤，
    让它跑完会碰到真浏览器和真文件系统。
    """
    import browser_use
    from app import executor

    captured: list = []

    class _FakeBrowser:
        def __init__(self, **kw):
            captured.append(kw)
            raise _Wired()

    monkeypatch.setattr(browser_use, "Browser", _FakeBrowser)
    # 桩 settings 只需撑到 Browser 构造那一刻：capture_session 先取 profile_dir。
    monkeypatch.setattr(executor, "get_settings", lambda: type(
        "S", (), {"browser_viewport_width": 1434, "browser_viewport_height": 825,
                  "profile_dir": str(tmp_path)})())

    with pytest.raises(_Wired):
        asyncio.run(executor.capture_session("http://x", "u", "p", project_id=1))

    assert captured == [{
        "headless": True,
        "keep_alive": True,
        "enable_default_extensions": False,
        "viewport": {"width": 1434, "height": 825},
        "user_data_dir": captured[0]["user_data_dir"],  # profile 目录由运行时决定
        "args": ["--no-proxy-server"],
    }]


def test_viewport_kwargs_is_used_by_both_browser_constructions():
    """静态钉住：源码里两处 Browser(...) 都带**_viewport_kwargs()。

    纯文本断言，不是行为断言 —— 但它防的是"有人新增了第三处 Browser(...) 却忘了接线"，
    这类漏接在运行时才暴露，且症状是偶发的元素找不到。
    """
    import inspect

    from app import executor

    src = inspect.getsource(executor)
    n = src.count("Browser(")
    wired = src.count("**_viewport_kwargs()")
    assert n >= 2, f"预期至少两处 Browser 构造，实际 {n} 处"
    assert wired >= 2, (
        f"只有 {wired} 处接了 _viewport_kwargs()，"
        f"但有 {n} 处 Browser(...) —— 有构造漏接视口"
    )