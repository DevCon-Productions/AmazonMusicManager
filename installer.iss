; Inno Setup script for Amazon Music Manager.
; Prerequisite: build the app first so dist\AmazonMusicManager\ exists:
;     pyinstaller --noconfirm AmazonMusicManager.spec
; Then compile this script (produces installer_output\AmazonMusicManager-Setup-1.0.exe):
;     "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
; ...or open this file in the Inno Setup IDE and click Build.

#define AppName "Amazon Music Manager"
#define AppVersion "1.0"
#define AppPublisher "DevCon Productions"
#define AppExeName "AmazonMusicManager.exe"

[Setup]
; AppId uniquely identifies this app for upgrades/uninstall. Keep it stable
; across versions. (Generated once; do not change.)
AppId={{8F3A1C72-9B4E-4D6A-AE21-5C7D9E0F1234}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=installer_output
OutputBaseFilename=AmazonMusicManager-Setup-{#AppVersion}
SetupIconFile=AmazonMusicManager_Logo2-Square.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Installs into Program Files, so admin rights are requested.
PrivilegesRequired=admin
UninstallDisplayIcon={app}\{#AppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The entire PyInstaller onedir output (exe + all dependencies).
Source: "dist\AmazonMusicManager\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
