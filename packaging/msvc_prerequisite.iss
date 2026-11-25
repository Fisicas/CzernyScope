{ Include after [Code] in the installer. Requires Inno Setup with CreateDownloadPage.
  Source: Microsoft's WinGet catalog, Microsoft.VCRedist.2015+.x64 14.52.37029.0.
  https://github.com/microsoft/winget-pkgs/blob/master/manifests/m/Microsoft/VCRedist/2015%2B/x64/14.52.37029.0/Microsoft.VCRedist.2015%2B.x64.installer.yaml
  The installer obtains the prerequisite directly from Microsoft. Keep the URL,
  SHA-256 and package version together when updating the pin. }
const
  VCRuntimeKey = 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64';
  VCRuntimeMinimum = '14.52.37029.0';
  VCRedistVersion = '14.52.37029.0';
  VCRedistFile = 'VC_redist.x64.exe';
  VCRedistSHA256 = '9C02C9ED120F671B900E6B9471D890E3B2414117A7E53F4AE2E855466FA2B7C9';
  VCRedistURL =
    'https://download.visualstudio.microsoft.com/download/pr/' +
    '78d061c5-9ef3-42ab-98ed-3309d1095dc4/' + VCRedistSHA256 + '/' + VCRedistFile;
  WindowsCancelled = 1223;
  WindowsRestartInitiated = 1641;
  WindowsRestartRequired = 3010;

var
  VCRuntimeDownloadPage: TDownloadWizardPage;

function ReadRuntimeVersion(RootKey: Integer; var Version: Int64): Boolean;
var
  Installed, Major, Minor, Build, Revision: Cardinal;
begin
  Result :=
    RegQueryDWordValue(RootKey, VCRuntimeKey, 'Installed', Installed) and
    (Installed = 1) and
    RegQueryDWordValue(RootKey, VCRuntimeKey, 'Major', Major) and
    RegQueryDWordValue(RootKey, VCRuntimeKey, 'Minor', Minor) and
    RegQueryDWordValue(RootKey, VCRuntimeKey, 'Bld', Build) and
    RegQueryDWordValue(RootKey, VCRuntimeKey, 'Rbld', Revision) and
    (Major <= 65535) and (Minor <= 65535) and
    (Build <= 65535) and (Revision <= 65535);
  if Result then
    Version := PackVersionComponents(Major, Minor, Build, Revision);
end;

function HasRequiredVCRuntime: Boolean;
var
  InstalledVersion, MinimumVersion: Int64;
begin
  { The app and prerequisite are x64. Both registry views can record the x64
    package. Query the explicit x64 package name in each view. }
  MinimumVersion := PackVersionComponents(14, 52, 37029, 0);
  Result := ReadRuntimeVersion(HKLM64, InstalledVersion) and
    (ComparePackedVersion(InstalledVersion, MinimumVersion) >= 0);
  if not Result then
    Result := ReadRuntimeVersion(HKLM32, InstalledVersion) and
      (ComparePackedVersion(InstalledVersion, MinimumVersion) >= 0);
  Result := Result and FileExists(ExpandConstant('{sys}\msvcp140.dll')) and
    FileExists(ExpandConstant('{sys}\msvcp140_1.dll')) and
    FileExists(ExpandConstant('{sys}\msvcp140_2.dll')) and
    FileExists(ExpandConstant('{sys}\vcruntime140.dll')) and
    FileExists(ExpandConstant('{sys}\vcruntime140_1.dll'));
  if Result then
    Log('Microsoft Visual C++ x64 runtime detected: ' + VersionToStr(InstalledVersion));
end;

procedure InitializeWizard;
begin
  VCRuntimeDownloadPage := CreateDownloadPage(
    'Getting the Microsoft Visual C++ Runtime',
    'Downloading the required 64-bit runtime from Microsoft.', nil);
  VCRuntimeDownloadPage.ShowBaseNameInsteadOfUrl := True;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  RedistPath, RedistParams: String;
  ExitCode: Integer;
begin
  Result := '';
  if HasRequiredVCRuntime then
    Exit;

  if WizardSilent and not IsAdmin then begin
    Result := 'The Microsoft Visual C++ x64 runtime is required. Run interactive setup to ' +
      'approve its installation, or install it before running silent setup.';
    Exit;
  end;

  Log('Microsoft Visual C++ x64 prerequisite minimum: ' + VCRuntimeMinimum);
  Log('Downloading Microsoft package ' + VCRedistVersion + ', SHA-256: ' + VCRedistSHA256);
  VCRuntimeDownloadPage.Clear;
  VCRuntimeDownloadPage.Add(VCRedistURL, VCRedistFile, VCRedistSHA256);
  VCRuntimeDownloadPage.Show;
  try
    try
      VCRuntimeDownloadPage.Download;
    except
      if VCRuntimeDownloadPage.AbortedByUser then
        Result := 'The Microsoft Visual C++ Runtime download was canceled.'
      else
        Result := 'Could not download or verify the Microsoft Visual C++ Runtime. ' +
          GetExceptionMessage;
      Exit;
    end;
  finally
    VCRuntimeDownloadPage.Hide;
  end;

  { Another installer may have satisfied the prerequisite while this downloaded. }
  if HasRequiredVCRuntime then
    Exit;

  RedistPath := ExpandConstant('{tmp}\') + VCRedistFile;
  if WizardSilent then
    RedistParams := '/install /quiet /norestart'
  else
    RedistParams := '/install /passive /norestart';
  if not ShellExec('runas', RedistPath, RedistParams, '',
    SW_SHOWNORMAL, ewWaitUntilTerminated, ExitCode) then begin
    if ExitCode = WindowsCancelled then
      Result := 'Installation of the Microsoft Visual C++ Runtime was canceled.'
    else
      Result := 'Could not start the Microsoft Visual C++ Runtime installer. ' +
        SysErrorMessage(ExitCode);
    Exit;
  end;

  Log('Microsoft Visual C++ Runtime installer exit code: ' + IntToStr(ExitCode));
  if (ExitCode = WindowsRestartRequired) or (ExitCode = WindowsRestartInitiated) then begin
    NeedsRestart := not WizardSilent;
    Result := 'Restart Windows to finish installing the Microsoft Visual C++ Runtime, ' +
      'then run CzernyScope Setup again.';
    Exit;
  end;

  if not HasRequiredVCRuntime then
    Result := 'The Microsoft Visual C++ x64 runtime ' + VCRuntimeMinimum +
      ' or newer is still unavailable. The runtime installer returned ' +
      IntToStr(ExitCode) + '.';
end;
