"""需规文档直接上传给助手（2026-10-09）。

用户的原话：「我想让这个 agent 实现的功能应该是我发需归文件给内置的助手，
包括不同格式，然后它可以生成测试用例」。

所以在"上传"这件事上要守的不是"文件存下来了"，而是：

1. **不同格式都要能读** —— 用户不会为了这个工具去转换格式。
   实测支持 md / txt / json / csv / docx / pdf / xlsx / html / rtf。
2. **上传即入库、入库即规划** —— 这是"少一步手工"的价值所在。
   如果只入库不规划，用户还得跑到另一个面板重新选一遍，上下文全靠自己记。
3. **追加而不是替换** —— 一个项目的需规常常是好几份文件。
   替换式上传会让上一份内容消失，而用户通常并不知道这一点。
4. **格式不认识/文件损坏要给一句清楚的话**，不能 500 —— 用户需要知道
   是"格式不支持"还是"文件坏了"，这两种的下一步动作不一样。
"""

from __future__ import annotations

import io
import os
import tempfile
import uuid

import pytest


@pytest.fixture()
def client():
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    dbname = f"_tp_specup_{uuid.uuid4().hex[:8]}.db"
    tmp = os.path.join(tempfile.gettempdir(), dbname)
    if os.path.exists(tmp):
        os.remove(tmp)

    from fastapi.testclient import TestClient

    from app import db as dbmod
    from app.models import Base

    eng = create_async_engine(
        "sqlite+aiosqlite:///" + tmp.replace("\\", "/"), future=True, poolclass=NullPool
    )
    prev_engine, prev_session = dbmod._engine, dbmod._Session
    dbmod._engine = eng
    dbmod._Session = async_sessionmaker(eng, expire_on_commit=False)

    async def _create():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())
    from app.main import app

    assert dbname in str(eng.url).replace("%5F", "_"), f"没连到临时库：{eng.url}"
    with TestClient(app) as c:
        yield c

    asyncio.run(eng.dispose())
    dbmod._engine, dbmod._Session = prev_engine, prev_session
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass


DOC = """# 资源审批配置

## 3.1 列表查询
在资源审批配置列表中，可以按名称筛选。点击查询后列表只显示名称匹配的记录。

## 3.2 新建规则
点击新建规则，弹窗中包含规则名称、使用范围、资源范围三个字段，三个字段都必填。
"""


def _project(client, name: str = "P") -> int:
    return client.post("/api/projects", json={"name": name}).json()["id"]


def _upload(client, pid: int, filename: str, data: bytes, append: bool | None = None):
    url = f"/api/projects/{pid}/spec/upload"
    if append is not None:
        url += f"?append={'true' if append else 'false'}"
    return client.post(url, files={"file": (filename, io.BytesIO(data), "text/plain")})


# ---------------------------------------------------------------------------
# 格式支持
# ---------------------------------------------------------------------------


