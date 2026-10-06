"""执行期入口：把用例声明的测试数据变成"可上传的文件"+ 一段提示词。

单独成模块的原因
----------------
`app/executor.py` 要改的地方只有两行接线（下面 `prepare()` 的调用点），
但那段逻辑本身有坑，放进 executor 会让那个 2500 行的文件更难读：

1. **产物目录的键是 RunResult.id，不是 run.id**。`executor.py` 里
   `art = f"runs/{spec.result_id or spec.case_id}"` —— 同一用例每跑一次是一个新目录。
   放在这里算，测试才不用启动浏览器。
2. **文件必须落在 `runs/<结果id>/data/`**。这不是随手选的：清理脚本
   （`scripts/cleanup_orphan_artifacts.py`）按"`run.id` ∪ `run_result.video_url` 解析出的
   id"判断哪些运行目录要保留，放在这个位置就被自动覆盖，不会被当成孤儿删掉。
3. **agent 只允许上传白名单里的路径**。browser-use 的 `upload_file` 动作
   实测只接受三类：`available_file_paths`、本次会话下载的文件、FileSystem 沙箱里的文件。
   自己生成的文件**不在任何一类里**，所以必须显式注册，否则 agent 传不上去 ——
   而这个失败长这样："agent 说我不能上传这个文件"，很容易被误判成被测系统的限制。

用法（executor.py 里的两行）
----------------------------
    prepared = prepare(spec.data_files, spec.case_key, artifacts_root, spec.result_id)
    # → agent_kwargs["available_file_paths"] = prepared.upload_paths
    # → 拼进 extend_system_message：prepared.prompt_block
"""

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass, field
from typing import Any

from app import testdata

log = logging.getLogger(__name__)


@dataclass
class Prepared:
    """一次执行里可上传的文件集合。"""

    files: list[testdata.PreparedFile] = field(default_factory=list)
    # 声明有问题时不会抛出去打断执行，而是降级成"没有文件"并留下一句原因。
    # 理由：一条用例的数据声明写错，不该让整轮运行失败 —— 那样用户会以为是
    # 被测系统坏了。而"文件没准备好"这件事必须能被 agent 和报告看见。
    error: str = ""

    @property
    def upload_paths(self) -> list[str]:
        """给 `Agent(available_file_paths=...)` 的绝对路径列表。"""
        return [str(f.path) for f in self.files]

    @property
    def prompt_block(self) -> str:
        """给系统提示词的一段说明。没有文件时返回空串（不注入噪音）。"""
        parts: list[str] = []
        if self.files:
            parts.append(
                "**上传文件**：本次运行已由平台准备好下列文件（来自用例的「测试数据」声明）。\n"
                "要用它们，走 `upload_file` 动作并传下面的**绝对路径**；"
                "**不要**把路径打进输入框，也**不要**试图自己造文件"
                "（你没有生成 xlsx 的能力，写出来的文件浏览器不认）。\n"
                + testdata.summarize(self.files)
            )
        if self.error:
            parts.append(
                f"**注意**：这条用例声明的测试数据无法生成（{self.error}），"
                "所以本次**没有**可上传的文件。如果用例要求导入文件，"
                "请把这一点作为失败原因报出来，不要假装导入成功。"
            )
        return "\n\n".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "files": [f.as_dict() for f in self.files],
            "error": self.error,
        }


def data_dir(artifacts_root: pathlib.Path | str, result_id: int) -> pathlib.Path:
    """产物目录下的 data/ 子目录。"""
    return pathlib.Path(artifacts_root) / "runs" / str(result_id or 0) / "data"


def prepare(
    decl: Any,
    case_key: str = "",
    artifacts_root: pathlib.Path | str = "artifacts",
    result_id: int = 0,
) -> Prepared:
    """把声明物化成文件。**任何失败都降级成空集合 + error**，不抛异常。

    不抛是刻意的：执行期抛异常会中止整条用例，而"数据声明写错"是配置问题，
    不是被测系统的失败。真正需要中断的情况（运行被取消）有另外的通道。
    """
    if decl in (None, "", {}):
        return Prepared()
    try:
        files = testdata.materialize(decl, data_dir(artifacts_root, result_id), case_key)
    except testdata.SpecError as exc:
        log.warning("用例 %s 的测试数据声明无效：%s", case_key or "?", exc)
        return Prepared(error=str(exc))
    except Exception as exc:  # noqa: BLE001 —— 写盘也可能失败（磁盘满、权限）
        log.exception("用例 %s 的测试数据物化失败", case_key or "?")
        return Prepared(error=f"{type(exc).__name__}: {exc}")
    return Prepared(files=files)


def persist_manifest(prepared: Prepared, artifacts_root: pathlib.Path | str, result_id: int) -> pathlib.Path | None:
    """把本次生成的文件清单写进产物目录。

    为什么要有这个文件：报告里"上传了哪个文件"如果只存在于 agent 的思考过程里，
    失败时就无法回答"传上去的东西对不对"。文件名 + sha256 + 字节数够复现判断。
    """
    if not prepared.files:
        return None
    root = data_dir(artifacts_root, result_id).parent
    root.mkdir(parents=True, exist_ok=True)
    out = root / "testdata.json"
    import json

    out.write_text(
        json.dumps(prepared.as_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out
