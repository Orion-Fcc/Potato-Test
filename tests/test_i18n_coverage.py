"""中文界面里不该冒出未登记的英文文案。

为什么值得一条测试
==================
这个项目的 i18n 是 **英文串当键、`zh` 表覆盖** 的设计（见 `web/src/i18n.ts` 开头）：
`t("Start run")` 在中文环境下查 `zh["Start run"]`，查不到就回落到键本身 —— 也就是
**英文原文直接显示给用户**。

所以"新增了一句文案但忘了加到 zh 表"在开发和测试里都不会报错，它只在用户看到一句
英文时暴露。而且它极容易漏：`t()` 是零成本的，加文案的人不会记得另一边还有个表。

实测（2026-10-08）：662 个 `t()` 字面量里只有 4 个没登记，且都是中文自映射
（显示正常）。也就是说现状**几乎是齐的** —— 这条测试的作用是保持这个状态，
而不是去补一大片债。

为什么不查 JS/TS 的类型系统
--------------------------
键是任意字符串，没有联合类型可查；`zh` 是个 `Record<string, string>`，拼错键名
不会报错。所以只能靠文本比对，这也和本仓库既有的一批"读源码的接线测试"同路数。
"""

from __future__ import annotations

import pathlib
import re

import pytest

_WEB = pathlib.Path(__file__).resolve().parents[1] / "web" / "src"

# 键在 i18n.ts 里可能写成 "双引号"、'单引号' 或不带引号的标识符（中文键就是这样）。
_ZH_DQ = re.compile(r"^\s*\"((?:[^\"\\]|\\.)*)\"\s*:", re.M)
_ZH_SQ = re.compile(r"^\s*'((?:[^'\\]|\\.)*)'\s*:", re.M)
_ZH_IDENT = re.compile(r"^\s*([\w\u4e00-\u9fff]+)\s*:", re.M)

# t("...") / t('...')，排除 foo.t(...)（那可能是别的库）
_CALL = re.compile(r"(?<![\w.])t\(\s*(?:\"((?:[^\"\\]|\\.)*)\"|'((?:[^'\\]|\\.)*)')")


def _unescape(s: str) -> str:
    """源码里的 \\" 与运行时字符串里的 " 是同一个字符，比对前先归一。"""
    return s.replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")


def _zh_keys() -> set[str]:
    text = (_WEB / "i18n.ts").read_text(encoding="utf-8")
    keys = {_unescape(m) for m in _ZH_DQ.findall(text)}
    keys |= {_unescape(m) for m in _ZH_SQ.findall(text)}
    keys |= set(_ZH_IDENT.findall(text))
    return keys


def _used_keys() -> dict[str, str]:
    """字面量键 → 它第一次出现的文件（报错时能直接指到该改哪）。"""
    out: dict[str, str] = {}
    for f in list(_WEB.rglob("*.tsx")) + list(_WEB.rglob("*.ts")):
        if f.name == "i18n.ts":
            continue
        text = f.read_text(encoding="utf-8", errors="ignore")
        for m in _CALL.finditer(text):
            key = _unescape(m.group(1) or m.group(2))
            out.setdefault(key, str(f.relative_to(_WEB)))
    return out


def test_every_t_call_has_a_chinese_entry():
    """每个 `t("…")` 字面量都必须在 zh 表里有登记。

    漏登记 = 中文界面显示英文原文，而且不会有任何报错。
    """
    if not _WEB.is_dir():
        pytest.skip(f"前端源码不在：{_WEB}")

    zh = _zh_keys()
    used = _used_keys()
    missing = {k: v for k, v in used.items() if k not in zh}

    assert not missing, (
        "以下文案被 t() 使用但 i18n.ts 的 zh 表里没有登记（中文界面会显示原文）：\n"
        + "\n".join(f"  - {k[:90]}  ({v})" for k, v in sorted(missing.items()))
    )


def test_the_extractor_is_not_silently_finding_nothing():
    """自证：提取器必须真的抓到东西。

    没有这条，"正则写坏了 → missing 为空 → 测试通过"就会伪装成一切正常 ——
    一个永远通过的守卫比没有守卫更糟，因为它看起来像保护。
    """
    if not _WEB.is_dir():
        pytest.skip(f"前端源码不在：{_WEB}")
    assert len(_zh_keys()) > 300, "zh 表解析结果异常地少，正则可能失效了"
    assert len(_used_keys()) > 300, "t() 字面量解析结果异常地少，正则可能失效了"
