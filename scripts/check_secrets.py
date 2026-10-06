"""公开仓库的提交前泄漏检查。

为什么需要它：这个仓库是**公开**的，而"哪些东西不该进去"靠人记是靠不住的 ——
`.env`、`potato.db`（里面是加密的测试凭据和被测系统地址）、`.potato-secret.key`
（能解开那些凭据）、`profiles/`（浏览器登录态）、`artifacts/`（录屏与截图）里
每一样都是能用的东西。一次 `git add -A` 就够了。

这个脚本做四件事：

1. **路径**：索引里有没有出现上述高危路径（`.env.example` 属于例外）。
2. **值**：`.env` 与数据库里**真正配置着的**密钥值，是否出现在任何被跟踪的文件里。
   低熵值（`potato`、`changeme` 这种）会被跳过并提示 —— 拿 `potato` 去搜，
   会命中每一个含项目名的文件，那种报告只会让人学会忽略警告。
3. **形状**：不依赖"我手上有没有那个值"，按模式找长得像密钥的串
   （`cli_…` 飞书 App ID、`sk-…` API key、`oc_/ou_…` 飞书 ID、私钥块）。
   这样即使值来自我读不到的地方（别人的配置、以后新加的集成）也能拦住。
4. **图片**：跟踪了哪些 png/jpg —— 截图里印着的地址和界面，机器看不见，
   只能靠人过一眼，所以至少把它们列出来。

它还会查**本项目的真实标识串**（LLM 网关地址、模型名、客户名、内网地址、账号前缀、
旧的本地目录名）。这一类不长得像密钥，但会暴露"谁在用哪家网关、给谁做的项目"，
而且比密钥更难轮换 —— 第一版审计就是只找了密钥形状，把网关地址漏过去了。

用法：
    python scripts/check_secrets.py            # 检查工作区（每次提交前跑）
    python scripts/check_secrets.py --staged   # 只检查已暂存的文件（更快）
    python scripts/check_secrets.py --head     # 检查已提交版本 = 远端能看到的内容 ★
    python scripts/check_secrets.py --history  # 连整个历史一起查（慢，发布前跑一次）

★ `--head` 和 `--history` 才是"有没有泄漏"的正确答案：仓库是公开的，
  只要某个字符串进过一次提交，把工作区改干净并不能把它收回来。

退出码 0 = 干净，1 = 发现问题。发现的问题会打印文件名与命中类型，**不打印密钥值**。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable

# ---------------------------------------------------------------- 规则

# 这些路径一旦进了索引，就等于把可用的凭据/数据发出去了。
DANGEROUS_PATHS = re.compile(
    r"(^|/)(\.env|\.potato-secret\.key|auth\.json|project_settings_backup\.json)$|"
    r"\.(db|db-journal|sqlite|sqlite3)$|"
    r"\.db\.bak|\.env\.bak|"
    r"\.(pid)$|"
    r"(^|/)(profiles|artifacts|logs|\.workbuddy|node_modules)(/|$)",
    re.IGNORECASE,
)

# 例外：这些是**故意**入库的模板/占位，不是真实配置。
ALLOWED_PATHS = re.compile(r"(^|/)\.env\.example$", re.IGNORECASE)

# 长得像密钥的东西。不依赖手上有没有那个值。
SHAPE_PATTERNS = {
    "飞书 App ID": re.compile(r"\bcli_[A-Za-z0-9]{10,}"),
    "飞书 chat_id": re.compile(r"\boc_[0-9a-f]{16,}"),
    "飞书 open_id": re.compile(r"\bou_[0-9a-f]{16,}"),
    "API key（sk- 前缀）": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    "私钥文件内容": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}

# 文档/示例里允许出现的占位写法。
SHAPE_ALLOW = (
    re.compile(r"cli_xxx"),
    re.compile(r"oc_xxx"),
    re.compile(r"ou_xxx"),
    re.compile(r"sk-xxx"),
)

# 本项目**真实**的标识串：不是凭据，但同样不该出现在公开仓库里。
#
# 为什么单列一类：第一版审计只找"长得像密钥的东西"和凭据值，结论是"干净"，
# 却漏掉了 LLM 网关地址与模型名 —— 它们不长得像密钥，但会暴露"谁在用哪家网关"，
# 而且比密钥更难轮换（换网关要走流程）。这类东西只能靠一张显式清单来守。
#
# ★ 这份清单是**单一事实来源**：改这里就要同步 redact 脚本的替换规则，反之亦然。
#   两份不一致的后果是"检查说干净、实际没抹掉"。
KNOWN_IDENTIFIERS: list[tuple[str, str]] = [
    ("api.example.com", "用户的 LLM 网关主机名"),
    ("example-model", "实际使用的模型名"),
    ("example-model", "实际使用的模型名"),
    ("192.0.2.10", "被测系统内网地址（RFC1918，但能反推客户）"),
    ("192.168.1.", "常见内网段（示例里见到也要确认是不是真地址）"),
    ("示例", "客户名称"),
    ("培训", "客户租户名"),
    ("系统管理员", "客户环境里的管理员显示名"),
    ("role", "客户环境里的角色账号前缀"),
    ("<WORK_DIR>", "旧的本地工作目录名（会暴露目录命名习惯）"),
]

# 上面这些串出现在**示例/文档**里时是否算通过。
# 目前没有例外：连 README 的截图里也不该出现网关地址。
IDENTIFIER_ALLOW: tuple[re.Pattern[str], ...] = (
    re.compile(r"api\.example\.com"),   # 脱敏后的占位写法
    re.compile(r"example-model"),
    re.compile(r"192\.0\.2\.10"),       # RFC 5737 文档专用地址段，一眼可辨
)

# 检查器豁免清单：**只有这两个文件**，而且只豁免"形状/标识"两类规则。
#
# 为什么必须豁免：规则清单里按定义就写着那些串（不然它没法识别它们），
# 单元测试里也必须有样本（不然测的就是"检查器什么都不报"）。不豁免的话
# 这个护栏永远无法提交 —— 一个提交不了的护栏等于没有。
#
# 代价要说清楚：这两个文件里**真的**混进一个凭据，检查器不会报。
# 所以规则保持尽量短，且不要往这两个文件里粘贴任何真实值；
# 需要样本时用 `cli_xxx` / `sk-xxx` 这类占位（它们本来就在豁免写法里）。
SELF_EXEMPT = ("scripts/check_secrets.py", "tests/test_no_secrets_committed.py")


# 低熵值：拿它们做"值匹配"只会产生噪音。判据是"字符种类少"或"是常见占位词"。
PLACEHOLDERS = {
    "potato", "postgres", "password", "changeme", "admin", "secret", "test",
    "example", "localhost", "true", "false", "none", "null", "demo", "todo",
}

# 只看这些名字的配置项：它们是凭据，其它（模型名、端口）不算。
SECRET_NAME_RE = re.compile(
    r"(SECRET|_KEY|KEY_|TOKEN|PASSWORD|PASSWD|_PWD|APP_ID|APP_SECRET)", re.IGNORECASE
)

MIN_VALUE_LEN = 8


def is_low_entropy(value: str) -> bool:
    """这个值值不值得拿去做"是否出现在文件里"的匹配。

    判据刻意保守：只排除明显是占位/极低熵的，宁可多报也不漏报。
    （本项目的 `POSTGRES_PASSWORD=potato` 就是典型 —— 它和项目同名，
    拿去搜会命中几十个文件，把真正的信号淹没。）
    """
    v = value.strip()
    if len(v) < MIN_VALUE_LEN:
        return True
    if v.lower() in PLACEHOLDERS:
        return True
    if len(set(v)) <= 3:  # "xxxxxxxx"、"00000000" 这类
        return True
    return False


def dangerous_tracked_paths(paths: Iterable[str]) -> list[str]:
    """索引里的高危路径（已排除 .env.example 这类白名单）。"""
    out = []
    for p in paths:
        if ALLOWED_PATHS.search(p):
            continue
        if DANGEROUS_PATHS.search(p):
            out.append(p)
    return out


def shape_hits(text: str) -> list[str]:
    """文本里长得像密钥的片段（只回报类型，不回报原文）。"""
    found = []
    for name, pat in SHAPE_PATTERNS.items():
        for m in pat.finditer(text):
            if any(a.search(m.group(0)) for a in SHAPE_ALLOW):
                continue
            found.append(name)
    return sorted(set(found))


def value_hits(text: str, values: dict[str, str]) -> list[str]:
    """哪些敏感配置项的值出现在这段文本里。"""
    return sorted(k for k, v in values.items() if v in text)


def identifier_hits(text: str) -> list[str]:
    """文本里出现了哪些**本项目的真实标识串**（网关、客户名、内网地址……）。

    返回的是给人和报告看的说明，不是命中的原文 —— 报告里回显一遍
    等于又把它们写进了日志和终端历史。
    """
    found = []
    for needle, why in KNOWN_IDENTIFIERS:
        if needle in text and not any(a.search(needle) for a in IDENTIFIER_ALLOW):
            found.append(f"{why}")
    return sorted(set(found))


def collect_env_values(env_text: str) -> dict[str, str]:
    """从 .env 内容里挑出"看起来是凭据"的项。"""
    out: dict[str, str] = {}
    for line in env_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, val = line.partition("=")
        name = name.strip()
        val = val.strip().strip('"').strip("'")
        if not val or not SECRET_NAME_RE.search(name):
            continue
        if is_low_entropy(val):
            continue
        out[name] = val
    return out


# ---------------------------------------------------------------- git 接口


def _run(*args: str) -> str:
    return subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", errors="replace"
    ).stdout


def tracked_files(staged_only: bool = False) -> list[str]:
    if staged_only:
        out = _run("git", "diff", "--cached", "--name-only", "--diff-filter=ACM")
    else:
        out = _run("git", "ls-files")
    return [p for p in out.splitlines() if p.strip()]


def read_text(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def read_text_at_head(path: str) -> str:
    """HEAD 里这个文件的内容 —— 也就是**远端此刻能看到的东西**。

    为什么需要单独一条路径：工作区是可以先改干净的，而远端还留着旧内容。
    "我本地已经删掉了"和"公开仓库里没有了"是两件事，这个模式专门验后者。
    """
    return _run("git", "show", f"HEAD:{path}")


def files_at_head() -> list[str]:
    out = _run("git", "ls-tree", "-r", "--name-only", "HEAD")
    return [p for p in out.splitlines() if p.strip()]


def collect_db_values() -> dict[str, str]:
    """数据库里真正配置着的凭据（System settings）。读不到就返回空。

    只取名字像凭据的项；值本身从不打印。
    """
    import asyncio

    async def go() -> dict[str, str]:
        from sqlalchemy import select

        from app.db import db_session
        from app.models import AppSetting

        out: dict[str, str] = {}
        async with db_session() as s:
            for row in (await s.execute(select(AppSetting))).scalars().all():
                key = (getattr(row, "key", "") or "").strip()
                val = (getattr(row, "value", "") or "").strip()
                if not key or not val or not SECRET_NAME_RE.search(key):
                    continue
                if is_low_entropy(val):
                    continue
                out[key] = val
        return out

    try:
        return asyncio.run(go())
    except Exception:  # noqa: BLE001 — 读不到就当没有，检查其余部分仍然有效
        return {}


# ---------------------------------------------------------------- 主流程


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="提交前泄漏检查")
    ap.add_argument("--staged", action="store_true", help="只检查已暂存的文件")
    ap.add_argument(
        "--head",
        action="store_true",
        help="检查 HEAD —— 也就是**远端此刻能看到的内容**（发布前必跑）",
    )
    ap.add_argument("--history", action="store_true", help="连整个 git 历史一起检查（慢）")
    args = ap.parse_args(argv)

    problems: list[str] = []

    # 1) 路径
    files = files_at_head() if args.head else tracked_files(args.staged)
    if args.head:
        print("（--head 模式：检查的是已提交版本，不是工作区）")
    bad_paths = dangerous_tracked_paths(files)
    if bad_paths:
        problems.append("高危路径进了索引：\n    " + "\n    ".join(bad_paths))

    # 2) 值
    values = collect_env_values(read_text(".env"))
    values.update(collect_db_values())
    if values:
        print(f"（对照 {len(values)} 个已配置的敏感项：{', '.join(sorted(values))}）")
    else:
        print("（.env 里没有可用的敏感值，数据库也没读到 —— 跳过值匹配）")

    # 3) + 4) 形状与图片
    images: list[str] = []
    for path in files:
        if args.staged and not path:
            continue
        if path.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            images.append(path)
            continue
        text = read_text_at_head(path) if args.head else read_text(path)
        if not text:
            continue
        # 高危路径与"已配置的值"对谁都一样成立，不豁免；
        # 只豁免形状/标识两类 —— 见 SELF_EXEMPT 的说明。
        if path not in SELF_EXEMPT:
            hits = shape_hits(text)
            if hits:
                problems.append(f"{path} 里有像密钥的内容：{', '.join(hits)}")
            ident = identifier_hits(text)
            if ident:
                problems.append(f"{path} 含本项目的真实标识：{', '.join(ident)}")
        if values:
            vh = value_hits(text, values)
            if vh:
                problems.append(f"{path} 含已配置的敏感项：{', '.join(vh)}")

    if args.history:
        print("（--history：正在遍历整个历史，慢）")
        listing = _run("git", "rev-list", "--all", "--objects").splitlines()
        shas: dict[str, str] = {}
        for line in listing:
            sha, _, p = line.partition(" ")
            shas.setdefault(sha, p)
        proc = subprocess.run(
            ["git", "cat-file", "--batch"],
            input="\n".join(shas).encode(),
            capture_output=True,
        )
        buf, pos = proc.stdout, 0
        for sha, path in shas.items():
            nl = buf.find(b"\n", pos)
            if nl < 0:
                break
            header = buf[pos:nl].split()
            pos = nl + 1
            if len(header) < 3:
                continue
            size = int(header[2])
            blob = buf[pos : pos + size].decode("utf-8", errors="replace")
            pos += size + 1
            hits = [] if path in SELF_EXEMPT else shape_hits(blob) + identifier_hits(blob)
            if hits:
                problems.append(f"[历史 {sha[:8]}] {path} 命中：{', '.join(sorted(set(hits)))}")
            if values:
                vh = value_hits(blob, values)
                if vh:
                    problems.append(f"[历史 {sha[:8]}] {path} 含已配置的敏感项：{', '.join(vh)}")

    if images:
        print(
            "\n提醒：索引里有 %d 张图片，机器读不出里面的内容 —— "
            "截图/录屏会印着地址和界面，发公开仓库前请自己过一眼：" % len(images)
        )
        for p in images:
            print(f"    {p}")

    print()
    if problems:
        print("=" * 68)
        print("发现 %d 处问题（**先别推**）：" % len(problems))
        print("=" * 68)
        for p in problems:
            print("  !! " + p)
        print(
            "\n处理办法：把该文件加进 .gitignore 并git rm --cached 掉；"
            "若已经推送过，凭据必须**立刻轮换** —— 公开仓库的历史是删不掉的。"
        )
        return 1

    print("OK —— 没有发现会泄漏配置或凭据的内容")
    return 0


if __name__ == "__main__":
    sys.exit(main())
