"""应用版本号 —— **唯一来源**（2026-10-17）。

为什么单独一个文件：原来 `pyproject.toml` 写 `0.1.0`、安装包脚本写 `1.0.0`，
而"检查更新"要拿当前版本跟远端比 —— 两个源必然会判断错（明明有新版本却说已是最新，
或者反过来一直提示更新）。

约定：
  * 这里是唯一手写的地方；
  * `pyproject.toml` 的 `version` 必须与之一致（`tests/test_updater.py` 会查）；
  * `installer/potato.iss` 的 `#define AppVer` 也必须一致（同上，会查）。

改版本号的正确姿势：`python scripts/release.py --bump 1.0.1`（会同时改这三处 + 打标签）。
"""

APP_VERSION = "1.0.0"

# 更新源：本项目的 GitHub 仓库。改仓库地址要同时改 `app/updater.py` 里的下载白名单，
# 否则下载会被拒绝（那是故意的 —— 见 updater 的注释）。
GITHUB_REPO = "Orion-Fcc/Potato-Test"
