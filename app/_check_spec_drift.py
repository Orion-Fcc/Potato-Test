"""Run spec-drift detection against the real 501-case suite.

Unit tests can only prove the rules behave; this proves they fire on the actual data.
The expectation comes from the 2026-10-07 audit: the suite contained cases asserting the
old「无需审批、平台自动同步」 behaviour, which the implementation no longer has.

Read-only. Touches no case, no DB write, no browser.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(r"D:\agent_study\Potato Test")
sys.path.insert(0, str(ROOT))

from app.spec_drift import describe, find_all, find_contradictory_expectations  # noqa: E402

DB = ROOT / "potato.db"


def load_cases() -> list[dict]:
    if not DB.is_file():
        return []
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT case_key, name, expected, project_id FROM test_case"
        ).fetchall()
    except sqlite3.Error as exc:
        print(f"[SKIP] 读 test_case 失败：{exc}")
        return []
    finally:
        con.close()
    return [
        {"case_key": r[0], "name": r[1], "expected": r[2] or "", "project_id": r[3]}
        for r in rows
    ]


def main() -> int:
    cases = load_cases()
    if not cases:
        print("库里没有用例，跳过。")
        return 0

    print(f"用例总数：{len(cases)}\n")
    sigs = find_all(cases, None, observed_requires_approval=True)

    print("=== 套件自相矛盾（不依赖实测，最强信号）===")
    contra = find_contradictory_expectations(cases)
    if not contra:
        print("  无")
    for s in contra:
        print(f"  [{s.severity}] {s.detail}")
        print(f"         {s.evidence}")
        print(f"         涉及 {len(s.case_ids)} 条用例：{', '.join(list(s.case_ids)[:8])}")

    print("\n=== 全部信号 ===")
    print(describe(sigs))
    for s in sigs:
        print(f"\n  [{s.severity}] {s.kind}")
        print(f"    {s.detail}")
        if s.evidence:
            print(f"    证据：{s.evidence[:160]}")
        if s.case_ids:
            print(f"    用例（{len(s.case_ids)}）：{', '.join(list(s.case_ids)[:10])}")

    out = ROOT / "_drift_report.json"
    out.write_text(json.dumps([s.as_dict() for s in sigs], ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细已写入 {out.name}（{out.stat().st_size} bytes）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())