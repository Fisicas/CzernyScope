; Per-user CzernyScope installer. The stable AppId enables in-place upgrades.
#define AppExe "CzernyScope.exe"
#ifndef BundleDir
  #define BundleDir "..\dist\CzernyScope"
#endif
#define AppVersion GetStringFileInfo(BundleDir + "\" + AppExe, "ProductVersion")
#if AppVersion == ""
  #error Build the CzernyScope application folder first.
#endif

[Setup]
AppId={{64AE162A-438A-4A98-B210-6E47062FC11A}
AppName=CzernyScope
AppVersion={#AppVersion}
AppVerName=CzernyScope {#AppVersion}
VersionInfoVersion={#GetVersionNumbersString(BundleDir + "\" + AppExe)}
VersionInfoProductName=CzernyScope
VersionInfoDescription=CzernyScope installer
DefaultDirName={localappdata}\Programs\CzernyScope
DefaultGroupName=CzernyScope
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=CzernyScope-{#AppVersion}-setup
SetupIconFile=..\spectrometer_app\assets\spectrometer-live.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[InstallDelete]
; Replace the bundled runtime on upgrade. Measurement folders are outside {app}.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\CzernyScope"; Filename: "{app}\{#AppExe}"; AppUserModelID: "SpectrometerLive.Desktop"
Name: "{autodesktop}\CzernyScope"; Filename: "{app}\{#AppExe}"; AppUserModelID: "SpectrometerLive.Desktop"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start CzernyScope"; Flags: nowait postinstall skipifsilent