def test_uploading_markdown_stores_it_and_plans_modules(client) -> None:
    """★ 这就是用户要的那一步：发文件 →（入库）→（能生成用例）。

    这里**不**断言规划一定成功：规划要调模型，而 CI/测试环境里网关通常是不可用的
    （实测会回 401）。要守的是**降级行为** —— 网关调不通时仍然必须给出可用的分批，
    否则用户看到"上传成功但没有可生成的模块"，只能反复重传同一份文件。
    """
    pid = _project(client)
    r = _upload(client, pid, "req.md", DOC.encode("utf-8"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["format"] == "md"
    assert body["headings"] >= 3, f"没认出章节标题：{body}"
    assert body["modules"], "上传后必须给出可生成的模块（规划失败也要降级，不能为空）"
    assert body["appended"] is True
    # 降级时 error 要如实带出去 —— 用户得知道这批分批是"碎的"而不是模型归并过的
    if body["error"]:
        assert body["degraded"] is True, "规划报错却没标 degraded，用户会以为这就是模型的最佳分批"


def test_plain_text_upload_still_gets_in(client) -> None:
    """纯文本没有标题 —— 仍然必须入库，并**明确说明**为什么没有模块。

    只回一个空 modules 的话，用户会以为文件没传上去，然后反复重传。
    """
    pid = _project(client)
    r = _upload(client, pid, "notes.txt", "这是一段没有标题的说明文字。".encode("utf-8"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["chars"] > 0
    assert body["modules"] == []
    assert body["warnings"], "没有标题时必须给出警告，否则用户不知道为什么生成不出用例"


def test_csv_is_accepted(client) -> None:
    """字段表常常是 csv —— 用户不会为了这个工具去转格式。"""
    pid = _project(client)
    csv = "字段,类型,必填\n规则名称,string,是\n使用范围,string,是\n"
    r = _upload(client, pid, "fields.csv", csv.encode("utf-8"))
    assert r.status_code == 200, r.text
    assert r.json()["chars"] > 0


def test_unsupported_binary_gets_a_clear_message_not_a_500(client) -> None:
    """格式不认识时要说清"支持哪些"，而不是 500。

    "格式不支持"和"文件坏了"的下一步动作不一样，用户需要能分清。
    """
    pid = _project(client)
    r = _upload(client, pid, "thing.exe", b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 200)
    assert r.status_code == 400
    assert "support" in r.text.lower() or "支持" in r.text


def test_an_empty_file_is_handled(client) -> None:
    pid = _project(client)
    r = _upload(client, pid, "empty.md", b"")
    # 空文件要么被明确拒绝，要么入库 0 字符并给警告 —— 不能 500
    assert r.status_code in (200, 400)
    if r.status_code == 200:
        assert r.json()["warnings"] or r.json()["chars"] == 0


# ---------------------------------------------------------------------------
# 追加 vs 替换
# ---------------------------------------------------------------------------


def test_two_uploads_accumulate_by_default(client) -> None:
    """★ 一个项目的需规常常是好几份文件（需求 + 字段表 + 流程图）。

    默认"替换"会让第一份内容悄悄消失 —— 而用户完全看不出来，
    只会发现"上次能搜到的需求这次搜不到了"。

    判据用 **heading 数**而不是模块名：模块名是模型起的（测试环境里网关不可用，
    走的是降级路径），而 heading 来自文档本身，不受模型影响。

    ★ 每份文档的每个标题下面都**要有正文**：`knowledge.chunk_text` 只把"有内容的
    小节"记成 chunk，一个空标题（后面紧跟着下一个标题）不会成为独立章节。
    这是对的行为 —— 空标题本来就没有需求可生成 —— 所以测试文档要写成真实形态。
    """
    pid = _project(client)
    r1 = _upload(client, pid, "a.md", "# 甲\n## 甲-1\n甲的内容。\n## 甲-2\n甲的第二节。\n".encode("utf-8"))
    assert r1.status_code == 200
    n1 = r1.json()["headings"]
    assert n1 >= 2, f"第一份文档就只认出 {n1} 个标题"

    r2 = _upload(client, pid, "b.md", "# 乙\n## 乙-1\n乙的内容。\n".encode("utf-8"))
    assert r2.status_code == 200
    n2 = r2.json()["headings"]
    assert n2 > n1, f"追加模式下标题数没有增加（{n1} -> {n2}），第一份文档可能被覆盖了"


def test_replace_mode_is_opt_in(client) -> None:
    """要整份替换必须显式说 —— 默认行为不该是最具破坏性的那个。"""
    pid = _project(client)
    r1 = _upload(
        client, pid, "a.md", "# 甲\n## 甲-1\n甲的内容。\n## 甲-2\n甲的第二节。\n".encode("utf-8")
    )
    n1 = r1.json()["headings"]
    r = _upload(client, pid, "b.md", "# 乙\n## 乙-1\n乙的内容。\n".encode("utf-8"), append=False)
    assert r.status_code == 200
    assert r.json()["appended"] is False
    assert r.json()["headings"] < n1, "replace 模式应当丢掉第一次上传的内容（标题数没有减少）"


def test_upload_carries_warnings_and_truncation_flag(client) -> None:
    """截断必须如实上报 —— 一份被截掉的需规仍然"上传成功"，
    但生成出来的用例会缺一大块，用户得知道。"""
    pid = _project(client)
    r = _upload(client, pid, "big.md", ("# 大文档\n## 节\n" + "内容。" * 200_000).encode("utf-8"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert "truncated" in body and "warnings" in body
