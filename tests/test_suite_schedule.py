"""套件到期自动执行：排期算法本身。

为什么这里只测纯函数（不碰库、不碰启动）：`sweep_due_suites` 的行为有一半由数据库的
比较并交换（CAS）保证，那部分靠"两个 sweeper 同时读同一行"才能验，摆进单测只会写成
一个假装并发的假测试。真正容易写错、且写错了很难发现的是**日期推进**：

* monthly 用 `+30 天` 会漂 —— 1 月 31 日的"每月"会漂到 3 月 2 日，然后越漂越远；
* SQLite 对 `timezone=True` 的列仍然返回 naive datetime，直接和 aware 的 now 比会
  TypeError，整个扫描会在第一行就死掉，表现却是"排期静默不生效"。

这两条都在下面钉住。
"""

from __future__ import annotations

import pathlib
import sys
from datetime import UTC, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.suite_schedule import is_due, next_due  # noqa: E402


class _Suite:
    def __init__(self, cadence="weekly", due_at=None) -> None:
        self.cadence = cadence
        self.due_at = due_at


# ── next_due ─────────────────────────────────────────────────────────────────


def test_daily_weekly_biweekly_advance_by_fixed_days():
    base = datetime(2026, 3, 10, 9, 0, tzinfo=UTC)
    assert next_due("daily", base) == datetime(2026, 3, 11, 9, 0, tzinfo=UTC)
    assert next_due("weekly", base) == datetime(2026, 3, 17, 9, 0, tzinfo=UTC)
    assert next_due("biweekly", base) == datetime(2026, 3, 24, 9, 0, tzinfo=UTC)


def test_monthly_clamps_the_day_instead_of_overflowing_into_the_next_month():
    """1 月 31 日的"每月"应当落在 2 月最后一天，而不是 3 月 2 日。

    用 `+30 天` 就会漂；对写下 "monthly" 的人来说，日期落在正确的月份才是重点。
    """
    assert next_due("monthly", datetime(2026, 1, 31, 8, 0, tzinfo=UTC)) == datetime(
        2026, 2, 28, 8, 0, tzinfo=UTC
    )
    # 闰年 2 月 29 天
    assert next_due("monthly", datetime(2028, 1, 31, 8, 0, tzinfo=UTC)) == datetime(
        2028, 2, 29, 8, 0, tzinfo=UTC
    )


def test_monthly_crosses_the_year_boundary():
    assert next_due("monthly", datetime(2026, 12, 15, 8, 0, tzinfo=UTC)) == datetime(
        2027, 1, 15, 8, 0, tzinfo=UTC
    )


def test_no_cadence_has_no_next_due_date():
    """none / 空 / 拼错的值一律返回 None。

    绝不能把 None 当成"立刻到期" —— 那会让每个没有排期的套件每轮都被触发一次。
    """
    base = datetime(2026, 3, 10, tzinfo=UTC)
    for c in ("none", "", None, "  ", "hourly", "Daily-ish"):
        assert next_due(c, base) is None, c


def test_cadence_matching_is_case_and_space_insensitive():
    base = datetime(2026, 3, 10, tzinfo=UTC)
    assert next_due("  WEEKLY ", base) == next_due("weekly", base)


# ── is_due ───────────────────────────────────────────────────────────────────


def test_not_due_before_the_due_date():
    now = datetime(2026, 3, 10, 9, 0, tzinfo=UTC)
    s = _Suite("weekly", datetime(2026, 3, 17, 9, 0, tzinfo=UTC))
    assert is_due(s, now) is False


def test_due_once_the_date_has_passed():
    now = datetime(2026, 3, 17, 9, 0, tzinfo=UTC)
    s = _Suite("weekly", datetime(2026, 3, 17, 9, 0, tzinfo=UTC))
    assert is_due(s, now) is True


def test_a_cadence_without_a_due_date_is_never_due():
    """有 cadence 但没排期 = 首轮还没安排。

    这里替它"发明"一个到期时间，就等于系统自己启动了没人排过的套件。
    """
    assert is_due(_Suite("weekly", None), datetime(2026, 3, 10, tzinfo=UTC)) is False


def test_no_cadence_is_never_due_even_if_a_stale_due_date_is_left_over():
    """cadence 被改回 none 后，残留的 due_at 不能再触发任何东西。"""
    past = datetime(2020, 1, 1, tzinfo=UTC)
    assert is_due(_Suite("none", past), datetime(2026, 3, 10, tzinfo=UTC)) is False


def test_a_naive_due_date_from_sqlite_is_coerced_not_crashed_on():
    """SQLite 会回 naive datetime；不强制成 UTC 就会 TypeError。

    真出问题时的表象是"排期静默失效"——扫描器第一行抛异常、被外层 except 吞掉，
    日志里只有一行 warning。所以这条必须钉住。
    """
    naive = datetime(2026, 3, 17, 9, 0)  # no tzinfo, as SQLite returns it
    now = datetime(2026, 3, 18, 9, 0, tzinfo=UTC)
    assert is_due(_Suite("weekly", naive), now) is True
