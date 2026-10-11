#ifndef AppVersion
  #error AppVersion must be passed by build_windows.ps1
#endif
#ifndef BundleDirectory
  #error BundleDirectory must be passed by build_windows.ps1
#endif
#ifndef ArtifactDirectory
  #error ArtifactDirectory must be passed by build_windows.ps1
#endif
#ifndef Repository
  #define Repository "pumpkissster/Gravelle"
#endif

[Setup]
AppId={{6589345A-87B0-47CE-9424-71F892240DF7}
AppName=Gravelle
AppVersion={#AppVersion}
AppPublisher=Gravelle
AppPublisherURL=https://github.com/{#Repository}
AppSupportURL=https://github.com/{#Repository}/issues
AppUpdatesURL=https://github.com/{#Repository}/releases
DefaultDirName={localappdata}\Programs\Gravelle
DefaultGroupName=Gravelle
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir={#ArtifactDirectory}
OutputBaseFilename=Gravelle-Setup-{#AppVersion}-x64
SetupIconFile=gravelle.ico
UninstallDisplayIcon={app}\Gravelle.exe
LicenseFile=..\LICENSE
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
AppMutex=Local\GravelleLauncher-6589345A87B047CE942471F892240DF7
CloseApplications=no
RestartApplications=no
SetupLogging=yes
UninstallDisplayName=Gravelle
LanguageDetectionMethod=uilanguage
ShowLanguageDialog=yes
; Follow the current Windows UI language even when upgrading the English-only
; 1.8.2 installer. The user can still choose either language in the dialog.
UsePreviousLanguage=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimplified"; MessagesFile: "languages\ChineseSimplified.isl"

[CustomMessages]
english.DesktopShortcut=Create a desktop shortcut
english.ShortcutGroup=Shortcuts:
english.OpenGravelle=Open Gravelle
chinesesimplified.DesktopShortcut=创建桌面快捷方式
chinesesimplified.ShortcutGroup=快捷方式：
chinesesimplified.OpenGravelle=打开 Gravelle

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopShortcut}"; GroupDescription: "{cm:ShortcutGroup}"; Flags: unchecked

[Files]
Source: "{#BundleDirectory}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Gravelle"; Filename: "{app}\Gravelle.exe"
Name: "{autodesktop}\Gravelle"; Filename: "{app}\Gravelle.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Gravelle.exe"; Description: "{cm:OpenGravelle}"; Flags: nowait postinstall skipifsilent

; Account, library configuration and progress live outside {app}.
; No UninstallDelete rule: uninstall never removes LocalAppData\Gravelle.
