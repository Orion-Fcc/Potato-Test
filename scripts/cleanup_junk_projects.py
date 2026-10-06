"""清理测试写进真实数据库的垃圾项目 —— 默认**只预览**，删了才能恢复。

背景（2026-10-06）
------------------
`tests/test_knowledge_chunks.py` 没有做数据库隔离，每跑一次就在用户的 `potato.db`
里建 4 个项目（`kb-roundtrip` / `kb-replace` / `kb-a` / `kb-b`）。那天跑了 6 次全量
套件，用户的项目列表里就多了 24 个空壳 —— 界面上一眼就能看见，却极难定位到是哪个
测试干的。现在那个测试已经隔离，并且 conftest 里有"预防 + 探测"两道防线。

本脚本的立场
------------
删除**生产数据**这件事，不该由一个脚本顺手做掉。所以：

1. **默认只预览**，打印将要删除的每一行，不动任何数据。
2. `--apply` 才真的删，而且**先自动备份** `potato.db` 到 `.backups/`。
3. 只删**同时**满足三个条件的行：名字以 `kb-` 或 `PROBE-` 开头、URL 是占位地址
   （`http://x` / `http://probe`）、且**没有任何**用例/运行/凭据/知识分块挂在它下面。
   任何一条不满足就跳过并说明原因 —— 宁可少删，不可误删。
4. 永远不碰项目 1、2（用户自己的项目），并且删之前会把它们列出来对照。

用法
----
    python scripts/cleanup_junk_projects.py            # 预览（默认，什么都不改）
    python scripts/cleanup_junk_projects.py --apply    # 备份后删除
    python scripts/cleanup_junk_projects.py --apply --yes   # 不再要交互确认
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import sqlite3
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
DB = ROOT / "potato.db"
BACKUP_DIR = ROOT / ".backups"

JUNK_PREFIXES = ("kb-", "PROBE-")
JUNK_URLS = ("http://x", "http://probe")
# 用户自己的项目，任何情况下都不许动
PROTECTED_IDS = (1, 2)

# 有下游引用的表：任何一个非零都说明这个项目不是空壳，不能删。
# 全部都是"按 project_id 直查"的形状 —— 有例外，所以例外另开一张表（见下）。
CHILD_TABLES = (
    ("test_case", "project_id", "用例"),
    ("run", "project_id", "运行记录"),
    ("credential", "project_id", "凭据"),
    ("issue", "project_id", "缺陷"),
    ("environment", "project_id", "环境"),
    ("project_member", "project_id", "成员"),
    ("notification", "project_id", "通知"),
)

# 用例结果是**间接**挂在项目上的：run_result.run_id 指向 run.id，不是 project_id。
# 一开始我把它塞进上面那张表、也按 project_id 查，结果 `WHERE run_id = 3` 撞上了
# 别的项目的运行记录，于是 7 个垃圾项目被误判成"挂着用户数据"而拒绝删除 ——
# 方向是保守的（绝不会误删），但工具因此失灵，而失灵原因藏在一条看起来无害的
# SQL 里。所以它必须走子查询，不能和直查的表混在一起。
INDIRECT_TABLES = (
    (
        "run_result",
        "SELECT COUNT(*) FROM run_result WHERE run_id IN (SELECT id FROM run WHERE project_id = ?)",
        "用例结果",
    ),
)

# 知识分块单独算：它**不**算"用户数据"。污染源那个测试除了建项目，还顺手
# replace_knowledge() 写了几条分块 —— 实测每个 kb-* 项目挂着 1-2 条，全是测试文本。
# 所以删项目时要把这些分块一并删掉，否则会留下指向已删项目的孤儿行。
CHUNK_TABLE = ("knowledge_chunk", "project_id", "知识分块")


def child_counts(con: sqlite3.Connection, pid: int) -> tuple[dict[str, int], int]:
    """返回 (有用户数据的表 -> 条数, 知识分块条数)。"""
    out: dict[str, int] = {}
    for table, col, label in CHILD_TABLES:
        try:
            n = con.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} = ?", (pid,)).fetchone()[0]
        except sqlite3.Error:
            n = 0  # 表不存在 = 没有这一类数据
        if n:
            out[label] = n
    for _table, sql, label in INDIRECT_TABLES:
        try:
            n = con.execute(sql, (pid,)).fetchone()[0]
        except sqlite3.Error:
            n = 0
        if n:
            out[label] = n
    table, col, _ = CHUNK_TABLE
    try:
        chunks = con.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} = ?", (pid,)).fetchone()[0]
    except sqlite3.Error:
        chunks = 0
    return out, chunks


def survey() -> tuple[list[sqlite3.Row], list[tuple[sqlite3.Row, dict[str, int], int]]]:
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = list(con.execute("SELECT id, name, base_url, created_at FROM project ORDER BY id"))
    junk = []
    for r in rows:
        if r["id"] in PROTECTED_IDS:
            continue
        if not str(r["name"]).startswith(JUNK_PREFIXES):
            continue
        if str(r["base_url"]) not in JUNK_URLS:
            continue
        junk.append((r, *child_counts(con, r["id"])))
    con.close()
    return rows, junk


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="清理测试写进真库的垃圾项目（默认只预览）")
    ap.add_argument("--apply", action="store_true", help="真的删（默认只预览）")
    ap.add_argument("--yes", action="store_true", help="跳过交互确认")
    args = ap.parse_args(argv)

    if not DB.is_file():
        print(f"找不到 {DB}", file=sys.stderr)
        return 2

    rows, junk = survey()

    print("=" * 72)
    print(f"数据库：{DB}")
    print("=" * 72)
    print(f"项目总数：{len(rows)}")
    print("受保护（用户自己的，永远不动）：")
    for r in rows:
        if r["id"] in PROTECTED_IDS:
            print(f"  id={r['id']:<4} {r['name']}")

    empty = [(r, c, k) for r, c, k in junk if not c]
    attached = [(r, c, k) for r, c, k in junk if c]
    total_chunks = sum(k for _r, _c, k in junk)

    print()
    print(f"疑似测试垃圾：{len(junk)} 个（名字以 {JUNK_PREFIXES} 开头 + 占位 URL）")
    print(f"  其中无用户数据（可安全删除）：{len(empty)}")
    print(f"  其中挂着用户数据（**不会删**）：{len(attached)}")
    for r, c, k in attached:
        print(f"    id={r['id']} {r['name']} 挂着 {c}")
    print(f"  随项目一并删除的测试知识分块：{total_chunks} 条（也是那个测试写进去的）")

    if not junk:
        print("\n没有需要清理的项目。")
        return 0

    if not args.apply:
        print("\n这是**预览**，没有改动任何数据。")
        print(f"确认无误后执行：python scripts/{pathlib.Path(__file__).name} --apply")
        return 0

    if not empty:
        print("\n没有可安全删除的项目（挂着用户数据的一律不动）。")
        return 1

    if not args.yes:
        ids = [r["id"] for r, _c, _k in empty]
        answer = input(f"\n即将删除 {len(empty)} 个项目（ID: {ids}）及其 {total_chunks} 条知识分块。输入 yes 继续：")
        if answer.strip().lower() != "yes":
            print("已取消，什么都没删。")
            return 1

    # 先备份 —— 删除生产数据之前必须留一份能还原的东西
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = BACKUP_DIR / f"potato-before-junk-cleanup-{stamp}.db"
    # 用 sqlite 的备份 API 而不是文件复制：库可能正在被服务占用，
    # 直接 copy 一个事务中途的文件会得到损坏的副本。
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(backup)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    print(f"\n已备份：{backup.name}")

    con = sqlite3.connect(DB)
    ids = [r["id"] for r, _c, _k in empty]
    # 先删知识分块再删项目：反过来的话会留下指向已删项目的孤儿行。
    # 这些分块也是那个测试写进去的测试文本，不含任何用户内容。
    chunk_table, chunk_col, _ = CHUNK_TABLE
    removed_chunks = 0
    con.execute("BEGIN")
    for pid in ids:
        removed_chunks += con.execute(
            f"DELETE FROM {chunk_table} WHERE {chunk_col} = ?", (pid,)
        ).rowcount
        con.execute("DELETE FROM project WHERE id = ?", (pid,))
    con.commit()
    left = con.execute("SELECT COUNT(*) FROM project").fetchone()[0]
    con.close()

    print(f"已删除 {len(ids)} 个垃圾项目和 {removed_chunks} 条测试知识分块，现在库里有 {left} 个项目。")
    print(f"要回滚：把 {backup.name} 拷回 potato.db（先关掉正在运行的服务）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
