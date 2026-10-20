"""依赖锁文件（`constraints.txt`）与 `pyproject.toml` 的一致性（2026-10-20，P0-1）。

守的是一条很具体的故障：往 `pyproject.toml` 里加了一个依赖，却忘了锁版本 ——
CI 装到的是"当天的最新版"，打包出来又是另一个版本，于是本地绿、CI 绿、
用户装出来坏。测试直接比对两份文件，新增依赖时立刻红。

反过来也守：锁文件里的直接依赖必须写死 `==`，范围（`>=` / `~=`）不算锁。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONSTRAINTS = ROOT / "constraints.txt"
PYPROJECT = ROOT / "pyproject.toml"

# 只取需求名的部分：`uvicorn[standard]>=0.29` / `pywebview>=4.4; sys_platform == "win32"`
_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _normalize(name: str) -> str:
    """PEP 503：大小写、`_` / `.` / `-` 都等价（PyJWT == pyjwt）。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(req: str) -> str:
    m = _NAME_RE.match(req)
    assert m, f"看不懂的依赖写法：{req!r}"
    return _normalize(m.group(1))


def _locked_requirements() -> dict[str, str]:
    """constraints.txt → {规范化名: 整行}（注释与空行跳过）。"""
    out: dict[str, str] = {}
    for raw in CONSTRAINTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name = _requirement_name(line)
        assert name not in out, f"constraints.txt 里 {name} 出现了两次"
        out[name] = line
    return out


def _required_names() -> set[str]:
    """项目实际安装时会拉到的直接依赖：运行时 + dev + desktop。"""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = data["project"]
    names = {_requirement_name(r) for r in project.get("dependencies", [])}
    extras = project.get("optional-dependencies", {})
    for extra in ("dev", "desktop"):
        names |= {_requirement_name(r) for r in extras.get(extra, [])}
    return names


def test_every_direct_dependency_is_locked():
    locked = _locked_requirements()
    missing = sorted(_required_names() - set(locked))
    assert not missing, f"这些直接依赖没有锁版本（补进 constraints.txt）：{missing}"


def test_locks_are_exact_pins():
    """范围不算锁：`>=1.0` 在不同机器上会解析成不同版本。"""
    loosened = [
        line
        for line in _locked_requirements().values()
        if "==" not in line.split(";", 1)[0]
    ]
    assert loosened == [], f"这些依赖没有写死版本：{loosened}"


def test_constraints_are_used_by_ci():
    """锁文件存在但没人用等于没有 —— CI 的安装步骤必须带 `-c constraints.txt`。"""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "-c constraints.txt" in ci, "CI 安装依赖时没有引用 constraints.txt"
