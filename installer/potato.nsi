; Potato Test 桌面版安装包脚本（NSIS 3.11+）
;
; 编译：  makensis installer\potato.nsi
; 前置：  先跑 scripts\build_desktop.py 生成 dist_desktop\PotatoTest\*
;
; ── 为什么从 Inno Setup 换成 NSIS（2026-10-21）──────────────────────────────
;
; Inno 的 Setup.exe（SetupLdr）在**读取 .iss 之前**会先把主安装程序解压到
; `%TEMP%\is-XXXX.tmp`。用户机器上 TEMP 目录不可写时，安装立刻死在引导阶段：
;
;   安装程序无法创建目录 "...\Temp\is-XXXX.tmp"。错误 5: 拒绝访问。
;
; 这不是 .iss 能改的：PrivilegesRequired=lowest、[Code]、UseSetupLdr=no 都改变不了
; （UseSetupLdr=no 还会产成 setup.exe + *.bin 多文件，不能单文件分发）。
;
; NSIS 安装器直接从自身 exe 流式读取数据写入目标目录，**不经过 %TEMP%**，且
; RequestExecutionLevel user 不需要管理员。这是用户要求的"改代码、不提权"方案。
;
; ★ 不要在这里使用任何 NSIS 插件（nsExec / InetC / nsProcess / System ...）：
;   插件必须先解压到 $PLUGINSDIR（又是临时目录），会把同一条路走回去。
;   本脚本只用内置指令 + 纯宏（MUI2 / LogicLib / FileFunc 的 GetParameters）。
;
; ── 行为约定 ───────────────────────────────────────────────────────────────
;
; * per-user 安装到 %LOCALAPPDATA%\Programs\PotatoTest，不弹 UAC；
; * 静默参数：NSIS 原生 `/S`，同时识别旧版 Inno 更新器传入的 `/VERYSILENT`；
; * 覆盖安装只覆盖程序文件，**不动** potato.db / artifacts / profiles / logs / .env；
; * 卸载同样只删程序文件，用户数据留在原目录；
; * 桌面快捷方式默认不勾，开始菜单必建；
; * 安装目录必须可写（挡住 Program Files）。

Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "Sections.nsh"

; makensis 会把工作目录切到脚本所在目录，File/OutFile 的相对路径都相对 installer/

!define AppName "Potato Test"
!define AppExe "PotatoTest.exe"
!define AppVer "1.0.6"
!define AppRegKey "Software\PotatoTest"
!define UninstRegKey "Software\Microsoft\Windows\CurrentVersion\Uninstall\PotatoTest"
; 旧 Inno 版安装器写入的卸载键，升级时清掉
!define SEC_DESKTOP 1  ; 第二个 Section（桌面快捷方式）的索引
!define OldInnoUninstKey "Software\Microsoft\Windows\CurrentVersion\Uninstall\{8E2B7F4A-3C51-4C0E-9E77-5B2D8D1A6F31}_is1"

Name "${AppName}"
BrandingText "${AppName} ${AppVer}"

OutFile "..\dist_desktop\installer\PotatoTest-Setup-${AppVer}.exe"
InstallDir "$LOCALAPPDATA\Programs\PotatoTest"
InstallDirRegKey HKCU "${AppRegKey}" "InstallDir"
RequestExecutionLevel user

SetCompressor /SOLID lzma
SetCompressorDictSize 32

; ── 界面 ───────────────────────────────────────────────────────────────────
!define MUI_ABORTWARNING

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"

; ── 安装 ──────────────────────────────────────────────────────────────────
Section "Potato Test 程序（必需）" SEC_MAIN
  SectionIn RO

  SetOutPath "$INSTDIR"
  ; 程序 + _internal + .env.example；不含任何用户数据
  File /r "..\dist_desktop\PotatoTest\*.*"

  WriteUninstaller "$INSTDIR\uninstall.exe"

  ; 迁移：清掉旧 Inno 安装器文件与卸载项，避免控制面板里出现两个 Potato Test
  Delete "$INSTDIR\unins000.exe"
  Delete "$INSTDIR\unins000.dat"
  DeleteRegKey HKCU "${OldInnoUninstKey}"

  ; 开始菜单（必建）
  CreateDirectory "$SMPROGRAMS\${AppName}"
  CreateShortcut "$SMPROGRAMS\${AppName}\${AppName}.lnk" "$INSTDIR\${AppExe}"
  CreateShortcut "$SMPROGRAMS\${AppName}\卸载 ${AppName}.lnk" "$INSTDIR\uninstall.exe"
  ; 旧 Inno 把快捷方式直接放在“程序”根下，清掉
  Delete "$SMPROGRAMS\${AppName}.lnk"

  ; 卸载信息
  WriteRegStr HKCU "${AppRegKey}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${UninstRegKey}" "DisplayName" "${AppName}"
  WriteRegStr HKCU "${UninstRegKey}" "DisplayVersion" "${AppVer}"
  WriteRegStr HKCU "${UninstRegKey}" "Publisher" "Potato Test"
  WriteRegStr HKCU "${UninstRegKey}" "DisplayIcon" "$INSTDIR\${AppExe}"
  WriteRegStr HKCU "${UninstRegKey}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKCU "${UninstRegKey}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
  WriteRegDWORD HKCU "${UninstRegKey}" "NoModify" 1
  WriteRegDWORD HKCU "${UninstRegKey}" "NoRepair" 1

  ; 记住桌面快捷方式的选择，供下次升级沿用
  SectionGetFlags ${SEC_DESKTOP} $0
  IntOp $0 $0 & ${SF_SELECTED}
  ${If} $0 == ${SF_SELECTED}
    WriteRegDWORD HKCU "${AppRegKey}" "DesktopIcon" 1
  ${Else}
    WriteRegDWORD HKCU "${AppRegKey}" "DesktopIcon" 0
  ${EndIf}
