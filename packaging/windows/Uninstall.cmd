@echo off
setlocal
:: Uninstaller for the PORTABLE build of EDB Explorer.
:: The portable build keeps everything (settings, signing key, trusted signers, disk cache) in the "data"
:: folder next to this script and writes nothing to the registry or your profile.
set "APPDIR=%~dp0"
set "APPDIR=%APPDIR:~0,-1%"
echo.
echo  EDB Explorer (portable) - uninstall
echo  Folder: %APPDIR%
echo.
choice /C YN /M "Delete the settings, signing key, trusted signers and cache in %APPDIR%\data"
if errorlevel 2 goto keepdata
if exist "%APPDIR%\data" rd /s /q "%APPDIR%\data"
echo  Removed %APPDIR%\data
:keepdata
echo.
choice /C YN /M "Delete the whole application folder as well (removes EDB Explorer completely)"
if errorlevel 2 goto done
:: A batch file cannot delete the folder it runs from - hand the job to a copy in %TEMP%.
set "HELPER=%TEMP%\edb-explorer-uninstall-%RANDOM%.cmd"
(
  echo @echo off
  echo timeout /t 2 /nobreak ^>nul
  echo rd /s /q "%APPDIR%"
  echo if exist "%APPDIR%" ^(echo Some files could not be removed - close EDB Explorer and delete the folder manually.^) else ^(echo EDB Explorer has been removed.^)
  echo pause
  echo del "%%~f0"
) > "%HELPER%"
start "" cmd /c "%HELPER%"
exit /b 0
:done
echo  Done. The application files were left in place.
pause
