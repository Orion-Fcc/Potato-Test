"""数据隔离：预期里写死数字的用例，第一次对、第二次必然假失败。

现象（本项目实测，不是理论）：case 2 同一条用例三次运行，判定里提到的记录数是
4 / 11 / 5 —— 因为列表里本来就有 11 条规则（前面 14 条新增类用例跑完留下的）。

**会假失败的是预期里写死了绝对数字的那批**。扫出 12 条，例如：
  case 14 「聚合展示」  预期"该规则只占 1 行"   → 上次跑完那条还在，就是 2 行
  case 45 「新增：正常填写后保存」  预期"列表中存在 1 行 SIT手测-场地结算"
  case 62 「业务后果」   预期"核对列表两行金额"

这叫**自污染**：测试自己制造了让后续（以及自己下次）失败的脏数据。
"""
import re

from app.data_hygiene import DEFAULT_DATA_HYGIENE, render
from app.executor import CaseSpec, build_task

# 扫出来的这批：expected 里写死了「N 条 / N 行」
HARD_CODED = (14, 17, 45, 46, 48, 61, 62, 177, 203, 208, 306, 311)


def test_not_injected_when_absent() -> None:
    """没填 data_hygiene 就不注入 —— 不能给每个用例都塞一段无关的话。"""
    t = build_task(CaseSpec(case_id=1, prompt="新增一条规则并保存"), "中文")
    assert "残留" not in t and "当成缺陷" not in t, "未填时不应注入隔离提示"
    # 空格/None 同样不注入
    assert render(None) == ""
    assert render("   \n  ") == ""


def test_injected_when_present() -> None:
    s = CaseSpec(
        case_id=1,
        prompt="新增一条规则并保存",
        expected="列表中出现 1 行",
        data_hygiene=DEFAULT_DATA_HYGIENE,
    )
    t = build_task(s, "中文")
    assert "不要把环境里已有的数据当成缺陷" in t
    # 关键约束必须都在：别拿总数当依据、缺前置数据要说缺
    assert "总共有几条" in t, "必须禁止以列表总条数为判断依据（那正是 case 2 翻车的原因）"
    assert "缺少核对所需的前置数据" in t, "必须要求缺前置数据时如实说明而不是自行造数"


def test_data_hygiene_comes_after_the_task() -> None:
    """顺序：先给任务、再给隔离约定 —— 否则读起来像任务本身的一部分。"""
    s = CaseSpec(
        case_id=1, prompt="TASK_MARKER", data_hygiene=DEFAULT_DATA_HYGIENE
    )
    t = build_task(s, "中文")
    assert t.index("TASK_MARKER") < t.index("不要把环境里已有的数据当成缺陷")


def test_default_text_names_the_real_failure_mode() -> None:
    """默认文案必须点名"自污染"这个真实机制，不能泛泛而谈。"""
    assert "其他用例运行留下的" in DEFAULT_DATA_HYGIENE or "之前某次运行" in DEFAULT_DATA_HYGIENE
    # 不能出现"总数应该等于 N"这类反向指导
    assert not re.search(r"总数(应该|应当)为", DEFAULT_DATA_HYGIENE)


def test_spec_defaults_to_none() -> None:
    """老调用点不传这个参数也不能炸。"""
    assert CaseSpec(case_id=1, prompt="x").data_hygiene is None
