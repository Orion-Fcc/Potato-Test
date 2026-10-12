"""AI 用例优化提案：生成、收口、采纳落地（2026-10-12）。

## 这组测试守的是什么

用户要的是"AI 优化/增加/删除用例"，但他选了**先出提案、逐条审阅后再入库**。
于是这个功能的风险不在"AI 提得准不准"，而在**边界**：

* AI 能不能自己改用例库？（不能 —— 只有 `decide` 会写 `test_case`）
* `remove` 会不会真的删掉用例？（不会 —— 改成 deprecated，可恢复）
* `update` 会不会整条覆盖？（不会 —— 只合并提案里出现的字段）
* 模型编了一个不存在的用例编号，会不会照样进库？（不会 —— 收口时丢掉）
* 人工改判的通过率，会不会把"自动判定的失败"混进来算？（不会 —— 只认 override）

这些每一条都对应一次"用户以为没事、实际已经出事"的可能，所以按边界写，不按覆盖率写。
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import uuid

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.case_optimize import (  # noqa: E402
    _fix_flow_direction,
    build_prompt,
    group_cases_by_module,
    parse_proposals,
    spec_text_for_module,
)


@pytest.fixture()
def client():
    """TestClient + 只属于本测试的临时库（理由见 docs/PROJECT_ISOLATION.md 第七节）。"""
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    dbname = f"_tp_prop_{uuid.uuid4().hex[:8]}.db"
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


def _project(client, name="P") -> int:
    return client.post("/api/projects", json={"name": name}).json()["id"]


def _case(client, pid: int, name: str, *, module: str = "模块A", expected: str = "看到列表") -> dict:
    r = client.post(
        f"/api/projects/{pid}/testcases",
        json={"name": name, "prompt": f"打开页面做 {name}", "module": module, "expected": expected},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _seed_overview(client, pid: int, html: str) -> None:
    """项目文档是提案生成的输入 —— 没有它就无从优化。"""
    r = client.put(
        f"/api/projects/{pid}/docs",
        json={"kind": "overview", "title": "项目总览", "html": html},
    )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 收口：模型输出 → 提案
# ---------------------------------------------------------------------------
class TestRoleValidation:
    """★ 账号必须**在项目真实存在的清单里**（2026-10-12 用户要求「附上账号」）。

    这是这一块最容易出问题的地方：模型会照**需规里的角色名**写
    （"结算单发起人"、"培训保障设施设备维护审批人"），而 credential 表里
    那个账号叫 `sittrapply`、`sittrapprovea`。照收下来，采纳后用例会因为
    "找不到账号"直接失败 —— 而用户看不出是提案的问题。

    实测项目里已经有 2 条用例踩了这个坑（role 写成了中文角色名）。

    所以不在清单里的一律**丢掉**（留空 = 用项目默认账号），而不是留着让它跑的时候炸。
    """

    ROLES = {"sittrapply", "sittrapprovea"}

    def test_a_known_role_is_kept(self):
        got = parse_proposals(
            {"proposals": [{"action": "add", "case": {"prompt": "p"}, "role": "sittrapply"}]},
            known_keys=set(),
            module="M",
            valid_roles=self.ROLES,
        )
        assert got[0].role == "sittrapply"
        assert got[0].payload["role"] == "sittrapply"

    def test_an_invented_role_is_dropped_not_kept(self):
        """★ 编出来的角色名要丢掉。

        留着的后果不是"少了个账号"，而是**这条用例跑不起来**，
        而用户点采纳时是看不出来的。
        """
        got = parse_proposals(
            {"proposals": [{"action": "add", "case": {"prompt": "p"}, "role": "结算单发起人"}]},
            known_keys=set(),
            module="M",
            valid_roles=self.ROLES,
        )
        assert got[0].role == "", "编造的角色被留下了"
        assert got[0].payload["role"] == ""

    def test_case_level_role_cannot_bypass_validation(self):
        """`case.role` 是另一个入口，同样要过校验 —— 否则等于开了个后门。"""
        got = parse_proposals(
            {
                "proposals": [
                    {
                        "action": "add",
                        "role": "sittrapply",
                        "case": {"prompt": "p", "role": "瞎编的"},
                    }
                ]
            },
            known_keys=set(),
            module="M",
            valid_roles=self.ROLES,
        )
        assert got[0].payload["role"] == "sittrapply", "case.role 绕过了校验"

    def test_update_changes_role_must_also_validate(self):
        got = parse_proposals(
            {
                "proposals": [
                    {
                        "action": "update",
                        "case_key": "TC-1",
                        "changes": {"expected": "x", "role": "不存在的角色"},
                    }
                ]
            },
            known_keys={"TC-1"},
            module="M",
            valid_roles=self.ROLES,
        )
        assert "role" not in got[0].payload, f"changes.role 绕过了校验：{got[0].payload}"
        assert "expected" in got[0].payload, "连合法字段也被一起丢了"

    def test_no_account_list_means_no_role_at_all(self):
        """项目还没配账号时，role 一律留空 —— 宁可用默认账号，也不要跑不通的角色。"""
        got = parse_proposals(
            {"proposals": [{"action": "add", "case": {"prompt": "p"}, "role": "any"}]},
            known_keys=set(),
            module="M",
            valid_roles=None,
        )
        assert got[0].role == ""


class TestProposalCarriesFlowAndAccount:
    """★ 端到端：提案里要真的带上**流程图**与**账号**（2026-10-12 用户要求）。

    ## 为什么用"假模型"而不是真调网关

    真调网关在写测试时是不可靠的（实测撞过 402 余额耗尽、空回复），
    而这里要验的是**链路**：模型给的东西有没有被收下、校验、存库、并在采纳时落到用例上。
    假模型让这条链路变成确定的 —— 它与"模型答得好不好"是两件事，
    后者靠人去读提案，不靠测试。

    ## 账号这条为什么最值得测

    模型会照需规里的角色名写（"结算单发起人"），而项目里的账号叫 `sittrapply`。
    不校验的话，采纳后用例会因为"找不到账号"直接失败 ——
    而用户在点采纳时完全看不出来。
    """

    def _seed_credential(self, project_id: int, role: str, *, healthy: bool = True) -> None:
        """直接塞一行凭据。

        不走 `POST /credentials`：那个端点要求配了 `POTATO_SECRET_KEY`（要加密存储），
        而这些测试关心的是"账号清单有没有被用上"，不是加密链路。
        """
        import asyncio

        from app import db as dbmod
        from app.models import Credential

        async def _go() -> None:
            async with dbmod.db_session() as s:
                s.add(
                    Credential(
                        project_id=project_id,
                        type="password",
                        role=role,
                        label=role,
                        username=role,
                        secret="x",
                        is_active=True,
                        healthy=healthy,
                    )
                )
                await s.commit()

        asyncio.run(_go())

    def test_the_module_flow_and_role_are_stored(self, client, monkeypatch):
        pid = _project(client, "prop-flow")
        _case(client, pid, "已有一条", module="审批模块")
        _seed_overview(client, pid, "<h2>审批模块</h2><p>审批链怎么走</p>")
        self._seed_credential(pid, "sittrapply")

        flow = 'flowchart TD\n  A["申请人 提交"] --> B{"审批人 审批"}\n  B -->|"通过"| C["结算"]'

        async def fake_call(system, user, max_tokens=4000):  # noqa: ANN001
            # 模块流程 + 一条带账号的 add 提案
            return (
                {
                    "flow": flow,
                    "proposals": [
                        {
                            "action": "add",
                            "summary": "补一条审批通过后结算的用例",
                            "reason": "需规把审批—结算闭环列为主线",
                            "evidence": ["需规：申请—审批—使用—结算"],
                            "role": "sittrapply",
                            "case": {
                                "name": "审批通过后生成结算单",
                                "module": "审批模块",
                                "prompt": "登录后提交申请，审批通过，检查结算单",
                                "expected": "生成结算单",
                            },
                        }
                    ],
                },
                {"total": 100, "calls": 1},
            )

        monkeypatch.setattr("app.specgen._call_json", fake_call)
        r = client.post(f"/api/projects/{pid}/case-proposals/generate", json={})
        assert r.status_code == 200, r.text
        assert r.json()["created"] == 1, r.json()

        props = client.get(f"/api/projects/{pid}/case-proposals").json()["proposals"]
        assert len(props) == 1
        p = props[0]
        assert p["role"] == "sittrapply", f"账号没存下来：{p}"
        assert p["mermaid"], "流程图没存下来"
        assert "flowchart TD" in p["mermaid"]
        assert p["reason"], "理由没存下来"
        assert p["evidence"], "依据没存下来"

    def test_an_invented_role_never_reaches_the_store(self, client, monkeypatch):
        """★ 模型编的角色名不该进库 —— 进了库就会在采纳后变成跑不通的用例。"""
        pid = _project(client, "prop-badrole")
        _case(client, pid, "占位", module="审批模块")
        _seed_overview(client, pid, "<h2>审批模块</h2><p>x</p>")
        self._seed_credential(pid, "sittrapply")

        async def fake_call(system, user, max_tokens=4000):  # noqa: ANN001
            return (
                {
                    "flow": 'flowchart TD\n  A["甲"] --> B["乙"]',
                    "proposals": [
                        {
                            "action": "add",
                            "summary": "x",
                            "role": "结算单发起人",  # 需规里的角色名，不是账号
                            "case": {"name": "n", "prompt": "p"},
                        }
                    ],
                },
                {"total": 1, "calls": 1},
            )

        monkeypatch.setattr("app.specgen._call_json", fake_call)
        client.post(f"/api/projects/{pid}/case-proposals/generate", json={})
        props = client.get(f"/api/projects/{pid}/case-proposals").json()["proposals"]
        assert props, "提案没生成"
        assert props[0]["role"] == "", f"编造的角色进库了：{props[0]['role']}"

    def test_accepting_an_add_puts_the_account_on_the_case(self, client, monkeypatch):
        """★ 采纳后账号要落到**用例**上 —— 否则附加账号就只是展示，跑起来还是默认账号。"""
        pid = _project(client, "prop-applyrole")
        _case(client, pid, "占位", module="审批模块")
        _seed_overview(client, pid, "<h2>审批模块</h2><p>x</p>")
        self._seed_credential(pid, "sittrapprovea")

        async def fake_call(system, user, max_tokens=4000):  # noqa: ANN001
            return (
                {
                    "flow": 'flowchart TD\n  A["甲"] --> B["乙"]',
                    "proposals": [
                        {
                            "action": "add",
                            "summary": "补审批用例",
                            "role": "sittrapprovea",
                            "case": {
                                "name": "审批通过",
                                "module": "审批模块",
                                "prompt": "以审批人身份审批",
                                "expected": "通过",
                            },
                        }
                    ],
                },
                {"total": 1, "calls": 1},
            )

        monkeypatch.setattr("app.specgen._call_json", fake_call)
        client.post(f"/api/projects/{pid}/case-proposals/generate", json={})
        prop = client.get(f"/api/projects/{pid}/case-proposals").json()["proposals"][0]

        r = client.post(
            f"/api/projects/{pid}/case-proposals/{prop['id']}/decide", json={"decision": "accept"}
        )
        assert r.status_code == 200, r.text
        assert r.json()["applied"][0]["result"] == "created"

        made = next(
            c for c in client.get(f"/api/projects/{pid}/testcases").json() if c["name"] == "审批通过"
        )
        assert made["role"] == "sittrapprovea", f"账号没落到用例上：{made.get('role')}"

    def test_unhealthy_accounts_are_disclosed_to_the_model(self, client, monkeypatch):
        """不可用的账号也要告诉模型 —— 否则它会指定一个跑不通的账号，
        而用户不知道为什么这条用例一跑就失败。"""
        pid = _project(client, "prop-unhealthy")
        _case(client, pid, "占位", module="审批模块")
        _seed_overview(client, pid, "<h2>审批模块</h2><p>x</p>")
        self._seed_credential(pid, "sittrapply", healthy=True)
        self._seed_credential(pid, "sittrapprovea", healthy=False)

        seen: dict = {}

        async def fake_call(system, user, max_tokens=4000):  # noqa: ANN001
            seen["user"] = user
            return ({"flow": "", "proposals": []}, {"total": 1, "calls": 1})

        monkeypatch.setattr("app.specgen._call_json", fake_call)
        client.post(f"/api/projects/{pid}/case-proposals/generate", json={})
        prompt = seen.get("user") or ""
        assert "sittrapply" in prompt, "可用账号没进提示词"
        assert "sittrapprovea" in prompt, "不可用账号没进提示词"
        assert "不可用" in prompt, "没标明哪个不可用"


class TestParseProposals:
    """模型很爱给变体，收口必须容错，且**不能把不存在的用例编号收进来**。"""

    def test_a_valid_add_proposal(self):
        got = parse_proposals(
            {
                "proposals": [
                    {
                        "action": "add",
                        "summary": "补一条并发提交用例",
                        "reason": "需规要求幂等",
                        "evidence": ["R-提交：重复提交应被拒绝"],
                        "case": {"name": "并发提交", "prompt": "连点两次提交", "expected": "只成功一次"},
                    }
                ]
            },
            known_keys=set(),
            module="提交模块",
        )
        assert len(got) == 1
        assert got[0].action == "add"
        assert got[0].payload["prompt"] == "连点两次提交"
        assert got[0].payload["module"] == "提交模块", "模块应回落到当前批次"

    def test_add_without_a_prompt_is_dropped(self):
        """没有任务描述的用例跑不了，收进来只会污染用例库。"""
        got = parse_proposals(
            {"proposals": [{"action": "add", "summary": "x", "case": {"name": "无任务"}}]},
            known_keys=set(),
            module="M",
        )
        assert got == []

    def test_unknown_action_is_dropped(self):
        got = parse_proposals(
            {"proposals": [{"action": "rewrite", "summary": "x"}, {"action": "add"}]},
            known_keys=set(),
            module="M",
        )
        assert got == []

    def test_update_and_remove_must_reference_a_known_case(self):
        """★ 模型会编出清单里不存在的编号。

        照收下来就会生成"改一条不存在的用例"的提案，用户点采纳时才发现 ——
        而那时他已经信了这份清单。
        """
        bad = parse_proposals(
            {"proposals": [{"action": "remove", "case_key": "TP-FAKE-999", "summary": "删"}]},
            known_keys={"TP-A-001"},
            module="M",
        )
        assert bad == [], "不存在的编号被收进来了"
        good = parse_proposals(
            {"proposals": [{"action": "remove", "case_key": "TP-A-001", "summary": "删"}]},
            known_keys={"TP-A-001"},
            module="M",
        )
        assert len(good) == 1

    def test_update_keeps_only_allowed_fields(self):
        got = parse_proposals(
            {
                "proposals": [
                    {
                        "action": "update",
                        "case_key": "TP-A-001",
                        "summary": "改预期",
                        "changes": {"expected": "新预期", "id": 999, "created_at": "x"},
                    }
                ]
            },
            known_keys={"TP-A-001"},
            module="M",
        )
        assert got[0].payload == {"expected": "新预期"}, "不该让模型改 id/创建时间"

    def test_update_with_no_usable_changes_is_dropped(self):
        got = parse_proposals(
            {"proposals": [{"action": "update", "case_key": "TP-A-001", "changes": {"id": 1}}]},
            known_keys={"TP-A-001"},
            module="M",
        )
        assert got == []

    def test_bogus_shapes_do_not_crash(self):
        for bad in ({}, {"proposals": "不是列表"}, {"proposals": [None, 1, "x"]}, "不是字典", None):
            parse_proposals(bad, known_keys={"A"}, module="M")

    def test_priority_is_normalized(self):
        for given, want in (("p0", "P0"), ("P2", "P2"), ("URGENT", "P1"), (None, "P1")):
            got = parse_proposals(
                {"proposals": [{"action": "add", "case": {"prompt": "x", "priority": given}}]},
                known_keys=set(),
                module="M",
            )
            assert got[0].payload["priority"] == want


class TestFlowDirection:
    """★ 过长的横向流程图要改成纵向（2026-10-12）。

    实测：7 个节点的 `flowchart LR` 渲染出 1320x76 —— **17:1**。
    缩进提案页 950px 宽的容器后每个节点只剩十几像素，文字看不清。
    图是用来判断"为什么要改这条用例"的，看不清等于没有。

    提示词里已经让模型按步骤数选方向，但模型不总听，所以代码里兜一道。
    """

    def test_a_long_lr_chain_becomes_td(self):
        src = (
            'flowchart LR\n  A["甲"] --> B["乙"]\n  B --> C["丙"]\n'
            '  C --> D["丁"]\n  D --> E["戊"]'
        )
        out = _fix_flow_direction(src)
        assert out.split("\n")[0].strip() == "flowchart TD"
        # 只改方向那一行，节点与边必须原样保留
        assert 'A["甲"] --> B["乙"]' in out
        assert 'D["丁"]' in out

    def test_a_short_lr_chain_is_left_alone(self):
        """≤4 步时 LR 正好（扁扁的填满宽度），不该动。"""
        src = 'flowchart LR\n  A["甲"] --> B["乙"]\n  B --> C["丙"]'
        assert _fix_flow_direction(src).split("\n")[0].strip() == "flowchart LR"

    def test_td_is_never_touched(self):
        src = 'flowchart TD\n  A["甲"] --> B["乙"]\n  B --> C["丙"]\n  C --> D["丁"]\n  D --> E["戊"]'
        assert _fix_flow_direction(src) == src

    def test_a_leading_comment_does_not_confuse_it(self):
        """`%%` 注释可能在声明之前 —— 声明行不一定是第一行。"""
        src = (
            '%% 说明\nflowchart LR\n  A["甲"] --> B["乙"]\n  B --> C["丙"]\n'
            '  C --> D["丁"]\n  D --> E["戊"]'
        )
        out = _fix_flow_direction(src)
        assert "flowchart TD" in out
        assert "%% 说明" in out, "注释被吃掉了"

    def test_empty_is_safe(self):
        assert _fix_flow_direction("") == ""

    def test_a_proposal_flow_with_no_edges_is_safe(self):
        assert _fix_flow_direction("flowchart LR\n  A[\"孤立节点\"]") == "flowchart LR\n  A[\"孤立节点\"]"


class TestBuildPrompt:
    def test_includes_module_cases_and_spec(self):
        p = build_prompt("模块A", [{"case_key": "TP-A-001", "name": "进列表", "expected": "有数据"}], "需规正文")
        assert "模块A" in p
        assert "TP-A-001" in p
        assert "需规正文" in p

    def test_discloses_truncation(self):
        """截断了必须说明 —— 否则模型会对没看到的部分照样下结论。"""
        cases = [{"case_key": f"K{i}", "name": f"用例{i}"} for i in range(100)]
        p = build_prompt("M", cases, "x")
        assert "只列了前" in p
        assert "不要提 remove" in p

    def test_prompt_stays_under_the_gateway_limit(self):
        """★ 提示词总长必须低于网关的"空回复"边界（2026-10-12）。

        实测：这个话题的网关对超过约 4.5~5k 字符的 user 提示词
        返回 HTTP 200 + **空内容**（无报错）。40 条用例的完整提示词约 9700 字符，
        于是 12 个模块**全部**失败（"共 0 字符"），一次都跑不成。

        这条测试就是防止有人再把上限调大回去 —— 调大不会报错，
        只会在真跑的时候一个模块都出不来。
        """
        cases = [
            {
                "case_key": f"TP-X-{i:03d}",
                "name": f"很长的用例名称{'甲' * 40}{i}",
                "expected": "预期的内容" * 20,
                "prompt": "给浏览器 agent 的任务描述" * 20,
                "role": "sittrapprovea",
            }
            for i in range(200)
        ]
        p = build_prompt("M", cases, "需规正文" * 2000, [{"role": "sittrapprovea", "healthy": True}])
        assert len(p) < 5000, f"提示词 {len(p)} 字符，会撞上网关的空回复边界"

    def test_module_with_no_cases_says_so(self):
        assert "还没有任何用例" in build_prompt("空模块", [], "需规")


class TestGroupCasesByModule:
    def test_groups_and_counts(self):
        cases = [
            {"module": "A", "status": "active", "case_key": "1"},
            {"module": "A", "status": "active", "case_key": "2"},
            {"module": "B", "status": "active", "case_key": "3"},
        ]
        got = dict(group_cases_by_module(cases))
        assert len(got["A"]) == 2
        assert len(got["B"]) == 1

    def test_deprecated_cases_are_excluded(self):
        """对已作废的用例提"改/删"没有意义，而且会干扰判重。

        一条作废的和一条有效的内容相同，模型会把它报成"重复" ——
        于是用户收到一条"删掉这条有效的"的错误建议。
        """
        cases = [
            {"module": "A", "status": "active", "case_key": "1"},
            {"module": "A", "status": "deprecated", "case_key": "2"},
        ]
        got = dict(group_cases_by_module(cases))
        assert [c["case_key"] for c in got["A"]] == ["1"]

    def test_blank_module_becomes_uncategorized(self):
        got = dict(group_cases_by_module([{"module": "", "status": "active", "case_key": "1"}]))
        assert "未分类" in got


class TestSpecTextForModule:
    def test_picks_the_matching_block(self):
        html = "<h2>模块A</h2><p>模块A 的规则</p><h2>模块B</h2><p>模块B 的规则</p>"
        got = spec_text_for_module(html, "模块A")
        assert "模块A 的规则" in got
        assert "模块B 的规则" not in got

    def test_falls_back_to_whole_doc(self):
        html = "<h2>别的</h2><p>内容</p>"
        assert "内容" in spec_text_for_module(html, "找不到的模块")

    def test_empty_input(self):
        assert spec_text_for_module("", "M") == ""


# ---------------------------------------------------------------------------
# 边界：AI 不能自己改用例库
# ---------------------------------------------------------------------------
def test_generate_requires_a_project_document(client):
    """没有项目文档就没有优化的依据 —— 要如实说不，而不是凭空提一堆建议。"""
    pid = _project(client, "no-doc")
    _case(client, pid, "进列表")
    r = client.post(f"/api/projects/{pid}/case-proposals/generate", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "项目文档" in body["error"]


def test_generate_requires_existing_cases(client):
    pid = _project(client, "no-case")
    _seed_overview(client, pid, "<h2>项目总览</h2><p>有内容</p>")
    r = client.post(f"/api/projects/{pid}/case-proposals/generate", json={})
    assert r.json()["ok"] is False
    assert "用例" in r.json()["error"]


def test_generate_stores_proposals_without_touching_cases(client, monkeypatch):
    """★ 这条是本功能最核心的不变量。

    生成阶段只写 `case_proposal`。如果它顺手动了 `test_case`，
    那用户点的"审阅"就成了摆设 —— 而这正是他明确不要的。
    """
    pid = _project(client, "gen-only")
    c = _case(client, pid, "进列表")
    _seed_overview(client, pid, "<h2>模块A</h2><p>模块A 要支持并发提交</p>")

    async def fake(groups, accounts=None):
        from app.case_optimize import Proposal

        return (
            [
                Proposal(
                    action="add",
                    summary="补并发提交",
                    reason="需规要求",
                    evidence=["模块A 要支持并发提交"],
                    payload={"name": "并发提交", "prompt": "连点两次", "expected": "只成功一次", "module": "模块A"},
                ),
                Proposal(action="remove", case_key=c["case_key"], summary="这条多余", reason="与另一条重复"),
            ],
            {"total": 10, "calls": 1},
            0,
            [],
            {},
        )

    monkeypatch.setattr("app.case_optimize.optimize_project", fake)
    r = client.post(f"/api/projects/{pid}/case-proposals/generate", json={})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 2

    # 用例库**一条都没变**
    cases = client.get(f"/api/projects/{pid}/testcases").json()
    assert len(cases) == 1, f"生成阶段动了用例库：{len(cases)} 条"
    assert cases[0]["status"] == "active", "生成阶段不该作废用例"

    props = client.get(f"/api/projects/{pid}/case-proposals").json()
    assert props["counts"]["pending"] == 2
    assert props["pending_by_action"] == {"add": 1, "remove": 1}


def test_regenerating_does_not_duplicate_the_same_proposal(client, monkeypatch):
    """重新生成不该把同一批提案翻倍 —— 否则用户会看到两份一样的"删除这条"。"""
    pid = _project(client, "gen-dup")
    _case(client, pid, "进列表")
    _seed_overview(client, pid, "<h2>模块A</h2><p>x</p>")

    async def fake(groups, accounts=None):
        from app.case_optimize import Proposal

        return (
            [Proposal(action="add", summary="补一条", payload={"name": "a", "prompt": "p"})],
            {"total": 1, "calls": 1},
            0,
            [],
            {},
        )

    monkeypatch.setattr("app.case_optimize.optimize_project", fake)
    client.post(f"/api/projects/{pid}/case-proposals/generate", json={})
    r2 = client.post(f"/api/projects/{pid}/case-proposals/generate", json={})
    assert r2.json()["created"] == 0, "重复提案没有被去重"
    assert client.get(f"/api/projects/{pid}/case-proposals").json()["counts"]["pending"] == 1


# ---------------------------------------------------------------------------
# "没问题"不等于"没审成功"
# ---------------------------------------------------------------------------
def test_an_empty_result_is_a_success_not_a_failure(client, monkeypatch):
    """★ 这是一次真实误报（2026-10-12）。

    原来的判据是"调用没抛异常就算成功，靠 usage 是不是空来判失败"，
    而模型对一个**已经覆盖良好**的模块，正确的回答就是 `{"proposals": []}`
    （usage 非空、提案为空）。旧代码把它当成失败，于是界面上报
    "有 N 个模块失败"，让用户以为漏审了 —— 实际上那批已经审过、结论是"没问题"。

    这个区分很重要：用户会**再点一次**去"补齐"那些模块，
    而重试永远不会产生提案，他只会一直看到失败。
    """
    pid = _project(client, "empty-ok")
    _case(client, pid, "覆盖良好的用例")
    _seed_overview(client, pid, "<h2>M</h2><p>x</p>")

    async def fake(groups, accounts=None):
        # 模型成功返回、但一条提案都没有
        return [], {"total": 500, "calls": 1}, 0, [], {}

    monkeypatch.setattr("app.case_optimize.optimize_project", fake)
    r = client.post(f"/api/projects/{pid}/case-proposals/generate", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["created"] == 0
    assert body["modules_failed"] == 0, "把'没有问题'报成了'审失败'"
    assert body["failed_modules"] == []


def test_failed_modules_are_named_not_just_counted(client, monkeypatch):
    """只说"有 2 个模块失败"，用户不知道是哪 2 个，也就无法判断要不要补。"""
    pid = _project(client, "named-fail")
    _case(client, pid, "占位")
    _seed_overview(client, pid, "<h2>M</h2><p>x</p>")

    async def fake(groups, accounts=None):
        return [], {"total": 1, "calls": 1}, 2, ["模块甲", "模块乙"], {}

    monkeypatch.setattr("app.case_optimize.optimize_project", fake)
    r = client.post(f"/api/projects/{pid}/case-proposals/generate", json={})
    assert r.json()["modules_failed"] == 2
    assert r.json()["failed_modules"] == ["模块甲", "模块乙"]


# ---------------------------------------------------------------------------
# 采纳的三种落地方式
# ---------------------------------------------------------------------------
def _make_proposal(client, pid, monkeypatch, prop, *, case=None):
    """借 generate 端点把一条手工提案写进库（避免直接操作 ORM）。"""
    _seed_overview(client, pid, "<h2>M</h2><p>x</p>")
    if case is None:
        case = _case(client, pid, "占位")

    async def fake(groups, accounts=None):
        return ([prop], {"total": 1, "calls": 1}, 0, [], {id(prop): "模块A"})

    monkeypatch.setattr("app.case_optimize.optimize_project", fake)
    client.post(f"/api/projects/{pid}/case-proposals/generate", json={})
    props = client.get(f"/api/projects/{pid}/case-proposals").json()["proposals"]
    return props[0]["id"]


def test_accepting_an_add_creates_a_case(client, monkeypatch):
    from app.case_optimize import Proposal

    pid = _project(client, "acc-add")
    _case(client, pid, "占位")
    prop = Proposal(
        action="add",
        summary="补并发提交",
        evidence=["需规：并发提交"],
        payload={"name": "并发提交", "prompt": "连点两次提交", "expected": "只成功一次", "module": "模块A"},
    )
    pid_prop = _make_proposal(client, pid, monkeypatch, prop)

    r = client.post(f"/api/projects/{pid}/case-proposals/{pid_prop}/decide", json={"decision": "accept"})
    assert r.status_code == 200, r.text
    assert r.json()["applied"][0]["result"] == "created"

    cases = client.get(f"/api/projects/{pid}/testcases").json()
    made = [c for c in cases if c["name"] == "并发提交"]
    assert len(made) == 1, "新增提案没有落成用例"
    assert made[0]["case_key"], "新用例必须有编号"
    assert "AI 提案" in (made[0].get("source") or ""), "新用例要留下出处"


def test_accepting_a_remove_only_deprecates(client, monkeypatch):
    """★ 不真删。

    删掉之后那条编号会从报告里消失，而真删不可逆 —— 用户想反悔时没有任何办法。
    作废可以在用例页改回来。
    """
    from app.case_optimize import Proposal

    pid = _project(client, "acc-remove")
    c = _case(client, pid, "要删掉的")
    prop = Proposal(action="remove", case_key=c["case_key"], summary="多余", reason="重复")
    pid_prop = _make_proposal(client, pid, monkeypatch, prop)

    r = client.post(f"/api/projects/{pid}/case-proposals/{pid_prop}/decide", json={"decision": "accept"})
    assert r.json()["applied"][0]["result"] == "deprecated"

    cases = client.get(f"/api/projects/{pid}/testcases").json()
    mine = [x for x in cases if x["id"] == c["id"]]
    assert len(mine) == 1, "用例被真删了 —— 应该只是作废"
    assert mine[0]["status"] == "deprecated"


def test_accepting_an_update_merges_only_given_fields(client, monkeypatch):
    """只合并提案里出现的字段，不整条覆盖 —— AI 只该改它看见的那几处。"""
    from app.case_optimize import Proposal

    pid = _project(client, "acc-update")
    c = _case(client, pid, "原用例", expected="老的预期")
    prop = Proposal(
        action="update",
        case_key=c["case_key"],
        summary="改预期",
        payload={"expected": "新的预期"},
    )
    pid_prop = _make_proposal(client, pid, monkeypatch, prop)
    client.post(f"/api/projects/{pid}/case-proposals/{pid_prop}/decide", json={"decision": "accept"})

    after = next(x for x in client.get(f"/api/projects/{pid}/testcases").json() if x["id"] == c["id"])
    assert after["expected"] == "新的预期"
    assert after["name"] == "原用例", "没提议改的字段被覆盖了"
    assert after["prompt"] == c["prompt"], "prompt 被覆盖了"


def test_rejecting_leaves_everything_alone(client, monkeypatch):
    from app.case_optimize import Proposal

    pid = _project(client, "rej")
    c = _case(client, pid, "别动我")
    prop = Proposal(action="remove", case_key=c["case_key"], summary="多余")
    pid_prop = _make_proposal(client, pid, monkeypatch, prop)

    r = client.post(f"/api/projects/{pid}/case-proposals/{pid_prop}/decide", json={"decision": "reject"})
    assert r.json()["applied"][0]["result"] == "rejected"
    after = next(x for x in client.get(f"/api/projects/{pid}/testcases").json() if x["id"] == c["id"])
    assert after["status"] == "active", "驳回之后用例被动过了"


def test_a_proposal_can_only_be_decided_once(client, monkeypatch):
    """二次采纳必须被拒 —— 否则一次误点会重复建用例。"""
    from app.case_optimize import Proposal

    pid = _project(client, "twice")
    _case(client, pid, "占位")
    prop = Proposal(action="add", summary="补", payload={"name": "新", "prompt": "p"})
    pid_prop = _make_proposal(client, pid, monkeypatch, prop)

    client.post(f"/api/projects/{pid}/case-proposals/{pid_prop}/decide", json={"decision": "accept"})
    r2 = client.post(f"/api/projects/{pid}/case-proposals/{pid_prop}/decide", json={"decision": "accept"})
    assert r2.json()["applied"] == []
    assert r2.json()["skipped"], "重复采纳没有被拒"
    assert len([x for x in client.get(f"/api/projects/{pid}/testcases").json() if x["name"] == "新"]) == 1


def test_bulk_decide(client, monkeypatch):
    from app.case_optimize import Proposal

    pid = _project(client, "bulk")
    _case(client, pid, "占位")
    _seed_overview(client, pid, "<h2>M</h2><p>x</p>")

    async def fake(groups, accounts=None):
        return (
            [
                Proposal(action="add", summary="a1", payload={"name": "N1", "prompt": "p1"}),
                Proposal(action="add", summary="a2", payload={"name": "N2", "prompt": "p2"}),
            ],
            {"total": 1, "calls": 1},
            0,
            [],
            {},
        )

    monkeypatch.setattr("app.case_optimize.optimize_project", fake)
    client.post(f"/api/projects/{pid}/case-proposals/generate", json={})
    ids = [p["id"] for p in client.get(f"/api/projects/{pid}/case-proposals").json()["proposals"]]
    r = client.post(f"/api/projects/{pid}/case-proposals/decide", json={"ids": ids, "decision": "accept"})
    assert len(r.json()["applied"]) == 2
    names = {x["name"] for x in client.get(f"/api/projects/{pid}/testcases").json()}
    assert {"N1", "N2"} <= names


def test_bulk_decide_needs_ids(client):
    pid = _project(client, "bulk-empty")
    r = client.post(f"/api/projects/{pid}/case-proposals/decide", json={"ids": [], "decision": "accept"})
    assert r.status_code == 400


def test_bad_decision_is_rejected(client, monkeypatch):
    from app.case_optimize import Proposal

    pid = _project(client, "bad-dec")
    _case(client, pid, "占位")
    pid_prop = _make_proposal(client, pid, monkeypatch, Proposal(action="add", summary="x", payload={"prompt": "p"}))
    r = client.post(f"/api/projects/{pid}/case-proposals/{pid_prop}/decide", json={"decision": "maybe"})
    assert r.status_code == 400


def test_proposals_are_scoped_to_their_project(client, monkeypatch):
    """跨项目读/写别人的提案必须被拒 —— 这是多项目隔离的一部分。"""
    from app.case_optimize import Proposal

    a = _project(client, "iso-a")
    b = _project(client, "iso-b")
    _case(client, a, "占位")
    _case(client, b, "占位")
    pid_prop = _make_proposal(client, a, monkeypatch, Proposal(action="add", summary="x", payload={"prompt": "p"}))

    r = client.post(f"/api/projects/{b}/case-proposals/{pid_prop}/decide", json={"decision": "accept"})
    assert r.json()["applied"] == [] and r.json()["skipped"], "跨项目动到了别的项目的提案"
    # B 项目看不到 A 的提案
    assert client.get(f"/api/projects/{b}/case-proposals").json()["proposals"] == []


# ---------------------------------------------------------------------------
# 人工确认过的通过率
# ---------------------------------------------------------------------------
def test_verified_coverage_ignores_auto_verdicts(client):
    """★ 只认人工改判过的结果。

    自动判定里有相当一部分不是产品结论（环境没起来、agent 没做完、判定器挂了）。
    把它们当失败算进通过率，会让一个模块看起来千疮百孔，
    而实际上可能一条真缺陷都没有 —— 用户拿着一份不可信的数字做不了决定。
    """
    pid = _project(client, "cov")
    c1 = _case(client, pid, "用例1", module="模块A")
    c2 = _case(client, pid, "用例2", module="模块A")
    c3 = _case(client, pid, "用例3", module="模块A")

    # 直接造结果行，而不是走 POST /runs：那个端点会**异步**去跑浏览器，
    # 在测试里既慢又拿不到确定的结果行（实测断言时是 0 行）。
    # 这里要验的是统计口径，不是执行链路，所以自己把数据摆好。
    import asyncio

    from app import db as dbmod
    from app.models import Run, RunResult

    async def _seed():
        async with dbmod.db_session() as s:
            run = Run(project_id=pid, name="R", status="completed")
            s.add(run)
            await s.flush()
            s.add_all(
                [
                    # 人工改判为通过（自动判定是 failed —— 不该被算进去）
                    RunResult(run_id=run.id, case_id=c1["id"], status="failed", verdict_override="passed"),
                    # 人工改判为失败（自动判定是 passed）
                    RunResult(run_id=run.id, case_id=c2["id"], status="passed", verdict_override="failed"),
                    # 只有自动判定，没人看过
                    RunResult(run_id=run.id, case_id=c3["id"], status="failed", verdict_override=None),
                ]
            )
            await s.commit()

    asyncio.run(_seed())

    cov = client.get(f"/api/projects/{pid}/verified-coverage").json()
    mod = next(m for m in cov["modules"] if m["module"] == "模块A")
    assert mod["verified_total"] == 2, f"把自动判定算进人工确认了：{mod}"
    assert mod["verified_pass"] == 1
    assert mod["verified_rate"] == 50.0
    assert mod["awaiting_review"] == 1, "待人工确认的条数没有如实报出"
    assert cov["summary"]["verified_total"] == 2
    assert cov["summary"]["awaiting_review"] == 1


def test_verified_coverage_with_nothing_verified(client):
    """没有任何人工确认时，通过率必须是 None 而不是 0。

    显示 0% 会被读成"全挂了"，而事实是"没人看过" —— 这两件事的处置完全相反。
    """
    pid = _project(client, "cov-empty")
    _case(client, pid, "用例1", module="模块A")
    cov = client.get(f"/api/projects/{pid}/verified-coverage").json()
    assert cov["summary"]["verified_total"] == 0
    assert cov["summary"]["verified_rate"] is None
    if cov["modules"]:
        assert cov["modules"][0]["verified_rate"] is None
