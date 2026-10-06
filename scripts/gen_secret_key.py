"""把 .env 里的 POTATO_SECRET_KEY 填成一个新生成的 Fernet 密钥。

为什么单独一个脚本，而不是在 .bat / .sh 里塞一行 `python -c`：

1. 批处理的引号转义极容易出错，而这段逻辑是"找到那一行改掉，没有就追加" ——
   写在命令行里没法验证，出错的方式又很安静（比如密钥没写进去，直到第一次
   保存测试凭据时才报错）。本项目已经因为一次 `> 2>&1` 手误留下过垃圾文件，
   所以这类逻辑一律放进可测的 .py。
2. 一键部署脚本（scripts/deploy.sh / deploy.bat）两边都要做同一件事，
   共用一份实现就不会出现"Windows 上装了、Linux 上没装"的差异。

行为：
  * .env 不存在 → 从 .env.example 复制一份再改（调用方通常已经复制过了）。
  * 已有非空的 POTATO_SECRET_KEY → **不动它**（改掉会让库里已有的凭据全部解不开）。
  * 其余情况 → 写入新生成的密钥。

用法：
    python scripts/gen_secret_key.py            # 就地修改 .env
    python scripts/gen_secret_key.py --print    # 只打印，不写文件
"""

from __future__ import annotations

import argparse
import pathlib
import sys

KEY = "POTATO_SECRET_KEY"


def generate() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def has_key(text: str) -> bool:
    """.env 里是否已有一个**非空**的密钥值。"""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{KEY}=") and stripped[len(KEY) + 1 :].strip():
            return True
    return False


def write_key(text: str, key: str) -> str:
    """返回替换后的全文。保持行序，只在原位置改值，不重排用户的文件。"""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{KEY}="):
            lines[i] = f"{KEY}={key}"
            break
    else:
        lines.append(f"{KEY}={key}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成并写入 POTATO_SECRET_KEY")
    ap.add_argument("--print", dest="print_only", action="store_true", help="只打印，不写文件")
    ap.add_argument("--env", default=".env", help=".env 路径（默认 ./.env）")
    args = ap.parse_args(argv)

    key = generate()
    if args.print_only:
        print(key)
        return 0

    path = pathlib.Path(args.env)
    if not path.is_file():
        example = path.with_name(".env.example")
        if not example.is_file():
            print(f"找不到 {path}，也找不到 {example}", file=sys.stderr)
            return 2
        path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")

    text = path.read_text(encoding="utf-8")
    if has_key(text):
        # 静默跳过：密钥已经存在时"重新生成"会把已存的测试凭据全部变成解不开的密文，
        # 而调用它的是一键部署脚本，用户看不见这条提示也无所谓 —— 行为正确更重要。
        return 0

    path.write_text(write_key(text, key), encoding="utf-8")
    print(f"已写入 {KEY} 到 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
