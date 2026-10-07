; Установщик Монитора Основит DIY (Inno Setup 6). Собирается и проверяется в CI: .github/workflows/ci.yml (package).
;
;   iscc /DAppVersion=2.8.0 /DSourceDir=<развёрнутая сборка с python\> /DOutputDir=dist installer\setup.iss
;   подпись (если есть сертификат): добавить /DSign "/Sosnovit=signtool sign ... $f"
;
; Как сняты риски установщика:
;   • без прав администратора: ставится в папку пользователя (%LOCALAPPDATA%\Programs), там можно писать —
;     данные, как и в архиве, лежат в папке data рядом с программой;
;   • обновление не трогает data: в установщике её нет, а [InstallDelete] чистит только папки кода;
;   • удаление снимает задачи Планировщика (start.py --unschedule) и спрашивает про данные — по умолчанию
;     оставляет; при тихом удалении (/VERYSILENT) данные остаются всегда;
;   • запущенная программа (окно или сбор по расписанию) перед обновлением и удалением закрывается — иначе
;     занятые файлы Python сломали бы обновление или остались бы после удаления;
;   • в Program Files не ставится: программа хранит данные рядом с собой, а там у пользователя нет прав на запись.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\build\stage"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif
#define AppName "Монитор Основит DIY"

[Setup]
AppId={{8C2F5B1E-6A3D-4F7B-9E21-0D4C6B7A9F13}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
VersionInfoVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\{#AppName}
UsePreviousAppDir=yes
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=Osnovit-DIY-{#AppVersion}-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
UninstallDisplayName={#AppName}
SetupLogging=yes
#ifdef Sign
SignTool=osnovit
SignedUninstaller=yes
#endif

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Ярлык на рабочем столе"; GroupDescription: "Ярлыки:"

[InstallDelete]
; при обновлении — только код программы и Python: старые файлы не должны «пережить» новую версию.
; Папку data не трогаем никогда.
Type: filesandordirs; Name: "{app}\monitor"
Type: filesandordirs; Name: "{app}\ui"
Type: filesandordirs; Name: "{app}\config"
Type: filesandordirs; Name: "{app}\tests"
Type: filesandordirs; Name: "{app}\docs"
Type: filesandordirs; Name: "{app}\python"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
Name: "{app}\data"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\Запустить.cmd"; WorkingDir: "{app}"; Flags: runminimized; Comment: "Цены Петровича и Лемана ПРО против Основит"
Name: "{autoprograms}\{#AppName} — проверка компьютера"; Filename: "{app}\Запустить.cmd"; Parameters: "--selftest"; WorkingDir: "{app}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\Запустить.cmd"; WorkingDir: "{app}"; Flags: runminimized; Tasks: desktopicon

[Run]
Filename: "{app}\Запустить.cmd"; WorkingDir: "{app}"; Description: "Запустить {#AppName}"; Flags: postinstall nowait skipifsilent runminimized shellexec

[UninstallDelete]
; кэши Python, которые программа создаёт сама во время работы
Type: filesandordirs; Name: "{app}\monitor\__pycache__"
Type: filesandordirs; Name: "{app}\tests\__pycache__"
Type: filesandordirs; Name: "{app}\python\__pycache__"
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]
{ Закрыть запущенную программу: процессы python.exe из папки программы (окно, сбор, проверка Планировщика).
  Ошибки не мешают: если PowerShell недоступен, остаётся штатное закрытие занятых файлов Inno Setup. }
procedure StopApp();
var
  Dir: String;
  ResultCode: Integer;
begin
  Dir := ExpandConstant('{app}\python\');
  if not DirExists(Dir) then
    Exit;
  StringChangeEx(Dir, '''', '''''', True);
  { PSModulePath из окружения сбрасываем на системный: путь от PowerShell 7 ломает модули Windows PowerShell }
  Exec('powershell.exe', '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "' +
    '$env:PSModulePath = [Environment]::GetEnvironmentVariable(''PSModulePath'', ''Machine''); ' +
    'Get-Process python, pythonw -ErrorAction SilentlyContinue | ' +
    'Where-Object { $_.Path -and $_.Path.StartsWith(''' + Dir + ''', [StringComparison]::OrdinalIgnoreCase) } | ' +
    'Stop-Process -Force; Start-Sleep -Seconds 1"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function HasData(): Boolean;
begin
  Result := FileExists(ExpandConstant('{app}\data\monitor-2.1.sqlite')) or
    FileExists(ExpandConstant('{app}\data\settings-2.1.json'));
end;

function UnderProgramFiles(Path: String): Boolean;
var
  P: String;
begin
  P := Lowercase(AddBackslash(Path));
  Result := (Pos(Lowercase(AddBackslash(ExpandConstant('{commonpf64}'))), P) = 1) or
    (Pos(Lowercase(AddBackslash(ExpandConstant('{commonpf32}'))), P) = 1);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpSelectDir) and UnderProgramFiles(WizardDirValue()) then
  begin
    MsgBox('В Program Files программу ставить нельзя: она хранит собранные данные рядом с собой, ' +
      'а там у обычного пользователя нет прав на запись.' + #13#10#13#10 +
      'Выберите другую папку — например, предложенную по умолчанию.', mbError, MB_OK);
    Result := False;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if UnderProgramFiles(ExpandConstant('{app}')) then
  begin
    Result := 'В Program Files программу ставить нельзя: она хранит данные рядом с собой. Выберите другую папку.';
    Exit;
  end;
  StopApp();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    StopApp();
    { задачи Планировщика снимаем, пока программа ещё на месте: иначе они остались бы и каждый день
      сообщали об ошибке запуска }
    Exec(ExpandConstant('{app}\python\python.exe'), '"' + ExpandConstant('{app}\start.py') + '" --unschedule',
      ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
  if (CurUninstallStep = usPostUninstall) and HasData() and (not UninstallSilent) then
  begin
    if MsgBox('Удалить и собранные данные — цены, историю, настройки, резервные копии?' + #13#10#13#10 +
      'Нет — данные останутся в папке:' + #13#10 + ExpandConstant('{app}\data') + #13#10 +
      'и подхватятся, если поставить программу снова.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(ExpandConstant('{app}'), True, True, True);
  end;
end;
