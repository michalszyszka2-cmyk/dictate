; Inno Setup script for dictate -- wraps the PyInstaller onedir build into one setup .exe.
; Build with packaging\build.cmd, which passes /DAppVersion from dictate\__init__.py.
; Per-user install, no admin prompt. Settings, history, the API key and the speech
; model live in %APPDATA%\dictate and are left alone by install, upgrade and uninstall.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{E8B5A419-D3F3-4CD3-B964-5CD49D64F433}
AppName=dictate
AppVersion={#AppVersion}
AppPublisher=Michal Szyszka
DefaultDirName={localappdata}\Programs\dictate
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\build\installer
OutputBaseFilename=dictate-setup-{#AppVersion}
SetupIconFile=..\assets\dictate.ico
UninstallDisplayIcon={app}\dictate.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
; Same shortcut name the tray's "Start with Windows" toggle uses (install.py), so the two agree.
Name: "startup"; Description: "Start dictate when Windows starts"
Name: "desktopicon"; Description: "Desktop shortcut"; Flags: unchecked

[Files]
Source: "..\build\dist\dictate\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; An upgrade replaces the whole bundle, so no stale DLLs from the old version linger.
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{userprograms}\dictate"; Filename: "{app}\dictate.exe"
Name: "{userstartup}\dictate"; Filename: "{app}\dictate.exe"; Tasks: startup
Name: "{userdesktop}\dictate"; Filename: "{app}\dictate.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\dictate.exe"; Description: "Start dictate now"; Flags: nowait postinstall

[Code]
procedure StopDictate();
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM dictate.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(500);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopDictate();
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  StopDictate();
  Result := True;
end;

{ The tray's "Start with Windows" toggle may have written dictate.lnk itself.
  Remove it only if it points at this install -- a dev copy's shortcut to
  dictate.cmd under the same name must survive. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Lnk: String;
  Target: String;
  Shell, Link: Variant;
begin
  if CurUninstallStep <> usPostUninstall then
    exit;
  Lnk := ExpandConstant('{userstartup}\dictate.lnk');
  if not FileExists(Lnk) then
    exit;
  try
    Shell := CreateOleObject('WScript.Shell');
    Link := Shell.CreateShortcut(Lnk);
    Target := Link.TargetPath;
  except
    exit;
  end;
  if CompareText(Target, ExpandConstant('{app}\dictate.exe')) = 0 then
    DeleteFile(Lnk);
end;