SectionEnd

Section "桌面快捷方式"
  CreateShortcut "$DESKTOP\${AppName}.lnk" "$INSTDIR\${AppExe}"
SectionEnd

; ── 卸载 ──────────────────────────────────────────────────────────────────
Section "Uninstall"
  Delete "$SMPROGRAMS\${AppName}\${AppName}.lnk"
  Delete "$SMPROGRAMS\${AppName}\卸载 ${AppName}.lnk"
  RMDir "$SMPROGRAMS\${AppName}"
  Delete "$SMPROGRAMS\${AppName}.lnk"
  Delete "$DESKTOP\${AppName}.lnk"

  ; ★ 只删程序文件。potato.db / artifacts / profiles / logs / .env 一律保留。
  Delete "$INSTDIR\${AppExe}"
  Delete "$INSTDIR\.env.example"
  RMDir /r "$INSTDIR\_internal"
  Delete "$INSTDIR\unins000.exe"
  Delete "$INSTDIR\unins000.dat"
  Delete "$INSTDIR\uninstall.exe"
  ; 目录非空（有用户数据）时这条会静默失败，正是想要的结果
  RMDir "$INSTDIR"

  DeleteRegKey HKCU "${UninstRegKey}"
  DeleteRegKey HKCU "${OldInnoUninstKey}"
  DeleteRegKey HKCU "${AppRegKey}"
SectionEnd

; ── 初始化 ────────────────────────────────────────────────────────────────
Function .onInit
  ; 旧版 1.0.5 更新器用 Inno 的参数启动新安装器。识别后转成 NSIS 静默模式，
  ; 否则升级会弹出完整向导、卡在等用户点击。
  ${GetParameters} $R0
  ${GetOptions} $R0 "/VERYSILENT" $R1
  ${IfNot} ${Errors}
    SetSilent silent
  ${EndIf}

  ; 桌面快捷方式默认不勾；升级时沿用上次选择（或旧 Inno 留下的桌面图标）
  SectionSetFlags ${SEC_DESKTOP} 0
  ReadRegDWORD $0 HKCU "${AppRegKey}" "DesktopIcon"
  ${If} $0 == 1
    SectionSetFlags ${SEC_DESKTOP} ${SF_SELECTED}
  ${Else}
    IfFileExists "$DESKTOP\${AppName}.lnk" 0 +2
      SectionSetFlags ${SEC_DESKTOP} ${SF_SELECTED}
  ${EndIf}

  ; 程序在运行就装不了：非静默提示用户，静默则等它自己退出（最多 15 秒）
  FindWindow $0 "" "${AppName}"
  ${If} $0 != 0
    ${If} ${Silent}
      StrCpy $1 0
      ${Do}
        Sleep 500
        FindWindow $0 "" "${AppName}"
        ${If} $0 == 0
          ${ExitDo}
        ${EndIf}
        IntOp $1 $1 + 1
      ${LoopUntil} $1 >= 30
      FindWindow $0 "" "${AppName}"
      ${If} $0 != 0
        SetErrorLevel 2
        Quit
      ${EndIf}
    ${Else}
      MessageBox MB_ICONSTOP|MB_OK "Potato Test 正在运行，请先退出后再安装。"
      Abort
    ${EndIf}
  ${EndIf}
FunctionEnd

; 安装目录必须对当前用户可写。默认目录本来就满足；这里挡住用户手动改成
; Program Files 的情况 —— 装进去会"能打开、一存东西就报错"。
Function .onVerifyInstDir
  CreateDirectory "$INSTDIR"
  ClearErrors
  FileOpen $0 "$INSTDIR\.potato-write-test" w
  IfErrors not_writable
  FileClose $0
  Delete "$INSTDIR\.potato-write-test"
  Goto done
not_writable:
  MessageBox MB_ICONSTOP|MB_OK "当前用户没有这个目录的写权限：$\r$\n$INSTDIR$\r$\n$\r$\n请选择“文档”或其他用户目录下的路径（例如默认路径）。"
  Abort
done:
FunctionEnd
