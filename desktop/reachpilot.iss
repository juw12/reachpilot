; ReachPilot for Windows - per-user installer (no admin rights). build.py compiles it with:
;   iscc /DAppVersion=1.0.0 /DSourceDir=<dist\ReachPilot> /DOutputDir=<dist> /DIconFile=<build\reachpilot.ico> reachpilot.iss
; Installs to %LOCALAPPDATA%\Programs\ReachPilot, adds ReachPilot to the Start menu (and optionally the desktop) and lists it
; in Settings > Apps. The window keeps its data (what you saved in it) in %LOCALAPPDATA%\ReachPilot, which
; uninstalling leaves in place so a reinstall finds it again.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "dist\ReachPilot"
#endif
#ifndef OutputDir
  #define OutputDir "dist"
#endif
#ifndef IconFile
  #define IconFile "build\reachpilot.ico"
#endif

[Setup]
AppId={{E81247BA-698B-569E-A6DE-E1E1C366EEFB}
AppName=ReachPilot
AppVersion={#AppVersion}
AppVerName=ReachPilot {#AppVersion}
AppPublisher=Juwen Lie
AppPublisherURL=https://juw12.github.io/reachpilot/
AppSupportURL=https://github.com/juw12/reachpilot
DefaultDirName={autopf}\ReachPilot
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=ReachPilot-Windows-Setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\ReachPilot.exe
UninstallDisplayName=ReachPilot
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=no
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\ReachPilot"; Filename: "{app}\ReachPilot.exe"; Comment: "ReachPilot writes each post from a repos real facts"
Name: "{userdesktop}\ReachPilot"; Filename: "{app}\ReachPilot.exe"; Comment: "ReachPilot writes each post from a repos real facts"; Tasks: desktopicon

[Run]
Filename: "{app}\ReachPilot.exe"; Description: "{cm:LaunchProgram,ReachPilot}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\ReachPilot.exe"; Parameters: "--stop"; Flags: runhidden waituntilterminated; RunOnceId: "StopReachPilot"

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  { Updating: close an open ReachPilot window first so its files can be replaced. }
  if FileExists(ExpandConstant('{app}\ReachPilot.exe')) then
    Exec(ExpandConstant('{app}\ReachPilot.exe'), '--stop', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;
