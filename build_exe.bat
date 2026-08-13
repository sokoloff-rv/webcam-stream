@echo off
setlocal
cd /d "%~dp0"
title Build WebcamStream.exe

set "SRC=%~dp0"
set "WORK=%TEMP%\wcs_build"
set "LOG=%WORK%\build_log.txt"

echo.
echo ================================================================
echo   Building portable WebcamStream.exe
echo   Build runs in a short temp path to avoid Windows path limits.
echo ================================================================
echo.

rem ---- find a working Python ----
set "PY="
python -c "import sys" >nul 2>&1 && set "PY=python"
if not defined PY (
    py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
    echo Python not found.
    echo Install it from https://www.python.org/downloads/
    echo IMPORTANT: check "Add Python to PATH" during setup.
    echo.
    pause
    exit /b 1
)
%PY% -c "import sys, platform; print('Python:', sys.version.split()[0], platform.machine()); print('Path  :', sys.executable)"

rem ---- prepare a clean short working directory ----
if exist "%WORK%" rmdir /s /q "%WORK%"
mkdir "%WORK%" 2>nul
if not exist "%WORK%" (
    echo Cannot create work folder: %WORK%
    pause
    exit /b 1
)
copy /y "%SRC%webcam_gui.py" "%WORK%\" >nul
copy /y "%SRC%webcam_stream.py" "%WORK%\" >nul
copy /y "%SRC%settings_store.py" "%WORK%\" >nul
if not exist "%WORK%\webcam_gui.py" (
    echo Source files not found next to this .bat file.
    echo Keep webcam_gui.py, webcam_stream.py and settings_store.py together.
    pause
    exit /b 1
)
echo Work folder: %WORK%
echo.

rem ---- Microsoft Store Python: sandboxed, camera usually blocked ----
%PY% -c "import sys; raise SystemExit(1 if 'WindowsApps' in sys.executable else 0)"
if errorlevel 1 (
    echo ----------------------------------------------------------------
    echo  NOTE: this Python comes from the Microsoft Store and runs in a
    echo  sandbox. The built exe may still fail to reach the camera.
    echo  If that happens, install Python from https://www.python.org/
    echo  and run this file again.
    echo ----------------------------------------------------------------
    echo.
)

echo [1/2] Installing dependencies...
%PY% -m pip install --upgrade pyinstaller opencv-python > "%LOG%" 2>&1
if errorlevel 1 (
    echo       FAILED. Last lines:
    echo.
    powershell -NoProfile -Command "Get-Content -Path '%LOG%' -Tail 20"
    echo.
    pause
    exit /b 1
)
echo       done.

echo [2/2] Building, this takes 2-5 minutes...
pushd "%WORK%"
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --noupx ^
    --name WebcamStream ^
    --distpath "%WORK%\dist" ^
    --workpath "%WORK%\tmp" ^
    --specpath "%WORK%" ^
    webcam_gui.py >> "%LOG%" 2>&1
popd

if not exist "%WORK%\dist\WebcamStream.exe" (
    echo.
    echo ================================================================
    echo   BUILD FAILED. Last 30 lines of the log:
    echo ================================================================
    echo.
    powershell -NoProfile -Command "Get-Content -Path '%LOG%' -Tail 30"
    echo.
    copy /y "%LOG%" "%SRC%build_log.txt" >nul 2>&1
    echo Full log copied next to this .bat as build_log.txt
    echo.
    pause
    exit /b 1
)

copy /y "%WORK%\dist\WebcamStream.exe" "%SRC%WebcamStream.exe" >nul
if not exist "%SRC%WebcamStream.exe" (
    echo.
    echo Built successfully, but could not copy the exe here
    echo (the folder path may be too long).
    echo Take it manually from:
    echo   %WORK%\dist\WebcamStream.exe
    echo.
    pause
    exit /b 0
)

echo.
echo ================================================================
echo   Done: WebcamStream.exe
echo   Portable - copy it anywhere, no installation needed.
echo   Tip: keep it in a short path like C:\WebcamStream
echo ================================================================
echo.
pause
