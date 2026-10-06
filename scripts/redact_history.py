"""重写整个 git 历史里的真实标识（配合 git filter-branch --index-filter）。

为什么需要单独一个脚本，而不是手敲 filter-branch：
规则来自 `check_secrets.py::REDACTIONS` —— **和检查器共用同一份**。
两份清单各写一遍的话，最坏的结果不是"报错"，而是"检查说干净、实际没抹掉"，
而这种不一致要等到有人真的去翻历史才会发现。

用法（**务必在一次性克隆/镜像上做，不要在日常使用的仓库上做**）：

    # 1) 备份（不可逆操作的前置条件）
    git bundle create ../backup.bundle --all

    # 2) 在镜像副本上重写
    git clone --mirror <本地仓库或远端> rewrite.git
    cd rewrite.git
    python <repo>/scripts/redact_history.py --index-filter

    # 3) 让旧 blob 真的不可达（filter-branch 会把原 ref 留在 refs/original/，
    #    那些 ref 仍然指着旧提交，gc 就永远回收不掉）
    rm -rf .git/refs/original        # 裸仓库里是 ./refs/original
    git reflog expire --expire=now --all
    git gc --prune=now --aggressive

    # 4) 验证 + 强推
    python <repo>/scripts/check_secrets.py --history
    git push --force origin main

    （裸仓库里没有 .git 子目录，refs 在 ./refs/original）

设计要点：
1. **逐 blob、逐编码替换**。历史里的 .bat 是 GBK，源码是 UTF-8；只按一种编码
   替换，中文词会在 .bat 里原样留下。`redact_bytes` 为每个词生成 utf-8/gbk/gb18030
   三种字节形态，谁先命中用谁。
2. **保留文件 mode**。用 `git update-index --cacheinfo <mode>,<sha>,<path>` 写回，
   而不是 `--index-info` 的裸三段 —— `deploy.sh` 的 100755 可执行位在历史里
   也必须保住，否则重写后它又变回 644，`./scripts/deploy.sh` 立刻 Permission denied。
3. **只改内容，不改结构**。提交数、作者、日期、message 全部原样保留：
   filter-branch 只在树变了才重写提交，但作者与时间戳是直接搬过去的。
4. **不推送**。这个脚本只负责改本地仓库；强推是人的决定，不该由脚本顺手做掉。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_secrets as cs  # noqa: E402  （必须先设好 sys.path）

LOG_LIMIT = 40


def _git(*args: str, binary: bool = False, **kwargs):
    """跑 git 并返回 stdout。binary=True 时不 decode（blob 是二进制）。"""
    return subprocess.run(
        ["git", *args], capture_output=True, check=True, **({} if binary else {"text": True}), **kwargs
    ).stdout


def index_entries() -> list[tuple[str, str, str]]:
    """当前索引里的 (mode, sha, path)，path 用 -z 读以免被特殊字符截断。"""
    raw = _git("ls-files", "-s", "-z", binary=True)
    entries = []
    for rec in raw.split(b"\0"):
        if not rec:
            continue
        meta, _, path = rec.partition(b"\t")
        mode, sha, _stage = meta.split()
        entries.append((mode.decode(), sha.decode(), path.decode("utf-8", "replace")))
    return entries


def run_index_filter() -> int:
    """filter-branch 的 index-filter 入口：只改当前 checkout 的这一棵树。"""
    changed = 0
    for mode, sha, path in index_entries():
        blob = _git("cat-file", "blob", sha, binary=True)
        new_blob, applied = cs.redact_bytes(blob)
        if new_blob == blob or not applied:
            continue
        new_sha = _git("hash-object", "-w", "--stdin", binary=True, input=new_blob).decode().strip()
        # mode 原样带回：这一步保住了 deploy.sh 的可执行位
        _git("update-index", "--cacheinfo", f"{mode},{new_sha},{path}")
        changed += 1
        print(f"  [改写] {path}  <- {'; '.join(applied[:3])}", file=sys.stderr)

    if changed:
        # 非零会让 filter-branch 认为这次重写"有东西变了"；这里是成功路径，保持 0，
        # 但把计数打印出来便于人工确认（filter-branch 只看退出码）。
        print(f"  本棵树改写了 {changed} 个文件", file=sys.stderr)
    return 0


def run_msg_filter() -> int:
    """filter-branch 的 msg-filter 入口：commit message 从 stdin 进、stdout 出。

    为什么必须单独做一遍：blob 改了不等于提交信息改了。实测里有 **1 条**早期提交
    的正文里贴了启动日志，而日志里就有真实的网关地址与模型名 —— 只跑 index-filter
    的话，它会作为 commit 对象留在库里（`rev-list --objects` 里提交对象是不带路径
    打印的，很容易被忽略成"空路径的怪东西"）。
    """
    raw = sys.stdin.buffer.read()
    out, applied = cs.redact_bytes(raw)
    if applied and out != raw:
        print(f"  [改写提交信息] {'; '.join(applied[:3])}", file=sys.stderr)
    sys.stdout.buffer.write(out)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="重写历史里的真实标识")
    ap.add_argument(
        "--index-filter",
        action="store_true",
        help="作为 git filter-branch --index-filter 的入口被调用",
    )
    ap.add_argument(
        "--msg-filter",
        action="store_true",
        help="作为 git filter-branch --msg-filter 的入口被调用（改提交信息）",
    )
    ap.add_argument(
        "--print-rules", action="store_true", help="打印将应用的替换规则，然后退出"
    )
    args = ap.parse_args(argv)

    if args.print_rules:
        if not cs.REDACTIONS:
            print(
                f"没有规则可应用：找不到 {cs.LOCAL_RULES_FILE}。\n"
                f"复制 scripts/secrets.local.example.json 成 {cs.LOCAL_RULES_FILE} 并填好。",
                file=sys.stderr,
            )
            return 2
        for old, new, why in cs.REDACTIONS:
            print(f"{old}  ->  {new}    # {why}")
        print(f"共 {len(cs.REDACTIONS)} 条；检查项另有 {len(cs.EXTRA_IDENTIFIERS)} 条（不自动替换）")
        return 0

    # 规则为空时必须拒绝运行，而不是"成功地什么都没做"。
    # 这是最坏的失败形态：filter-branch 会照常重写所有提交、给出漂亮的输出、
    # 推出一个什么都没变的分支，而人以为已经脱敏了。
    if not cs.REDACTIONS:
        print(
            f"[中止] 没有替换规则（找不到 {cs.LOCAL_RULES_FILE}）。"
            "空跑一遍重写会得到'看起来成功、实际什么都没改'的结果。",
            file=sys.stderr,
        )
        return 2

    if args.index_filter:
        return run_index_filter()
    if args.msg_filter:
        return run_msg_filter()

    ap.error(
        "请通过 git filter-branch --index-filter / --msg-filter 调用，"
        "或用 --print-rules 查看规则"
    )


if __name__ == "__main__":
    raise SystemExit(main())
