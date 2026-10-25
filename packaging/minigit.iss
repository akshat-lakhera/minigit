; Inno Setup script for the Windows installer. Installs for the current user (no admin
; prompt) and adds the install folder to the user's PATH, like Git for Windows does.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist"
#endif

[Setup]
AppId={{7D2E6C1A-8B1F-4F0C-9A5E-3C0B2F1D9E44}
AppName=minigit
AppVersion={#AppVersion}
AppPublisher=minigit
AppPublisherURL=https://github.com/akshat-lakhera/minigit
DefaultDirName={userpf}\minigit
DefaultGroupName=minigit
DisableProgramGroupPage=yes
DisableDirPage=no
PrivilegesRequired=lowest
OutputDir={#SourceDir}
OutputBaseFilename=minigit-{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
ChangesEnvironment=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName=minigit {#AppVersion}
WizardStyle=modern

[Files]
Source: "{#SourceDir}\minigit.exe"; DestDir: "{app}"; Flags: ignoreversion

[Registry]
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; \
  ValueData: "{olddata};{app}"; Check: NeedsAddPath(ExpandConstant('{app}'))

[Run]
Filename: "{cmd}"; Parameters: "/c echo minigit installed. Open a new terminal and run: minigit --help && pause"; \
  Flags: postinstall nowait skipifsilent runasoriginaluser; Description: "Show how to start"

[Code]
function NeedsAddPath(Param: string): boolean;
var
  OrigPath: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', OrigPath) then
  begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Uppercase(Param) + ';', ';' + Uppercase(OrigPath) + ';') = 0;
end;

procedure RemovePath(Param: string);
var
  OrigPath, NewPath: string;
  P: Integer;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', OrigPath) then
    exit;
  NewPath := ';' + OrigPath + ';';
  P := Pos(';' + Uppercase(Param) + ';', Uppercase(NewPath));
  if P = 0 then
    exit;
  Delete(NewPath, P, Length(Param) + 1);
  NewPath := Copy(NewPath, 2, Length(NewPath) - 2);
  RegWriteExpandStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', NewPath);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemovePath(ExpandConstant('{app}'));
end;
