; Inno Setup script for the Windows installer.
; Build after PyInstaller:  ISCC /DAppVersion=0.1.0 packaging\windows\installer.iss

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{6F1B2C1E-6A55-4C8E-9C7B-4C6D2A1F0B7E}
AppName=MirrorBridge
AppVersion={#AppVersion}
AppPublisher=MirrorBridge
DefaultDirName={autopf}\MirrorBridge
DefaultGroupName=MirrorBridge
DisableProgramGroupPage=yes
OutputDir=..\..\dist
OutputBaseFilename=MirrorBridge-{#AppVersion}-windows-setup
SetupIconFile=..\icons\MirrorBridge.ico
UninstallDisplayIcon={app}\MirrorBridge.exe
LicenseFile=..\..\LICENSE
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\..\dist\MirrorBridge\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\MirrorBridge"; Filename: "{app}\MirrorBridge.exe"
Name: "{autodesktop}\MirrorBridge"; Filename: "{app}\MirrorBridge.exe"; Tasks: desktopicon

[Run]
; Let iPhones, iPads and Macs on home/work networks reach the receiver.
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""MirrorBridge"""; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""MirrorBridge"" dir=in action=allow program=""{app}\MirrorBridge.exe"" enable=yes profile=private,domain"; Flags: runhidden
Filename: "{app}\MirrorBridge.exe"; Description: "Start MirrorBridge"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""MirrorBridge"""; Flags: runhidden; RunOnceId: "RemoveFirewallRule"
