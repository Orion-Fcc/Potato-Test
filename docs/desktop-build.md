# 桌面版：打包 / 安装 / 发布 / 更新

> 这是**唯一的分发形态**。本产品不提供服务端部署 —— 网页版已删除（浏览器打开本地端口会被拦）。
>
> 为什么"服务"还在：桌面窗口是用 WebView2 加载本地服务上的界面，那个服务是**渲染管道**，
> 不是给人访问的网站。`app/desktop_gate.py` 用一次性令牌把这一点做实了。

---

## 1. 打包（生成程序目录）

先装打包工具链（`pywebview` / `pyinstaller`，只有 Windows 需要），版本同样由锁文件固定：

```bat
.venv\Scripts\pip install -c constraints.txt -e ".[desktop]"
.venv\Scripts\python scripts\build_desktop.py
```

`build_desktop.py` 会先自动执行 `cd web && npm run build`，再跑 PyInstaller；调试时可加 `--skip-web` 复用现有 `web/dist`。

产出 `dist_desktop\PotatoTest\`（约 **481 MB**，含 Playwright 驱动 + OCR 模型），
双击 `PotatoTest.exe` 即可运行。

几个关键参数（脚本里有注释）：

| 参数 | 为什么 |
| --- | --- |
| `--windowed` | 不弹黑框；日志写 `logs\desktop.log` |
| `--add-data web/dist` | 前端必须塞进包内，装到别人机器上才不依赖仓库目录 |
| `--collect-all playwright` | 它带 node driver，静态分析看不见 |
| `--collect-all rapidocr / onnxruntime / pymupdf` | OCR 模型是**数据文件**，不 collect 会"能跑但找不到模型" |
| onedir（默认） | onefile 每次启动解压上百 MB，首启慢 3~8 秒，还容易被杀软拦 |

## 2. 编译安装包（WiX MSI）

安装包用 **WiX v5 的 per-user MSI**，不再用 NSIS。原因：MSI 由 Windows Installer
托管，不经过 NSIS exehead 的 `%TEMP%` 探测，用户临时目录异常也不影响安装；同时
安装、升级、卸载都走系统标准流程。

### 2.1 一次性工具链

工具装在项目内 `.tools\`，不需要管理员权限，也不碰全局 `~/.dotnet`：

```bat
:: .NET 8 SDK（只需一次）
powershell -ExecutionPolicy Bypass -File .tools\dotnet-install.ps1 -Version 8.0.426 -InstallDir .tools\dotnet

set DOTNET_ROOT=%CD%\.tools\dotnet
set PATH=%DOTNET_ROOT%;%PATH%

:: WiX v5（只需一次）
dotnet tool install wix --tool-path .tools\wix --version 5.0.2
```

`scripts\build_msi.py` 会自己设置 `DOTNET_ROOT`，日常打包不用手动设环境变量。

### 2.2 打包

确认 `dist_desktop\PotatoTest\` 已由第 1 步生成，然后：

```bat
.venv\Scripts\python scripts\build_msi.py
```

脚本依次做四件事：

1. 从 `app/version.py` 读版本号；
2. 跑 `installer/harvest.py` 扫描 `dist_desktop\PotatoTest`，生成 `installer\Components.wxs`；
3. 调 `.tools\wix\wix.exe build` 编译 `installer\PotatoTest.wxs` + `Components.wxs`；
4. 生成同名 `.sha256`（`sha256sum` 格式）。

只改了 `.wxs`、不想重新 harvest 时加 `--skip-harvest`。

产出 `dist_desktop\installer\PotatoTest-Setup-<版本>.msi` + `.sha256`。

**不要手改 `Components.wxs`** —— 每次打包会被覆盖。文件清单、目录层级、
Component GUID 都由 `harvest.py` 从实际产物生成；GUID 用 `uuid5(路径)` 固定，
同一路径跨版本不变，升级才能正确覆盖。

harvest 排除：`logs` / `artifacts` / `profiles` / `__pycache__` / `.pytest_cache`、
`potato.db*`、`.env`、`*.pyc`、`uninstall.exe`。安装包**不含用户数据**，
首次启动自动建空库。

编译时可能出现 `WIX5437`（标准目录不必重复声明）和 per-user 的 ICE38/ICE64
警告；WiX v5 不支持 v3 的 `-sice:` 参数，这些警告不影响产物，可忽略。

### 2.3 安装包行为

- **per-user、免 UAC**：装到 `%LOCALAPPDATA%\Programs\PotatoTest`；
- **不依赖 TEMP**：没有自定义动作，cab 由 Windows Installer 内嵌处理；
- **快捷方式**：开始菜单 + 桌面都会创建；
- **升级**：`UpgradeCode` 固定为 `{5B0E2F5A-3C8D-4E71-9A62-7D4C1B8E0A93}`，
  `MajorUpgrade AllowSameVersionUpgrades="yes"`，直接装新 MSI 即可覆盖；
- **清理旧安装**：安装时删除旧 NSIS / Inno 的卸载注册项和残留 `uninstall.exe`，
  避免控制面板出现重复条目；
- **卸载只删程序文件**：`potato.db` / `artifacts` / `profiles` / `logs` / `.env`
  **全部保留**。

> **1.0.6 → 1.0.7 必须手动装一次**：1.0.6 的更新器只认 `.exe` 资产，看不到
> `.msi`。从 1.0.7 起应用内更新可以正常下载并静默安装 MSI。

NSIS 脚本 `installer\potato.nsi` 仅作回退保留，正常发布不再编译它。

## 3. 发布（让"检查更新"有东西可查）

```bat
python scripts\release.py --bump 1.0.7       &:: 四处版本号一起改
:: 重新打包 + 编译 MSI（上面 1、2 两步）
set GITHUB_TOKEN=ghp_xxx                     &:: 需要 repo 权限的 token
python scripts\release.py --publish          &:: 建 Release + 上传 MSI
```

版本号有**四个地方**必须一致（`app/version.py` / `pyproject.toml` /
`installer/potato.nsi` / `web/package.json`）—— `tests/test_updater.py` 会检查，
`release.py` 也会在发布前再校验一次。

`release.py` 上传 `PotatoTest-Setup-<版本>.msi` 和同名 `.sha256`，顺序是先校验文件、
后安装包。

## 4. 更新（应用内）

设置页底部「版本」卡：

1. 点**检查更新** → 读本仓库的 `releases/latest`；
2. 有新版本 → **下载并安装** → 下载到临时目录并校验 SHA-256 → 延迟 6 秒启动
   `msiexec /i "<路径>" /qn /norestart` → 应用退出 → 静默覆盖安装；
3. **数据不动**（MSI 只覆盖自己安装的程序文件）。

安全边界（`app/updater.py` 文件头有完整说明）：

- **只有桌面版能装**（`sys.frozen`）；网页/源码模式一律 400；
- **下载地址白名单**：必须 `https://github.com/<本仓库>/releases/download/` 且文件名匹配
  `PotatoTest-Setup-*.msi` —— 这个接口会执行下载下来的安装包，不能放开；
- **SHA-256 校验**：同一 Release 里必须有 `<安装包名>.sha256`（`release.py` 会生成并
  **先上传校验文件、再传 MSI**）。哈希不一致、校验文件缺失或格式不对 —— 一律删掉下载物
  并拒绝安装，提示用户到 Release 页面手动下载。

> 安装包没有代码签名：用户**首次手动双击**时 Windows 可能提示"未知发布者"。
> 但应用内更新下载的文件不带网络来源标记，一般不会触发这个提示。
