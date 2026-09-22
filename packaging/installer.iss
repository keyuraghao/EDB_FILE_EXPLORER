; Inno Setup script - builds EDB-Explorer-<version>-setup.exe from the PyInstaller output.
; Invoked by packaging\build.ps1:  iscc /DVersion=0.1.0 /DSource=dist\edb-explorer-0.1.0 packaging\installer.iss
#ifndef Version
  #define Version "0.0.0"
#endif
#ifndef Source
  #define Source "dist\edb-explorer-" + Version
#endif

[Setup]
; All relative paths below (and /DSource) are resolved from the repository root, not from packaging\.
SourceDir=..
AppId={{7C1B4A0E-2F0D-4C7A-9E7B-3D5A1F6E8B42}
AppName=EDB Explorer
AppVersion={#Version}
AppVerName=EDB Explorer {#Version}
AppPublisher=Keyur Aghao
AppPublisherURL=https://github.com/keyuraghao/EDB_FILE_EXPLORER
AppSupportURL=https://github.com/keyuraghao/EDB_FILE_EXPLORER/issues
DefaultDirName={autopf}\EDB Explorer
DefaultGroupName=EDB Explorer
UninstallDisplayIcon={app}\EDB-Explorer.exe
OutputDir=dist
OutputBaseFilename=EDB-Explorer-{#Version}-setup
SetupIconFile=src\edb_explorer\resources\icon.ico
LicenseFile=LICENSE
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequiredOverridesAllowed=dialog
ChangesEnvironment=yes

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"
Name: "addtopath"; Description: "Add the edb-explorer command-line tool to PATH (for scripts and MCP clients)"; GroupDescription: "Command line:"
Name: "fileassoc"; Description: "Open .edb / .dit files with EDB Explorer"; GroupDescription: "File associations:"; Flags: unchecked

[Files]
Source: "{#Source}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\EDB Explorer"; Filename: "{app}\EDB-Explorer.exe"
Name: "{group}\Uninstall EDB Explorer"; Filename: "{uninstallexe}"
Name: "{autodesktop}\EDB Explorer"; Filename: "{app}\EDB-Explorer.exe"; Tasks: desktopicon

[Registry]
Root: HKA; Subkey: "Software\Classes\.edb\OpenWithProgids"; ValueType: string; ValueName: "EDBExplorer.Database"; ValueData: ""; Flags: uninsdeletevalue; Tasks: fileassoc
Root: HKA; Subkey: "Software\Classes\.dit\OpenWithProgids"; ValueType: string; ValueName: "EDBExplorer.Database"; ValueData: ""; Flags: uninsdeletevalue; Tasks: fileassoc
Root: HKA; Subkey: "Software\Classes\EDBExplorer.Database"; ValueType: string; ValueName: ""; ValueData: "ESE Database"; Flags: uninsdeletekey; Tasks: fileassoc
Root: HKA; Subkey: "Software\Classes\EDBExplorer.Database\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\EDB-Explorer.exe,0"; Tasks: fileassoc
Root: HKA; Subkey: "Software\Classes\EDBExplorer.Database\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\EDB-Explorer.exe"" ""%1"""; Tasks: fileassoc
Root: HKA; Subkey: "{code:PathRegKey}"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Tasks: addtopath; Check: NeedsAddPath(ExpandConstant('{app}'))

[Run]
Filename: "{app}\EDB-Explorer.exe"; Description: "Launch EDB Explorer"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\data"

[Code]
// After the files are gone, offer to remove per-user state (settings in the registry, signing key and
// trusted signers in %APPDATA%, disk caches in %LOCALAPPDATA% / %TEMP%) so an uninstall can be complete.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Answer: Integer;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    Answer := MsgBox('Also remove your EDB Explorer settings, project signing key, trusted signers and caches?' + #13#10 + #13#10 +
                     'Choose No to keep them for a later reinstall.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2);
    if Answer = IDYES then
    begin
      RegDeleteKeyIncludingSubkeys(HKCU, 'Software\EDB Explorer');
      DelTree(ExpandConstant('{userappdata}\EDB Explorer'), True, True, True);
      DelTree(ExpandConstant('{localappdata}\EDB Explorer'), True, True, True);
      DelTree(ExpandConstant('{tmp}\..\edb-rows-*'), False, True, True);
    end;
  end;
end;

function PathRegKey(Param: string): string;
begin
  if IsAdminInstallMode then
    Result := 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'
  else
    Result := 'Environment';
end;

function NeedsAddPath(Param: string): boolean;
var
  OrigPath: string;
  Root: Integer;
begin
  if IsAdminInstallMode then Root := HKLM else Root := HKCU;
  if not RegQueryStringValue(Root, PathRegKey(''), 'Path', OrigPath) then
  begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Uppercase(Param) + ';', ';' + Uppercase(OrigPath) + ';') = 0;
end;
