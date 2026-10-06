"""录像规格必须与页面视口一致（2026-10-06 用户要求）。

## 为什么这是必须钉死的约束

用户原话：「视口和我人工测试时要一模一样，视口不要有拉伸，要和正常人工测试页面一样」。

录像规格（`video_width/height`）与页面视口（`browser_viewport_width/height`）是
两套独立配置。历史上它们是 1280×800 vs 1434×825 —— 差 154px 宽。

后果不是"录像糊了"，而是**回放观感误导**：同一份录像，播放器容器窄时按接近1:1 显示、
宽时按比例拉伸（实测在 1900px 宽的播放器里被放到约 1.48倍），
于是看起来"界面比人工测试大一号"，像是布局不同——
**会让人误判被测页面的响应式行为**，而这正是本项目最在意的东西。

## 两条红线的区别（别混为一谈）

- **视口** = 被测对象的布局依据。只有"与用户人工环境对齐"这个理由能动它，
  绝不能为提速去动（config.py 里有长期注释）。
- **录像** = 回放素材。改它不改变任何判定结果，让它等于视口纯粹是为了回放观感。

所以本文件只锁「录像 == 视口」这个等式，不规定具体数值——
数值随被测环境变（换机器/换缩放要重量），等式永远成立。
"""

from __future__ import annotations

import inspect

from app.config import get_settings


def test_recorded_size_equals_viewport():
    """录像规格必须逐项等于视口，否则回放必然出现拉伸观感。"""
    s = get_settings()
    assert (s.video_width, s.video_height) == (
        s.browser_viewport_width,
        s.browser_viewport_height,
    ), (
        f"录像 {s.video_width}x{s.video_height} 与视口 "
        f"{s.browser_viewport_width}x{s.browser_viewport_height} 不一致 —— "
        f"回放会被播放器拉伸，看起来像页面布局变了。"
        f"要改请同时改这两组配置（.env 优先，见 VIDEO_WIDTH/HEIGHT 与 "
        f"BROWSER_VIEWPORT_WIDTH/HEIGHT）。"
    )


def test_viewport_matches_local_edge_measurement():
    """视口固定为 1434x825 —— 用户本机Edge 最大化的真实 CSS 视口。

    实测依据（本机 2026-10-06）：屏幕物理 2880x1800、DPI 192（缩放 200%）、
    任务栏以上可用 2880x1755；Edge 最大化窗口外框 2906x1730，
    页面 `innerWidth/innerHeight` = **1434x825**。

    换机器或改缩放后这个值会变——那时应重新实测并更新，
    **不要靠"物理尺寸 / 2"推算**（实测 2880/2=1440 但真实值是 1434，
    差值是窗口边框与标签栏/地址栏）。
    """
    s = get_settings()
    assert (s.browser_viewport_width, s.browser_viewport_height) == (1434, 825), (
        f"视口变成 {s.browser_viewport_width}x{s.browser_viewport_height}，"
        f"与实测的本机 Edge 视口 1434x825 不符。"
        f"如果是换了机器/改了缩放，请先用 CDP 实测 innerWidth/innerHeight 再改这里，"
        f"不要用物理分辨率推算。"
    )


def test_video_kwargs_reach_browser():
    """录像参数要真的传到 Browser —— 配了不等于生效。

    背景：Playwright 的 `record_video_size` 不接受部分尺寸时会静默忽略，
    届时录像仍是浏览器默认尺寸，而配置看起来是对的。
    """
    from app import executor

    src = inspect.getsource(executor)
    assert "record_video_size" in src, "录像尺寸没有传给 Browser"
    assert "record_video_framerate" in src, "录像帧率没有传给 Browser"


def test_video_resolution_is_wired_in_both_places():
    """两处构造 Browser 的地方都要接录像参数（防止新增路径漏配）。"""
    from app import executor

    capture_src = inspect.getsource(executor.capture_session)
    # capture_session 这条路径不录像（它只抓登录态），因此**不应该**有 record_video_size
    assert "record_video_size" not in capture_src, (
        "capture_session 不该录视频（它只负责登录），"
        "出现 record_video_size 说明录像逻辑被误复制过去了"
    )

    main_src = inspect.getsource(executor)
    assert main_src.count("record_video_size") >= 1, "主执行路径没有录像尺寸配置"
