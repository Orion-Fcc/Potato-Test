# 桌面版：打包 / 安装 / 发布 / 更新

> 这是**唯一的分发形态**。本产品不提供服务端部署 —— 网页版已删除（浏览器打开本地端口会被拦）。
>
> 为什么"服务"还在：桌面窗口是用 WebView2 加载本地服务上的界面，那个服务是**渲染管道**，
> 不是给人访问的网站。`app/desktop_gate.py` 用一次性令牌把这一点做实了。

---

## 1. 打包（生成程序目录）

```bat
.venv\Scripts\python scripts\build_desktop.py
```

产出 `dist_desktop\PotatoTest\`（约 **565 MB**，含 Playwright 驱动 + OCR 模型），
双击 `PotatoTest.exe` 即可运行。

几个关键参数（脚本里有注释）：

| 参数 | 为什么 |
| --- | --- |
| `--windowed` | 不弹黑框；日志写 `logs\desktop.log` |
| `--add-data web/dist` | 前端必须塞进包内，装到别人机器上才不依赖仓库目录 |
| `--collect-all playwright` | 它带 node driver，静态分析看不见 |
| `--collect-all rapidocr / onnxruntime / pymupdf` | OCR 模型是**数据文件**，不 collect 会"能跑但找不到模型" |
| onedir（默认） | onefile 每次启动解压上百 MB，首启慢 3~8 秒，还容易被杀软拦 |

## 2. 编译安装包

```bat
:: 装过一次 Inno Setup 之后
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\potato.iss
:: 或者只装了当前用户
"%LOCALAPPDATA%\InnoSetup6\ISCC.exe" installer\potato.iss
```

产出 `dist_desktop\installer\PotatoTest-Setup-<版本>.exe`（约 **182 MB**）。

安装包的行为：

- **不含任何数据**：没有 `potato.db` / `artifacts` / `profiles` / `.env`，装完首次启动自动建空库；
- 装到 `%LOCALAPPDATA%\Programs\PotatoTest`（per-user，**不需要管理员**）；
- **桌面快捷方式默认不勾**，启动菜单必建；另有可选开机自启；
- 卸载默认**保留用户数据**（只清 `logs`）。

> ★ **别装到 `Program Files`**：数据库、上传的原件、浏览器 profile 都落在程序目录旁边，
> 那边对普通用户不可写 —— 症状是"能打开、一保存就报错"，而且错误只在日志里。
> `.iss` 里加了安装前的目录可写检查，就是为了拦住这种装法。

## 3. 发布（让"检查更新"有东西可查）

```bat
python scripts\release.py --bump 1.0.1      &:: 三处版本号一起改
:: 重新打包 + 编译安装包（上面 1、2 两步）
set GITHUB_TOKEN=ghp_xxx                     &:: 需要 repo 权限的 token
python scripts\release.py --publish          &:: 建 Release + 上传安装包
```

版本号有**三个地方**必须一致（`app/version.py` / `pyproject.toml` / `installer/potato.iss`）——
`tests/test_updater.py` 会检查，`release.py` 也会在发布前再校验一次。

## 4. 更新（应用内）

设置页底部「版本」卡：

1. 点**检查更新** → 读本仓库的 `releases/latest`；
2. 有新版本 → **下载并安装** → 下载到临时目录 → 延迟 6 秒启动安装器 →
   应用退出 → 静默覆盖安装（`/VERYSILENT /CLOSEAPPLICATIONS`）；
3. **数据不动**（数据库/原件跟程序文件是分开的）。

安全边界（`app/updater.py` 文件头有完整说明）：

- **只有桌面版能装**（`sys.frozen`）；网页/源码模式一律 400；
- **下载地址白名单**：必须 `https://github.com/<本仓库>/releases/download/` 且文件名匹配
  `PotatoTest-Setup-*.exe` —— 这个接口会执行下载下来的 exe，不能放开。

> 安装包没有代码签名：用户**首次手动双击**时 Windows 可能提示"未知发布者"。
> 但应用内更新下载的文件不带网络来源标记，一般不会触发这个提示。
