; SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
; SPDX-License-Identifier: Apache-2.0

#ifndef AppVersion
  #error "AppVersion must be defined"
#endif
#ifndef SourceDir
  #error "SourceDir must be defined"
#endif
#ifndef OutputDir
  #error "OutputDir must be defined"
#endif

[Setup]
AppId={{8E0B6C2A-5A7D-4F1E-9C3B-2D6A1F4E7B90}
AppName=Ucho
AppVersion={#AppVersion}
AppVerName=Ucho {#AppVersion}
AppPublisher=Fryderyk Pryjma
AppPublisherURL=https://ucho.app
AppSupportURL=https://github.com/Fredpry/ucho/issues
DefaultDirName={%USERPROFILE}\Ucho\app
UsePreviousAppDir=yes
; install.ps1 creates Desktop and Start Menu shortcuts with the matching AppUserModelID.
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
AppMutex=Local\Ucho.App
OutputDir={#OutputDir}
OutputBaseFilename=Ucho-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=Ucho
UninstallDisplayIcon={uninstallexe}
ShowLanguageDialog=auto
#ifdef SetupIcon
SetupIconFile={#SetupIcon}
#endif

[Languages]
Name: "pl"; MessagesFile: "compiler:Languages\Polish.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
; UTF-8 with a BOM: Inno Setup 6 reads it as Unicode, so Polish keeps its letters.
en.RunningInstaller=Installing Python, libraries and the speech model (10 to 20 minutes). A console window shows the progress...
pl.RunningInstaller=Instaluję Pythona, biblioteki i model mowy (10 do 20 minut). Postęp widać w oknie konsoli...
en.InstallFailed=The installation did not finish. Details are in the log:%n%1%n%nRun Setup again or report a bug.
pl.InstallFailed=Instalacja nie została ukończona. Szczegóły są w logu:%n%1%n%nUruchom instalator jeszcze raz albo zgłoś błąd.
en.LaunchUcho=Start Ucho
pl.LaunchUcho=Uruchom Ucho

[InstallDelete]
; Remove shipped Python packages on upgrade so deleted modules do not linger, while keeping .venv, models and settings.
Type: filesandordirs; Name: "{app}\ucho"
Type: filesandordirs; Name: "{app}\ucho_mcp"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Run]
Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: """{app}\ucho.pyw"""; WorkingDir: "{app}"; Description: "{cm:LaunchUcho}"; Flags: postinstall nowait skipifsilent; Check: VenvReady

[UninstallDelete]
Type: dirifempty; Name: "{app}"

[Code]
#ifndef SkipInstallScript
procedure CurStepChanged(CurStep: TSetupStep);
var
  Params: string;
  ShowCmd: Integer;
  ResultCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    WizardForm.StatusLabel.Caption := CustomMessage('RunningInstaller');
    Params := '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\install.ps1') + '"';
    // Silent: the window is hidden, so install.ps1 must not wait for an answer
    // (it treats -NonInteractive like a non-interactive run). Otherwise the
    // console waits only after a failure; after success it closes and the
    // wizard advances to its finish page.
    if WizardSilent then
      Params := '-NonInteractive ' + Params
    else
      Params := Params + ' -PauseOnError';
    if WizardSilent then
      ShowCmd := SW_HIDE
    else
      ShowCmd := SW_SHOW;
    if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
                Params, ExpandConstant('{app}'), ShowCmd, ewWaitUntilTerminated, ResultCode) then
    begin
      Log('Failed to launch install.ps1, error code: ' + IntToStr(ResultCode));
      if not WizardSilent then
        MsgBox(FmtMessage(CustomMessage('InstallFailed'), [ExpandConstant('{app}\install.log')]), mbError, MB_OK);
    end
    else if ResultCode <> 0 then
    begin
      Log('install.ps1 exited with code: ' + IntToStr(ResultCode));
      if not WizardSilent then
        MsgBox(FmtMessage(CustomMessage('InstallFailed'), [ExpandConstant('{app}\install.log')]), mbError, MB_OK);
    end;
  end;
end;
#endif

function VenvReady: Boolean;
begin
  Result := FileExists(ExpandConstant('{app}\.venv\Scripts\pythonw.exe'));
end;

// [UninstallRun] cannot tell a silent uninstall from an interactive one: Inno
// evaluates its Check functions at install time. So uninstall.ps1 runs from
// here, before Inno removes the program files: with its questions in a
// console window, or hidden with -Yes (never touches recordings) when silent.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Params: string;
  ShowCmd: Integer;
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    Params := '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\uninstall.ps1') + '" -FromSetup';
    if UninstallSilent then
    begin
      Params := '-NonInteractive ' + Params + ' -Yes';
      ShowCmd := SW_HIDE;
    end
    else
      ShowCmd := SW_SHOW;
    if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
                Params, ExpandConstant('{app}'), ShowCmd, ewWaitUntilTerminated, ResultCode) then
      Log('Failed to launch uninstall.ps1, error code: ' + IntToStr(ResultCode))
    else if ResultCode <> 0 then
      Log('uninstall.ps1 exited with code: ' + IntToStr(ResultCode));
  end;
end;
