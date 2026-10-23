; INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
; NO LICENCE IS GRANTED. See LICENCE.txt.
;
; R75: Ingenium-Setup.exe, the Windows installer (Inno Setup 6). It needs
; nothing on the PC: no Python, no administrator rights. It installs the app
; for this user in %LOCALAPPDATA%\Programs\<name>, with its engine unpacked
; beside it, puts shortcuts with the app's own icon on the Desktop and in the
; Start menu, and lists the app in Settings, Apps, so it can be removed there.
; Run again, it updates in place. It never touches the user's chats, notes,
; settings or downloaded models: those live in %LOCALAPPDATA%\<name> and
; %APPDATA%\<name>, outside the program folder.
;
; A "models" folder beside Ingenium-Setup.exe (the copy for a PC without
; internet) is installed too; without it, the app downloads its models the
; first time it opens.
;
; The cloud build passes the values below; built by hand from this folder
; after a folder build (INGENIUM_ONEDIR=1), the defaults find everything.

#ifndef AppName
  #define AppName "Ingenium"
#endif
#ifndef AppFile
  #define AppFile "Ingenium"
#endif
#ifndef AppIdent
  #define AppIdent "com.yannieforest.ingenium"
#endif
#ifndef AppVersion
  #define AppVersion "6.2"
#endif
#ifndef VerNum
  #define VerNum "3.2.0.0"
#endif
#ifndef RepoDir
  #define RepoDir ".."
#endif
#ifndef SrcDir
  #define SrcDir RepoDir + "\dist\" + AppFile
#endif
#ifndef EngineDir
  #define EngineDir RepoDir + "\engine\windows-x64"
#endif
#ifndef OutDir
  #define OutDir RepoDir + "\dist"
#endif

[Setup]
AppId={#AppIdent}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName}
AppPublisher=Yannie D. Forest
AppCopyright=Copyright (c) 2026 Yannie D. Forest
VersionInfoVersion={#VerNum}
VersionInfoDescription={#AppName} Setup
VersionInfoProductName={#AppName}
VersionInfoProductTextVersion={#AppVersion}
; for this user only: no administrator rights, nothing outside the profile
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#AppFile}
; one click: no questions before it installs
DisableWelcomePage=yes
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
WizardStyle=modern
SetupIconFile={#RepoDir}\ingenium.ico
UninstallDisplayIcon={app}\{#AppFile}.exe
UninstallDisplayName={#AppName}
OutputDir={#OutDir}
OutputBaseFilename={#AppFile}-Setup
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64
MinVersion=10.0
; an open copy of the app is closed before its files are replaced
CloseApplications=yes
RestartApplications=no
; a log in the temporary folder, in case anything goes wrong on a PC
SetupLogging=yes

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Messages]
FinishedHeadingLabel=[name] is installed
FinishedLabel=You will find [name] on the Desktop and in the Start menu. Your chats, notes, settings and models are kept from one version to the next.

[InstallDelete]
; what the earlier ways of installing left in the same folder: the Python
; version's files, and an engine and program parts this version replaces
Type: filesandordirs; Name: "{app}\engine"
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: filesandordirs; Name: "{app}\tools"
Type: files; Name: "{app}\*.py"
Type: files; Name: "{app}\*.pyw"
Type: files; Name: "{app}\*.bat"
Type: files; Name: "{app}\install.json"
Type: files; Name: "{app}\ingenium.html"
Type: files; Name: "{app}\BUILD.txt"
Type: files; Name: "{app}\engine_windows.json"

[Files]
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#EngineDir}\*"; DestDir: "{app}\engine\windows-x64"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#RepoDir}\licences\*"; DestDir: "{app}\licences"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#RepoDir}\WINDOWS_README.txt"; DestDir: "{app}"; Flags: ignoreversion
; the copy for a PC without internet: its models folder, beside this installer
Source: "{src}\models\builtin.json"; DestDir: "{app}\models"; Flags: external skipifsourcedoesntexist ignoreversion
Source: "{src}\models\*"; DestDir: "{app}\models"; Flags: external skipifsourcedoesntexist onlyifdoesntexist recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppFile}.exe"; WorkingDir: "{app}"; Comment: "{#AppName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppFile}.exe"; WorkingDir: "{app}"; Comment: "{#AppName}"

[Run]
Filename: "{app}\{#AppFile}.exe"; Description: "Open {#AppName} now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: files; Name: "{app}\ingenium_crash.log"
Type: files; Name: "{app}\ingenium_diag.txt"
Type: filesandordirs; Name: "{app}\models"
Type: filesandordirs; Name: "{app}\engine"

[Code]
{ Stops every program running from Folder: an engine left running would hold
  its files. Windows' own process list (CIM) gives each program's path. }
procedure StopProgramsUnder(const Folder: String);
var
  ResultCode: Integer;
  F, Ps: String;
begin
  F := Folder;
  StringChangeEx(F, '''', '''''', True);
  Ps := '-NoProfile -ExecutionPolicy Bypass -Command "$f=''' + F + '''; ' +
        'Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($f, [System.StringComparison]::OrdinalIgnoreCase) } | ' +
        'ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"';
  Exec(ExpandConstant('{sysnative}\WindowsPowerShell\v1.0\powershell.exe'), Ps, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  StopProgramsUnder(ExpandConstant('{app}\engine'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    StopProgramsUnder(ExpandConstant('{app}'));
end;
