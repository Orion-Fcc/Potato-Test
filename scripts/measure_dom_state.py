"""量一下 browser-use 每步发出去的 DOM 状态到底有多大、能被裁掉多少。

为什么需要这个脚本（2026-10-11）
--------------------------------
用户的诉求是"DeepSeek 的 token 太花钱"。真库账本（potato.db，46 条有 usage 的结果，
271 次调用）拆出来是：输入 431 万 token、其中缓存命中 293 万（占输入的 68%）。
也就是说**每一轮真正按全价计费的是那 32% 未命中的部分**，而每一步都在重发的
DOM 状态（browser-use 的 element tree）是这份输入里最大的一块。

browser-use 有两个控制这块大小的参数：
  · include_attributes            每步给每个元素附带的属性白名单（默认 30+ 个）
  · max_clickable_elements_length 元素树文本的硬上限（默认 40000 字符）

本脚本的做法是**受控对比**：只采集一次真实页面的 DOM，然后把同一棵 DOM 树
用不同的参数各序列化一遍。同一棵树 ⇒ 差异只来自参数本身，不来自页面刷新。

同时必须核对 element（可交互元素）数量不变 —— 裁剪属性如果顺手把元素也裁掉了，
省下的 token 就是拿"agent 找不到控件"换来的，那是赔本买卖。

跑法（需要能访问内网被测系统）：
    .venv/Scripts/python.exe scripts/measure_dom_state.py
    .venv/Scripts/python.exe scripts/measure_dom_state.py --url http://192.0.2.10:8600/
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# browser-use 会被宿主机注入的透明代理带偏（内网地址被送去代理 → 全线失败）。
# 与 app/executor.py::make_browser 同款处理：清掉进程里的代理变量。
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"

# 真地址按本机 .secrets.local.json 的规则脱敏（RFC 5737 的 192.0.2.0/24 是保留给文档用的，
# 不是任何真实主机）。跑的时候用 --url 或 POTATO_MEASURE_URL 传真实地址进来即可。
DEFAULT_URL = os.environ.get("POTATO_MEASURE_URL", "http://192.0.2.10:8600/")

# browser-use 0.13.x 的默认白名单（browser_use/dom/views.py::DEFAULT_INCLUDE_ATTRIBUTES）。
# 脚本自带一份而不是 import：这样哪天上游改了默认值，这里也看得见差异。
UPSTREAM_DEFAULT = [
    "title", "type", "checked", "id", "name", "role", "value", "placeholder",
    "data-date-format", "alt", "aria-label", "aria-expanded", "data-state",
    "aria-checked", "aria-valuemin", "aria-valuemax", "aria-valuenow",
    "aria-placeholder", "pattern", "min", "max", "minlength", "maxlength", "step",
    "accept", "multiple", "inputmode", "autocomplete", "aria-autocomplete", "list",
    "data-mask", "data-inputmask", "data-datepicker", "format", "expected_format",
    "contenteditable", "pseudo",
]

# 精简版：只留"用来认元素 / 判断状态 / 填值"的那几类。
#
# 砍掉的四类，以及为什么可以砍：
#   ① 校验类（pattern/min/max/minlength/maxlength/step）——
#      它帮模型"一次填对"，但代价是每个输入框都多背 5~6 个属性；
#      实测页面上绝大多数是普通文本框，真正依赖 pattern 校验的极少。
#   ② 掩码/日期格式类（data-mask/data-inputmask/data-datepicker/format/expected_format）——
#      特定组件库的私有标记，本项目的表格页几乎不出现。
#   ③ 文件/多选类（accept/multiple/inputmode/autocomplete/aria-autocomplete/list）——
#      只在文件上传与联想输入框上有意义。
#   ④ aria-value* 与 pseudo/contenteditable —— 只有滑块/富文本才用得到。
#
# 保留的是 agent 真正赖以定位和判断的东西：id / name / type / role / value /
# placeholder / title / checked / aria-label / aria-expanded / data-state / aria-checked。
TRIMMED = [
    "title", "type", "checked", "id", "name", "role", "value", "placeholder",
    "alt", "aria-label", "aria-expanded", "data-state", "aria-checked",
]


def _pct(part: int, whole: int) -> str:
    return f"{(100.0 * part / whole):.1f}%" if whole else "—"


async def _capture(url: str, *, login: bool, tenant: str, user: str, pwd: str):
    """真实页面采集一次 DOM，返回 (BrowserStateSummary, 页面说明)。"""
    from browser_use import BrowserSession

    # enable_default_extensions=False 有两个原因，缺一个都跑不起来：
    #   ① 它会去下载 uBlock Origin Lite —— 本机出网走透明代理，这一步直接挂住（实测卡死）；
    #   ② app/executor.py::make_browser 本来就是关的（广告拦截会改页面，干扰用例）。
    # 量化用的浏览器必须与生产同配置，否则量出来的 DOM 不是生产看到的那一棵。
    session = BrowserSession(
        headless=True,
        args=["--no-proxy-server"],
        enable_default_extensions=False,
    )
    await session.start()
    try:
        # ★ 必须用 session.navigate_to 而不是自己 page.goto：
        #   DOM 采集走 DomWatchdog，它依赖 session 内部的 agent_focus_target_id。
        #   自己 goto 时那个焦点没有被设上，采到的就是空树（实测拿到 51 字符的
        #   "Empty DOM tree…"，而且不会报错，只会静默量出一个假的 0）。
        await session.navigate_to(url)
        # 给 SPA 一点时间把首屏渲染完，否则量到的是一个空壳。
        await asyncio.sleep(4)

        note = "登录页"
        if login:
            page = await session.get_current_page()
            if page is None:
                note = "拿不到页面对象，未登录"
            else:
                filled = await _try_login(page, tenant, user, pwd)
                note = "登录后落地页" if filled else "登录未成功，仍停在登录页"

        # 给 SPA 一点时间把首屏渲染完，否则量到的是一个空壳。
        await asyncio.sleep(3)
        state = await session.get_browser_state_summary(include_screenshot=False)
        return state, note
    finally:
        await session.stop()


# 登录表单填充 JS。
#
# 为什么走 evaluate 而不是 Playwright 的 get_by_placeholder：
# browser-use 在 0.13.10 已经不用 Playwright 的 Page/Python API 了 ——
# `session.get_current_page()` 返回的是自己的 `browser_use.actor.page.Page`，
# 只有 evaluate / press / goto 这几个方法（实测 `get_by_placeholder` 直接 AttributeError）。
#
# 为什么必须手动递归 shadowRoot：本系统登录页的三栏输入框都在
# `|SHADOW(open)|` 里（见 DOM 树文本），普通 querySelector 一个都找不到。
#
# 为什么要用 getOwnPropertyDescriptor 去 set value：Vue/Element Plus 的输入框
# 直接 `el.value = x` 不会触发框架的响应式更新，提交时值还是空的。
_JS_LOGIN = """(tenant, user, pwd) => {
  var rs = [document];
  (function walk(node){
    var all = node.querySelectorAll('*');
    for (var i = 0; i < all.length; i++) {
      if (all[i].shadowRoot) { rs.push(all[i].shadowRoot); walk(all[i].shadowRoot); }
    }
  })(document);
  function byPh(txt){
    for (var r = 0; r < rs.length; r++) {
      var els = rs[r].querySelectorAll('input');
      for (var i = 0; i < els.length; i++) {
        if (els[i].getAttribute('placeholder') === txt) return els[i];
      }
    }
    return null;
  }
  function setVal(el, v){
    if (!el) return false;
    var d = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
    d.set.call(el, v);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    return true;
  }
  var a = setVal(byPh('请输入租户名称'), tenant);
  var b = setVal(byPh('请输入用户名'), user);
  var c = setVal(byPh('请输入密码'), pwd);
  var clicked = false;
  for (var r = 0; r < rs.length && !clicked; r++) {
    var bs = rs[r].querySelectorAll('button');
    for (var i = 0; i < bs.length; i++) {
      if ((bs[i].innerText || '').indexOf('登录') >= 0) { bs[i].click(); clicked = true; break; }
    }
  }
  return JSON.stringify({tenant: a, user: b, pwd: c, clicked: clicked});
}"""


async def _try_login(page, tenant: str, user: str, pwd: str) -> bool:
    """按登录页的可见文案填三栏。失败就返回 False，不抛 —— 量的是状态大小，不是登录能力。"""
    try:
        raw = await page.evaluate(_JS_LOGIN, tenant, user, pwd)
        print(f"  （登录表单）{raw}")
        await asyncio.sleep(5)
        return "/login" not in (await page.get_url() or "")
    except Exception as exc:  # noqa: BLE001
        print(f"  （登录过程报错，按未登录处理：{type(exc).__name__}: {exc}）")
        return False


def _report(state, note: str) -> None:
    dom = getattr(state, "dom_state", None)
    if dom is None:
        print("拿不到 dom_state，无法量化。")
        return

    n_elements = len(getattr(dom, "selector_map", {}) or {})
    print(f"\n页面：{note}")
    print(f"可交互元素：{n_elements} 个")
    print(f"URL：{getattr(state, 'url', '?')}")

    # 同一棵 DOM 树，三种参数各序列化一遍
    #
    # ★ 不能传空列表：browser_use 的 dom/views.py 里是
    #   `include_attributes = include_attributes or DEFAULT_INCLUDE_ATTRIBUTES`，
    #   空列表是 falsy，会被当成"没传"而回落默认 —— 量出来跟默认一模一样。
    #   只留 id 才是真正的下界。
    variants = [
        ("上游默认（当前行为）", UPSTREAM_DEFAULT),
        ("精简属性白名单", TRIMMED),
        ("只留 id（下界）", ["id"]),
    ]
    results: list[tuple[str, int]] = []
    for label, attrs in variants:
        t0 = time.monotonic()
        text = dom.llm_representation(include_attributes=attrs or None)
        dt = time.monotonic() - t0
        results.append((label, len(text)))
        print(f"  {label:<22} {len(text):>8,} 字符   序列化 {dt * 1000:.0f} ms")

    base = results[0][1]
    print()
    for label, n in results[1:]:
        saved = base - n
        print(f"  {label} 相对默认省 {saved:,} 字符（{_pct(saved, base)}）")

    # 上限有没有真的生效：默认 40000
    over = base > 40000
    print(
        f"\n  40000 字符上限：{'★ 已生效，元素树被截断' if over else '未触发（当前状态比上限还小）'}"
    )
    if over:
        print("  → 说明真实页面里有元素根本发不到模型手上；此时调小上限等于继续丢元素，风险高。")


async def main() -> int:
    ap = argparse.ArgumentParser(description="量 browser-use 每步 DOM 状态的大小")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--no-login", action="store_true", help="只量登录页，不尝试登录")
    ap.add_argument("--tenant", default=os.environ.get("POTATO_MEASURE_TENANT", "示例培训"))
    ap.add_argument("--username", default="admin")
    ap.add_argument("--password", default="", help="留空则从 potato.db 里的 admin 凭据解密取")
    ap.add_argument("--dump", type=int, default=0, help="打印元素树文本的前 N 个字符（0=不打印）")
    args = ap.parse_args()

    pwd = args.password
    if not pwd and not args.no_login:
        from app.crypto import decrypt  # noqa: PLC0415 — 只有真要登录时才需要它
        import sqlite3

        con = sqlite3.connect(f"file:{ROOT / 'potato.db'}?mode=ro", uri=True)
        row = con.execute(
            "select secret from credential where username='admin' and healthy=1 limit 1"
        ).fetchone()
        con.close()
        if row:
            pwd = decrypt(row[0])
        else:
            print("库里没有健康的 admin 凭据，改为只量登录页。")
            args.no_login = True

    state, note = await _capture(
        args.url, login=not args.no_login, tenant=args.tenant, user=args.username, pwd=pwd
    )
    _report(state, note)
    if args.dump:
        dom = getattr(state, "dom_state", None)
        if dom is not None:
            print("\n" + "=" * 60 + f"\n元素树前 {args.dump} 字符：\n" + "=" * 60)
            print(dom.llm_representation(include_attributes=UPSTREAM_DEFAULT)[: args.dump])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
