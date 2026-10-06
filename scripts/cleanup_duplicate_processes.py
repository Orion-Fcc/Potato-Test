"""按命令行特征清理重复的 Potato_Test 进程，保留 pidfile 跟踪的那一组。

为什么不能只信 pidfile：BAT 用 `start` 启动，父 cmd 立即退出 → 进程成孤儿；清理只读
pidfile 里的单个 PID，一旦 pidfile 被覆盖/删除，旧进程就再也没人管（本次就是 190 分钟
前那对成了孤儿）。

安全性：
  - 只匹配 cmdline 含 potato_test 且含 feishu_poll / run_server 的进程
  - 保留 pidfile（.feishu_ws.pid / .potato.pid）指向的实例及其 launcher 父进程
  - 正在服务端口的 run_server 一律不动，除非显式要求
  - 默认 dry-run，加 --apply 才真正杀
"""
import argparse
import os
import sys
import time

import psutil

PROJ = os.path.dirname(os.path.abspath(__file__))
NOW = time.time()


def read_pid(path: str) -> int | None:
    try:
        with open(path) as fh:
            return int(fh.read().split()[0])
    except Exception:
        return None


def collect(target: str) -> list[psutil.Process]:
    """进程 + 它的父进程（venv 的 pythonw.exe 是 launcher，父子成组）。"""
    hits: list[psutil.Process] = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            n = (p.info["name"] or "").lower()
            if "python" not in n and "cmd" not in n:
                continue
            cl = " ".join(p.cmdline() or [])
            low = cl.lower()
            if "potato_test" not in low or target not in low:
                continue
            hits.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return hits


def group_of(p: psutil.Process) -> set[int]:
    """launcher 与真实解释器是一对：自身 PID + 父 PID（父必须是 python*）。"""
    ids = {p.pid}
    try:
        parent = psutil.Process(p.ppid())
        if "python" in (parent.name() or "").lower():
            ids.add(parent.pid)
    except Exception:
        pass
    return ids


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正杀进程；默认只报告")
    ap.add_argument("--also-server", action="store_true", help="连 run_server 的重复实例也清")
    args = ap.parse_args()

    targets = {"feishu_poll": read_pid(os.path.join(PROJ, ".feishu_ws.pid"))}
    if args.also_server:
        targets["run_server"] = read_pid(os.path.join(PROJ, ".potato.pid"))

    kill_ids: set[int] = set()
    report: list[str] = []

    for name, keep_pid in targets.items():
        procs = collect(name)
        if not procs:
            report.append(f"{name}: 没有进程")
            continue

        # 分组，并找出每个组的"存活时间"
        groups: dict[int, dict] = {}
        for p in procs:
            ids = group_of(p)
            gid = min(ids)
            g = groups.setdefault(gid, {"ids": set(), "newest": 0.0, "desc": ""})
            g["ids"] |= ids
            try:
                ct = p.create_time()
            except Exception:
                ct = 0.0
            if ct > g["newest"]:
                g["newest"] = ct
            if g["desc"] == "":
                g["desc"] = " ".join(p.cmdline() or [])[:110]

        # 保留哪一组：优先 pidfile 指向的；没有就保留最新的一组
        keep_gid = None
        for gid, g in groups.items():
            if keep_pid and keep_pid in g["ids"]:
                keep_gid = gid
                break
        if keep_gid is None:
            keep_gid = max(groups, key=lambda k: groups[k]["newest"])

        report.append(f"{name}: 共 {len(groups)} 组（pidfile 指向 {keep_pid}）")
        for gid, g in sorted(groups.items(), key=lambda kv: kv[1]["newest"]):
            keep = gid == keep_gid
            age = (NOW - g["newest"]) / 60
            report.append(
                f"    {'保留' if keep else '★清理'} 组(gid={gid}) pids={sorted(g['ids'])} "
                f"已运行 {age:.0f} 分钟"
            )
            if not keep:
                kill_ids |= g["ids"]

    print("=== 清理计划 ===")
    for line in report:
        print(" ", line)
    print()
    if not kill_ids:
        print("没有需要清理的重复进程 —— 已经是干净的单实例状态。")
        return 0

    print(f"将要结束 {len(kill_ids)} 个孤儿进程：{sorted(kill_ids)}")
    if not args.apply:
        print("\n[dry-run] 未执行。加 --apply 才真正结束。")
        return 0

    killed, failed = 0, []
    for pid in sorted(kill_ids):
        try:
            p = psutil.Process(pid)
            p.kill()
            killed += 1
        except psutil.NoSuchProcess:
            pass
        except Exception as exc:  # noqa: BLE001
            failed.append((pid, str(exc)))
    time.sleep(1.5)

    alive = []
    for pid in sorted(kill_ids):
        if psutil.pid_exists(pid):
            try:
                p = psutil.Process(pid)
                if p.status() != psutil.STATUS_ZOMBIE:
                    alive.append(pid)
            except Exception:
                pass

    print(f"已结束 {killed} 个，仍存活 {alive if alive else '无'}，失败 {failed if failed else '无'}")
    return 0 if not alive else 1


if __name__ == "__main__":
    sys.exit(main())
