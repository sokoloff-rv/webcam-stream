@echo off
setlocal
cd /d "%~dp0"
title Webcam stream

rem ============ LOGIN / PASSWORD - EDIT THESE TWO LINES ============
set "CAM_USER=admin"
set "CAM_PASSWORD=ChangeMe123"
rem ================================================================

rem ---- find a working Python ----
set "PY="
python -c "import sys" >nul 2>&1 && set "PY=python"
if not defined PY (
    py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
    echo.
    echo Python not found.
    echo Install it from https://www.python.org/downloads/
    echo IMPORTANT: check "Add Python to PATH" during setup.
    echo.
    pause
    exit /b 1
)

rem ---- make sure OpenCV is installed ----
%PY% -c "import cv2" >nul 2>&1
if errorlevel 1 (
    echo Installing OpenCV, this takes a minute...
    %PY% -m pip install opencv-python
    %PY% -c "import cv2" >nul 2>&1
    if errorlevel 1 (
        echo Retrying with --user...
        %PY% -m pip install --user opencv-python
    )
)

%PY% -c "import cv2" >nul 2>&1
if errorlevel 1 (
    echo.
    echo Could not install opencv-python automatically.
    echo Open a terminal and run:  python -m pip install opencv-python
    echo.
    pause
    exit /b 1
)

rem ---- run ----
%PY% webcam_stream.py --user "%CAM_USER%" --password "%CAM_PASSWORD%"

echo.
pause
