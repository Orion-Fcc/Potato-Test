"""常驻监控：把"新出现的进程"记到文件里，用来抓"终端窗口一闪而过"的元凶。

用法（在 PowerShell 或 cmd 里跑，自己按住不放，闪的时候看输出）：
    cd <PROJECT_DIR>
    .venv\\Scripts\\python.exe scripts\\watch_new_processes.py

它会持续打印新出现的进程名 + 父进程名 + 命令行前 120 字。
令 `timeout.exe` / `findstr.exe` / `cmd.exe` 这类反复出现，就是那个在循环的脚本；
若看到 `powershell.exe` 反复出现，那是某个 BAT 在反复调它。

按 Ctrl+C 停止。加 `--seconds 0` 可以一直跑到手动停。
"""
from __future__ import annotations

import argparse
import collections
import time

import psutil

WATCH = ("cmd", "conhost", "wscript", "cscript", "powershell", "timeout", "findstr", "mshta", "node")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=60.0, help="跑多少秒，0 = 一直跑")
    ap.add_argument("--all", action="store_true", help="不限终端类，记录所有新进程")
    args = ap.parse_args()

    baseline = {p.info["pid"] for p in psutil.process_iter(["pid"])}
    counts: collections.Counter = collections.Counter()

    print("开始监控。看到窗口闪的时候留意这里的输出（Ctrl+C 停止）…\n")
    deadline = None if args.seconds <= 0 else time.time() + args.seconds
    try:
        while deadline is None or time.time() < deadline:
            for p in psutil.process_iter(["pid", "name", "ppid", "cmdline"]):
                try:
                    pid = p.info["pid"]
                    if pid in baseline:
                        continue
                    baseline.add(pid)
                    name = (p.info["name"] or "?").lower()
                    if not args.all and not any(k in name for k in WATCH):
                        continue
                    try:
                        parent = psutil.Process(p.info["ppid"]).name()
                    except Exception:
                        parent = "?"
                    cl = " ".join(p.info["cmdline"] or [])[:120]
                    counts[name] += 1
                    stamp = time.strftime("%H:%M:%S")
                    print(f"{stamp}  {name:22} 父={parent:20} {cl}")
                except Exception:
                    pass
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass

    print("\n=== 汇总（谁出现得最多，就是它在循环）===")
    if not counts:
        print("  没抓到任何新进程。若这期间确实看到窗口闪过，说明它短到 0.1s 的采样也漏掉了，")
        print("  试试把脚本时间拉长，或者用 --all 看全部进程。")
    for name, cnt in counts.most_common(15):
        print(f"  {name:24} x{cnt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
