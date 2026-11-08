; Classark Exam Builder - the .exe installer (Inno Setup 6). Built by `package.py installer`,
; which passes AppVersion, SourceDir (the PyInstaller folder), OutputDir and IconFile, and
; Sign plus a /Sqbxsign=... command line when code signing is set up.
;
; Installs for the current user only (no admin prompt) into %LOCALAPPDATA%\Programs, with a
; Start menu folder and an optional desktop shortcut, and registers with Windows so
; Settings > Apps and Control Panel list it with Uninstall. Uninstalling leaves the user's
; exams and papers in %LOCALAPPDATA%\ClassarkExamBuilder (packaging/launcher.py).

#define AppName "Classark Exam Builder"
#define AppPublisher "Classark"
#define AppExe "ClassarkExamBuilder.exe"

[Setup]
; Never change AppId: it is how a newer version finds and replaces this one. The MSI
; (installer.wxs) has its own UpgradeCode - the two are separate installs.
AppId={{6F1C2A54-8B3E-4D7A-9C21-5E8F0B4A7D13}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoProductName={#AppName}
; just for the current user: {autopf} is then %LOCALAPPDATA%\Programs
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=yes
DisableReadyPage=yes
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
SetupIconFile={#IconFile}
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; an open copy is closed (and reopened after) rather than its files left half-replaced
CloseApplications=yes
RestartApplications=yes
Compression=lzma2/max
SolidCompression=yes
OutputDir={#OutputDir}
OutputBaseFilename=ClassarkExamBuilder-Setup-{#AppVersion}
#ifdef Sign
SignTool=qbxsign
SignedUninstaller=yes
#endif

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "installed.txt"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
; an upgrade replaces the whole program: no file from an older version is left behind
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Open {#AppName}"; Flags: nowait postinstall skipifsilent
