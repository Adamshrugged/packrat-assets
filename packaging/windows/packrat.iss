; Windows installer for Packrat (Inno Setup 6). Installs for the current user, so no admin rights are needed.
; Build: iscc /DMyVersion=1.2.3 packaging\windows\packrat.iss   (after the PyInstaller bundle; see packaging/README.md)
#ifndef MyVersion
  #define MyVersion "0.0.0"
#endif

[Setup]
AppId={{F36330CF-5A5F-4D54-99F8-ED9BBFF0C9BF}
AppName=Packrat
AppVersion={#MyVersion}
AppPublisher=Adamshrugged
AppPublisherURL=https://github.com/Adamshrugged/packrat-assets
DefaultDirName={localappdata}\Programs\Packrat
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist
OutputBaseFilename=Packrat-Setup-{#MyVersion}
SetupIconFile=..\..\build\icons\packrat.ico
UninstallDisplayIcon={app}\Packrat.exe
UninstallDisplayName=Packrat
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
; Closes a running Packrat before updating it
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "..\..\dist\Packrat\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\Packrat"; Filename: "{app}\Packrat.exe"
Name: "{autodesktop}\Packrat"; Filename: "{app}\Packrat.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Packrat.exe"; Description: "Open Packrat"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/im Packrat.exe /f"; Flags: runhidden; RunOnceId: "StopPackrat"

[Registry]
; Settings > Start when I log in adds this value; uninstalling removes it. Your settings and database are kept.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "Packrat"; Flags: uninsdeletevalue dontcreatekey
