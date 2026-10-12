; ===========================================================================
;  南苑抢课助手 · Windows 安装包（Inno Setup 6）
;  编译：ISCC.exe installer.iss
;  产物：dist\onedir-安装包版\nfu-qk-v<版本>-setup.exe
;  源：  dist\onedir-安装包版\南苑抢课助手\  ← **必须先跑 onedir 打包**（安装包只装 onedir 形态）
; ===========================================================================
;
;  🔴🔴 【装到哪】= %LOCALAPPDATA%\南苑抢课助手 —— **绝不要**改到 {pf}
;      （Program Files）。原因：本程序把 state/（选课清单/课程快照）和
;      logs/（运行日志）写在 **exe 同级目录**（见 serve.py::_app_root()），
;      而 Program Files 对普通用户是只读的 ⇒ 日志打不开、清单写不进。
;      2026-10-12 实测：产物放进只读目录，双击直接弹
;      「Unhandled exception in script」，服务根本起不来（16–19s 无响应）。
;
;      ⇒ DefaultDirName + PrivilegesRequired=lowest + DisableDirPage=yes
;        这**三行是一组**，改一个必须同时想清楚另外两个：
;        · 装到用户目录      → 免管理员权限（不发生 UAC）
;        · 不让用户改目录    → 防止手滑改成 Program Files 把程序改坏
;
;  🔴 【卸载不删用户数据】：state/ 与 logs/ 是程序跑起来之后才建的，
;      不在安装清单里 ⇒ Inno 卸载时不会动它们（{app} 目录会因非空而保留）。
;      末尾 [Code] 段会在卸载完成时把数据目录路径告诉用户，避免误以为全删了。
; ===========================================================================

#define AppName "南苑抢课助手"
#define AppNameEn "nanyuan-xk-assistant"
#define AppVersion "1.1.0"
#define AppExe "南苑抢课助手.exe"
#define AppSrc "dist\onedir-安装包版\南苑抢课助手"
#define RepoURL "https://github.com/tatattaa/nanyuan-xk-assistant"

[Setup]
; AppId 是卸载/升级的识别键，**一旦发布就不能再改**（改了会变成两个独立程序）。
AppId={{CF569140-43F6-42F4-875A-FC456E169F61}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=tatattaa
AppPublisherURL={#RepoURL}
AppSupportURL={#RepoURL}/issues
AppUpdatesURL={#RepoURL}/releases

; —— 安装位置：用户目录（可写）+ 普通用户权限（免 UAC）+ 锁死目录页 ——
DefaultDirName={localappdata}\{#AppName}
DisableDirPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=

DefaultGroupName={#AppName}
AllowNoIcons=yes
DisableProgramGroupPage=yes

; —— 产物 ——
OutputDir=dist\onedir-安装包版
OutputBaseFilename=nfu-qk-v{#AppVersion}-setup
SetupIconFile=assets\app.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}

; —— 压缩 / 界面 ——
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
MinVersion=10.0

; ⚠️ 本安装包**没有代码签名**（无证书）⇒ 用户首次运行会被 SmartScreen 拦
;    「未知发布者」，点「更多信息 → 仍要运行」即可。这不是安装包能解决的问题。

VersionInfoVersion=1.1.0
VersionInfoCompany=tatattaa
VersionInfoDescription={#AppName} 安装程序
VersionInfoProductName={#AppName}
VersionInfoProductVersion=1.1.0

[Languages]
Name: "chinese"; MessagesFile: "installer\ChineseSimplified.isl"

[Tasks]
; 默认**不勾选**桌面图标：开始菜单里已经有了，桌面留不留让用户自己决定。
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："; Flags: unchecked

[Files]
; 整个 onedir 目录树（含 _internal\）一起装 —— 少一个文件都起不来。
Source: "{#AppSrc}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[Code]
const
  // ⚠️ 换行码必须包在常量里：直接写 13/10 那种字符码且落在行首时，
  //    会被 ISPP 预处理器当成 # 开头的指令而报 Unknown preprocessor directive。
  //    另外 [Code] 段是 Pascal 语法，注释只能用 // ，不能用 INI 的 ; 。
  CRLF = #13#10;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
  Msg: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{app}');
    if DirExists(DataDir) then
    begin
      Msg := '程序已卸载。' + CRLF + CRLF +
             '注意：你保存的选课清单、蹲课清单和课程快照没有被删除，仍在这里：' +
             CRLF + DataDir + CRLF + CRLF +
             '需要彻底清理的话，请手动删除该文件夹。';
      MsgBox(Msg, mbInformation, MB_OK);
    end;
  end;
end;
