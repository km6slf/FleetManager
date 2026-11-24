; Inno Setup script for the AREDN Fleet Manager Windows installer.
; Build after PyInstaller:  iscc /DAppVersion=0.2.0 packaging\windows\installer.iss
#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif

[Setup]
AppId={{8F3C2A51-6D2B-4E7A-9C1E-5B7A2E4D9F10}
AppName=AREDN Fleet Manager
AppVersion={#AppVersion}
AppPublisher=KM6SLF
AppPublisherURL=https://github.com/km6slf/FleetManager
AppSupportURL=https://github.com/km6slf/FleetManager/issues
DefaultDirName={autopf}\AREDN Fleet Manager
DefaultGroupName=AREDN Fleet Manager
; Installs per-user without admin rights; can still be installed for all users.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\..\dist
OutputBaseFilename=AREDN-Fleet-Manager-{#AppVersion}-setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\AREDN-Fleet-Manager.exe
LicenseFile=..\..\LICENSE
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "..\..\dist\AREDN-Fleet-Manager\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\AREDN Fleet Manager"; Filename: "{app}\AREDN-Fleet-Manager.exe"
Name: "{group}\Uninstall AREDN Fleet Manager"; Filename: "{uninstallexe}"
Name: "{autodesktop}\AREDN Fleet Manager"; Filename: "{app}\AREDN-Fleet-Manager.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\AREDN-Fleet-Manager.exe"; Description: "Start AREDN Fleet Manager"; Flags: nowait postinstall skipifsilent

; Settings, logins and the firmware cache live in %APPDATA% / %LOCALAPPDATA%
; and are deliberately left in place on uninstall.
