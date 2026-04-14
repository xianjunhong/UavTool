@echo off
setlocal

set "HOLD=1"
if /I "%~1"=="--no-pause" set "HOLD=0"

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

set "FALLBACK_ENV=C:\Users\Frank\.conda\envs\UavTool"

if "%CONDA_PREFIX%"=="" (
  set "ENV_BASE=%FALLBACK_ENV%"
) else (
  set "ENV_BASE=%CONDA_PREFIX%"
)

if not exist "%ENV_BASE%\python.exe" (
  echo [ERROR] Python not found in env: %ENV_BASE%
  goto :FAIL
)

echo [INFO] Using ENV_BASE=%ENV_BASE%

set "PROJ_DIR=%ENV_BASE%\Library\share\proj"
set "GDAL_DIR=%ENV_BASE%\Library\share\gdal"

if not exist "%PROJ_DIR%" (
  echo [ERROR] PROJ data not found: %PROJ_DIR%
  goto :FAIL
)

if not exist "%GDAL_DIR%" (
  echo [ERROR] GDAL data not found: %GDAL_DIR%
  goto :FAIL
)

"%ENV_BASE%\python.exe" -m PyInstaller --noconfirm --clean --windowed --name UavTool ^
  --icon "%SCRIPT_DIR%uav_icon.ico" ^
  --collect-all osgeo ^
  --collect-all pyproj ^
  --add-data "%SCRIPT_DIR%uav_icon.ico;." ^
  --add-data "%PROJ_DIR%;proj" ^
  --add-data "%GDAL_DIR%;gdal-data" ^
  main.py

if errorlevel 1 (
  echo [ERROR] Build failed.
  goto :FAIL
)

echo [OK] Build complete.
echo Output folder: dist\UavTool

if exist "%SystemRoot%\System32\ie4uinit.exe" (
  echo [INFO] Refreshing Windows icon cache...
  "%SystemRoot%\System32\ie4uinit.exe" -ClearIconCache
  "%SystemRoot%\System32\ie4uinit.exe" -show
)

goto :END

:FAIL
if "%HOLD%"=="1" pause
exit /b 1

:END
if "%HOLD%"=="1" pause
