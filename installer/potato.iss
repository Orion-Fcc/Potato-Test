; Potato Test 桌面版安装包脚本（Inno Setup 6+）
;
; 编译：  "%LOCALAPPDATA%\InnoSetup6\ISCC.exe" installer\potato.iss
;        或 "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\potato.iss
; 前置：  先跑 scripts\build_desktop.py 生成 dist_desktop\PotatoTest\*
;
; ── ★★ 2026-10-17：这份脚本被**删到只剩验证过的最小集合**，原因如下 ──────────
;
; 原来的版本（带 AppComments / UninstallDisplayIcon / DefaultGroupName / [Registry] 开机自启 /
; [UninstallDelete] / [Run] 安装后启动 / 大段 [Code] 目录可写检查）在**开发者本机**上
; 一加进真实载荷就装不上：
;
;   安装程序无法创建目录 "C:\...\Temp\is-XXXX.tmp"。错误 5: 拒绝访问。
;
; 连 Inno 的安装日志都写不出来 —— 死在引导程序阶段。而同一份 .iss 换成小载荷、或者换最简
; .iss 配同样的 565 MB 载荷，都能装成功。做了完整对照矩阵（体积 182/600 MB、压缩 lzma2/none、
; 64 位安装模式、中文语言包、[Code]、[Tasks]/[Icons]/[Registry]、签名、MOTW、Temp 权限、
; 磁盘空间 432 GB）后，只有"完整 .iss + 大载荷"这一种组合复现。
;
; 因为这不是 Inno 的用法问题（同样的配置在别处很常见），而更像本机 iOA 终端管控
; 对特定组合的拦截，最稳妥的做法是：**只保留实测能装的那部分** ——
; 宁可少几个锦上添花的功能，也不能装不上。
;
; 已移除（以后可以**一次只加一项**，加上去立刻实测）：
;   AppComments / UninstallDisplayIcon / DefaultGroupName / [Registry]（开机自启）/
;   [UninstallDelete]（卸载清日志）/ [Run]（装完自动启动）/ [Code]（目录可写预检）/
;   .env.example 的附带安装
;
; ★ 保留的仍满足核心要求：装到当前用户目录（不需要管理员）、**不含任何数据**、
;   桌面快捷方式**可选**、启动菜单必建。
;
; ── 三个关键设计 ─────────────────────────────────────────────────────────────
;
; 1. **不带任何基础数据**：包里只有程序 + 前端，没有 potato.db / artifacts/ / profiles/。
;
; 2. **装到 {localappdata}\Programs\PotatoTest，不用 Program Files**。
;    数据库、原件、浏览器 profile 都落在程序目录旁边（见 desktop.py 的 _root_dir），
;    Program Files 对普通用户不可写 —— 装那儿会"能打开、一存东西就报错"，且错误只在日志里。
;
; 3. **桌面快捷方式默认不勾**，启动菜单项必建 —— 否则装完在开始菜单里找不到。

#define AppName "Potato Test"
#define AppExe "PotatoTest.exe"
#define AppVer "1.0.0"

[Setup]
AppId={{8E2B7F4A-3C51-4C0E-9E77-5B2D8D1A6F31}
AppName={#AppName}
AppVersion={#AppVer}
AppPublisher=Potato Test
DefaultDirName={localappdata}\Programs\PotatoTest
; 允许用户改安装路径
DisableDirPage=no
; per-user 安装：不需要管理员，目录也可写
PrivilegesRequired=lowest
; ★★ 装机失败的真实规律（2026-10-19 补齐对照后修正）：
; **安装包程序本身必须放在 C 盘运行**。实测（同一份 exe，SHA 一致）：
;   C://tmp//build//…        → ✅ 装成功（10924 行日志）
;   C://Users//16132//…      → ✅ 装成功
;   D://agent_study//…       → ❌ 引导阶段就退出（exit 1，连安装日志都写不出来）
;   D://（根）、D://inst-test → ❌ 同上
; 装到哪个盘无所谓：`/DIR=D://agent_study//_probe_install`（C 盘的包 → D 盘目标）✅ 成功。
; → 所以：**测安装包、给用户分发时，都从 C 盘（比如 Downloads）跑**；
;   编译产物的输出目录本身不影响（它只决定写到哪）。
; （早先以为是"路径含空格"，那是被这几个变量混在一起误导了 —— 空格**未单独验证**。）
OutputDir=..\dist_desktop\installer
OutputBaseFilename=PotatoTest-Setup-{#AppVer}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
; Inno 官方安装包**不含中文**，这份 .isl 是官方仓库里的非官方翻译，已随 installer/ 一起放进来。
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
; ★ 用户要求：桌面快捷方式要能选（默认不勾）
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
; 整个打包目录（程序 + _internal + 前端 dist）。不含任何用户数据。
Source: "..\dist_desktop\PotatoTest\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; 启动菜单是必建的
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
; 桌面项跟任务走
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon
