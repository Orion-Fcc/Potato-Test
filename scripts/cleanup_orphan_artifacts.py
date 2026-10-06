"""清理 artifacts/runs 下没有任何引用的运行批次 + 过时的数据库备份。

判据（第一版漏了一处，差点删掉 178 条结果的录像）
----------------------------------------------------
最初我只查了 `run` 表 —— 它只有 9 行，于是"看起来无人引用"的有 228 个批次。
但 `run_result.video_url` 里存着 `/artifacts/runs/177/…` 这类路径，而 177/178/
179/184/185/186 **根本不在 run 表里**（那批 run 的记录早被清掉了，行没了，
录像路径还留在结果行里）。只查 run 表会把 6 个仍被引用的批次一起删掉，
界面上的录像回放就变成一片空白。

所以判据是**两处引用的并集**：
  1. `run.id`
  2. `run_result.video_url` 里解析出的 `/artifacts/runs/(\\d+)/`
少查一处，就会把"看起来没人要"当成"没人要"。

用法
----
    python scripts/cleanup_orphan_artifacts.py            # 预览（默认）
    python scripts/cleanup_orphan_artifacts.py --apply    # 真删
    python scripts/cleanup_orphan_artifacts.py --apply --db-backups 3   # 顺带只留 3 个最新库备份
"""

from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import sqlite3
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
DB = ROOT / "potato.db"
RUNS = ROOT / "artifacts" / "runs"
BACKUP_DIR = ROOT / ".backups"

_VIDEO_RE = re.compile(r"/artifacts/runs/(\d+)/")


def referenced_run_ids() -> tuple[set[int], set[int], set[int]]:
    """返回 (run 表的 id, 录像路径里的 id, 两者并集)。"""
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    try:
        from_runs = {r[0] for r in con.execute("SELECT id FROM run")}
        from_videos: set[int] = set()
        for (url,) in con.execute(
            "SELECT video_url FROM run_result WHERE video_url IS NOT NULL AND video_url != ''"
        ):
            m = _VIDEO_RE.search(url or "")
            if m:
                from_videos.add(int(m.group(1)))
    finally:
        con.close()
    return from_runs, from_videos, from_runs | from_videos


def dir_size_mb(paths: list[pathlib.Path]) -> float:
    total = 0
    for p in paths:
        for f in p.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    return total / 1048576


def db_backups() -> list[tuple[pathlib.Path, float, str]]:
    out = []
    for p in sorted(ROOT.glob("potato.db.bak-*")):
        if p.is_file():
            out.append((p, p.stat().st_size / 1048576, time.strftime("%m-%d %H:%M", time.localtime(p.stat().st_mtime))))
    out.sort(key=lambda t: t[0].stat().st_mtime, reverse=True)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="清理无人引用的运行产物（默认只预览）")
    ap.add_argument("--apply", action="store_true", help="真删（默认只预览）")
    ap.add_argument("--db-backups", type=int, default=0, metavar="N",
                    help="顺带只保留最新的 N 个 potato.db.bak-*（其余进回收站），0 = 不动")
    args = ap.parse_args(argv)

    if not DB.is_file():
        print(f"找不到 {DB}", file=sys.stderr)
        return 2

    from_runs, from_videos, keep_ids = referenced_run_ids()

    print("=" * 72)
    print("artifacts/runs")
    print("=" * 72)
    if not RUNS.is_dir():
        print(f"  没有 {RUNS}，跳过")
        keep_dirs: list[pathlib.Path] = []
        drop_dirs: list[pathlib.Path] = []
    else:
        dirs = [p for p in RUNS.iterdir() if p.is_dir()]
        keep_dirs = [d for d in dirs if d.name.isdigit() and int(d.name) in keep_ids]
        drop_dirs = [d for d in dirs if d not in keep_dirs]
        print(f"  run 表引用的 id        : {sorted(from_runs)}")
        print(f"  录像路径引用的 id      : {sorted(from_videos)}")
        print(f"  目录总数              : {len(dirs)}")
        print(f"  必须保留（两处并集）  : {len(keep_dirs)} 个，{dir_size_mb(keep_dirs):.1f} MB")
        print(f"  无人引用              : {len(drop_dirs)} 个，{dir_size_mb(drop_dirs):.1f} MB "
              f"({dir_size_mb(drop_dirs)/1024:.2f} GB)")

    backups = db_backups()
    drop_backups: list[pathlib.Path] = []
    if args.db_backups and backups:
        drop_backups = [p for p, _s, _t in backups[args.db_backups:]]
        print()
        print("=" * 72)
        print("过时的数据库备份")
        print("=" * 72)
        for p, size, when in backups:
            mark = "保留" if p not in drop_backups else "清除"
            print(f"  [{mark}] {p.name:<52} {size:5.1f} MB  {when}")
        freed_mb = sum(p.stat().st_size for p in drop_backups) / 1048576
        print(f"  合计清除 {len(drop_backups)} 个，{freed_mb:.0f} MB")

    if not args.apply:
        print()
        print("这是**预览**，没有改动任何文件。")
        print("确认后执行：python scripts/cleanup_orphan_artifacts.py --apply --db-backups 2")
        return 0

    removed = 0
    freed = 0.0
    failed: list[tuple[str, str]] = []
    for d in drop_dirs:
        size = dir_size_mb([d])
        # 刻意**不**用 ignore_errors=True：那会把"文件被占用导致删不掉"伪装成成功，
        # 而调用者只会看到"已删除 N 个" —— 于是磁盘上留着残骸，而报告说已经清干净。
        try:
            shutil.rmtree(d)
        except OSError as exc:
            failed.append((d.name, str(exc)))
            continue
        removed += 1
        freed += size
    print(f"\n已删除 {removed} 个无人引用的运行批次，释放约 {freed:.0f} MB")
    print("  不可恢复：这些 run 的数据库记录早已被清掉，产物是仅存痕迹。")
    if failed:
        print(f"  ⚠️ 有 {len(failed)} 个批次**没能删掉**（通常是文件被进程占用）：")
        for name, why in failed[:10]:
            print(f"     {name}: {why}")
        print("     关掉正在运行的服务再跑一次即可。")

    if drop_backups:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        # 库备份很小而且是"后悔药"，走回收站而不是直接删
        moved = 0
        for p in drop_backups:
            dest = BACKUP_DIR / f"{p.name}.{stamp}"
            try:
                shutil.move(str(p), str(dest))
                moved += 1
            except OSError as exc:
                print(f"  移动失败 {p.name}: {exc}")
        print(f"已把 {moved} 个旧库备份移到 .backups/（不是删除，可随时挪回来）")

    print("\n★ 提醒：清空回收站后空间才真正释放。现在这些还在回收站里。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
