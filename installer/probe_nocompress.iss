; Potato Test 桌面版安装包脚本（Inno Setup 6+）
;
; 编译：  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\potato.iss
; 前置：  先跑 scripts\build_desktop.py 生成 dist_desktop\PotatoTest\*
;
; ── 三个关键设计 ─────────────────────────────────────────────────────────────
;
; 1. **不带任何基础数据**（用户要求「封装成无基础数据的安装包」）。
;    打包里只有程序 + 前端，没有 potato.db / artifacts/ / profiles/ ——
;    用户装完第一次启动时自动建库，界面是空的。
;
; 2. **装到 {localappdata}\Programs\PotatoTest，不用 Program Files**。
;    数据库、原件、浏览器 profile 都落在程序目录旁边（见 desktop.py 的 _root_dir）。
;    Program Files 对普通用户**不可写** —— 装那儿会出现"能打开、一存东西就报错"，
;    而且错误只在日志里。per-user 安装同时也不需要管理员权限。
;
; 3. **桌面快捷方式默认不勾**（用户要求「安装时给用户选择」），启动菜单项必建 ——
;    否则装完在开始菜单里找不到。

#define AppName "Potato Test"
#define AppExe "PotatoTest.exe"
#define AppVer "1.0.0"
#define AppPublisher "Potato Test"

[Setup]
AppId={{8E2B7F4A-3C51-4C0E-9E77-5B2D8D1A6F31}
AppName=PT
AppVersion={#AppVer}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\PotatoTest
DefaultGroupName={#AppName}
; 允许用户改安装路径
DisableDirPage=no
; per-user 安装：不需要管理员，目录也可写
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist_desktop\installer
OutputBaseFilename=PT-nocompress
Compression=none
SolidCompression=no
WizardStyle=modern
; 325 MB 的包，给它足够的解压空间提示
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AppExe}
; 安装包本身不含数据 —— 这一行是给人看的承诺，不是配置
AppComments=不含任何项目/用例数据，首次启动自动建库。

[Languages]
; Inno 官方安装包**不含中文**，这份 .isl 是官方仓库里的非官方翻译，已随 installer/ 一起放进来。
; 用相对路径引用（相对本 .iss 所在目录），这样换机器/换编译器目录都不用改。
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
; ★ 用户要求：桌面快捷方式要能选（默认不勾）
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: unchecked
; 额外可选：开机自启（写 HKCU，不需要管理员）
Name: "autostart"; Description: "开机自动启动 Potato Test"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
; 整个打包目录（程序 + _internal + 前端 dist）。不含任何用户数据。
Source: "..\dist_desktop\PotatoTest\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 配置模板：首次启动没有 .env 时至少要让人知道该填什么
Source: "..\.env.example"; DestDir: "{app}"; DestName: ".env.example"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
; 启动菜单是必建的
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
; 桌面项跟任务走
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; 开机自启（per-user，卸载时自动清掉）
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
    ValueName: "PotatoTest"; ValueData: """{app}\{#AppExe}"""; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 只清日志 —— 数据库/原件/profile 属于**用户数据**，卸载时默认保留
; （要彻底删，用户在资源管理器里自己删 {app} 目录即可；这样设计是为了避免"卸载把资料删了"）。
Type: filesandordirs; Name: "{app}\logs"

[Code]
// 装之前确认目标目录可写 —— 不然就是"装完能用、一存数据就报错"，比装不上更难查。
function InitializeSetup(): Boolean;
var
  TestPath: String;
  TestFile: String;
begin
  Result := True;
  TestPath := ExpandConstant('{localappdata}\Programs\PotatoTest');
  // 只在默认路径上做检查；用户自定义路径由 Inno 自己的写入权限检查兜底
  if not DirExists(TestPath) then
  begin
    if not ForceDirectories(TestPath) then
    begin
      MsgBox('无法创建目录：' + TestPath + #13#10 +
             'Potato Test 需要把数据库和原件写在这个目录旁边，请换一个可写的位置。',
             mbError, MB_OK);
      Result := False;
    end;
  end;
  TestFile := TestPath + '\.write-test';
  if Result then
  begin
    if SaveStringToFile(TestFile, 'ok', False) then
      DeleteFile(TestFile)
    else
    begin
      MsgBox('这个目录不可写：' + TestPath + #13#10 +
             '安装后会出现"能打开、一保存就失败"。请选择别的安装位置。',
             mbError, MB_OK);
      Result := False;
    end;
  end;
end;
