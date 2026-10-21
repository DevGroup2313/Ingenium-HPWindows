@echo off
setlocal enabledelayedexpansion
rem INGENIUM  ^|  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
rem NO LICENCE IS GRANTED. See LICENCE.txt. Every distributed copy carries a
rem build marker.
rem
rem Ingenium -- build the standalone Windows app.
rem Double-click this file. It needs Python 3 on THIS PC only; the finished
rem Ingenium.exe needs nothing installed on the PCs you run it on, except the
rem Microsoft Edge WebView2 runtime, which ships with Windows 11 and updated
rem Windows 10 (see WINDOWS_README.txt if a window will not open).
rem
rem R23: every step prints live as it works (no more silent window), the tool
rem install fails in seconds -- not minutes -- when the Python is too new,
rem tools already installed are never downloaded again, and the finished exe
rem is also placed on your Desktop.

cd /d "%~dp0"
set "FOLDER=%CD%"
set "LOG=build_log.txt"
> "%LOG%" echo Ingenium -- building the standalone Windows app
>> "%LOG%" echo Started %date% %time%

call :say ""
call :say "Ingenium -- building the standalone Windows app"
call :say ""
call :say "You do not have to build anything to use Ingenium: ingenium.html in"
call :say "this folder IS the whole app. This step wraps it in its own window as"
call :say "an .exe, with its own engine (llama.cpp, many times faster than the"
call :say "engine inside the page) and a built-in model. The first build downloads"
call :say "the engine (about 40 MB) and the model (about 1.3 GB) once; later builds"
call :say "reuse them. Every step prints what it is doing as it goes, and the"
call :say "finished app is installed for you with a shortcut on your Desktop."
call :say ""

rem --- [1/4] find a Python 3 the build tools actually support ----------------
call :say "[1/7] Checking Python..."
rem The build tools (PyInstaller, pythonnet) lag brand-new Python releases by
rem months. A machine whose default Python is too new fails the install step
rem with "no matching distribution" and nothing gets built. So prefer a Python
rem the tools are known to support, even when a newer one is the default.
set "PY="
where py >nul 2>&1
if not errorlevel 1 (
  py -3.13 -c "pass" >nul 2>&1
  if not errorlevel 1 set "PY=py -3.13"
  if not defined PY (
    py -3.12 -c "pass" >nul 2>&1
    if not errorlevel 1 set "PY=py -3.12"
  )
)

if defined PY (
  %PY% -c "import sys;sys.exit(0 if sys.version_info[:2] <= (3,13) else 7)" >nul 2>&1
  if errorlevel 7 set "PY="
)
if not defined PY goto :need_python
goto :python_ready

:need_python
rem R63: when no Python the build tools support is on this PC, the script no
rem longer stops at the advice. Windows 11 carries winget, which installs Python
rem 3.13 in place on one Y press, and the script then continues by itself. A
rem newer Python (3.14) that already exists is left entirely untouched: 3.13
rem installs beside it and only this build uses it.
call :say "No Python the build tools support was selected for this build."
for /f "delims=" %%V in ('py -3 --version 2^>^&1') do set "HAVEANY=%%V"
if defined HAVEANY call :say "This PC has !HAVEANY!, which is too new for the build tools today. 3.13 can live beside it untouched."
where winget >nul 2>&1
if errorlevel 1 goto :python_manual
call :say ""
set /p "PYGO=Install Python 3.13 for you now with winget? (Y/N): "
if /i not "!PYGO!"=="Y" goto :python_manual
call :say "Installing Python 3.13 (a minute or two)..."
winget install -e --id Python.Python.3.13 --accept-package-agreements --accept-source-agreements >> "%LOG%" 2>&1
if errorlevel 1 (
  call :say "winget could not do it this time. The manual route:"
  goto :python_manual
)
py -3.13 -c "pass" >nul 2>&1
if errorlevel 1 (
  call :say ""
  call :say "Python 3.13 installed, but this window cannot see it yet."
  call :say "Close this window, double-click the Build file again, and it"
  call :say "continues by itself."
  pause
  exit /b 1
)
set "PY=py -3.13"
goto :python_ready

:python_manual
call :say "Install Python 3.13 from https://www.python.org/downloads/"
call :say "During install, tick Add python.exe to PATH, then run this again."
pause
exit /b 1

:python_ready
for /f "delims=" %%V in ('%PY% --version 2^>^&1') do (
  call :say "Found: %%V (this copy of Python does the building)"
)

rem --- [2/4] private build environment and tools ----------------------------
call :say ""
call :say "[2/7] The build tools (PyInstaller and pywebview)..."
set "VENV=.buildenv"
set "VPY=%FOLDER%\%VENV%\Scripts\python.exe"
set "TOOLS_OK=0"
if exist "%VPY%" (
  "%VPY%" -c "import webview, PyInstaller" >nul 2>&1 && set "TOOLS_OK=1"
)
if "!TOOLS_OK!"=="1" (
  call :say "Already in place from a previous run -- nothing to download."
) else (
  if not exist "%VPY%" (
    call :say "Creating a private build environment (one time)..."
    %PY% -m venv "%VENV%" >> "%LOG%" 2>&1
    if errorlevel 1 goto tools_failed
  )
  if exist "wheels\*.whl" (
    call :say "Installing from the local wheels folder (no connection needed)..."
    rem R75: proxy_tools, a small part of pywebview, is published only as source
    call :runlive "'%VPY%' -m pip install --no-index --find-links wheels setuptools wheel"
    call :runlive "'%VPY%' -m pip install --no-index --find-links wheels --no-build-isolation proxy_tools"
    call :runlive "'%VPY%' -m pip install --no-index --find-links wheels --only-binary=:all: pyinstaller pywebview certifi"
    if errorlevel 1 goto tools_failed
  ) else (
    call :say "Downloading and installing them now (about 25 MB, one time;"
    call :say "kept afterwards, so the next run skips this entirely):"
    call :runlive "'%VPY%' -m pip install --disable-pip-version-check --only-binary=:all: --upgrade pip"
    rem R75: proxy_tools, a small part of pywebview, is published only as source
    rem (plain Python, installed in seconds); the rest must come ready-made
    call :runlive "'%VPY%' -m pip install --disable-pip-version-check proxy_tools"
    if errorlevel 1 goto tools_failed
    call :runlive "'%VPY%' -m pip install --disable-pip-version-check --only-binary=:all: "pythonnet>=3.0.3" "pywebview>=5" "pyinstaller>=6" certifi"
    if errorlevel 1 goto tools_failed
  )
)
rem R49: whatever pip resolved, prove it is a pywebview new enough to carry the
rem app's window (the events API and private storage). An ancient copy would
rem build an exe that crashes on open, exactly as the 3.14 machine showed.
"%VPY%" -c "import sys,webview; v=getattr(webview,'__version__','0'); sys.exit(0 if int(str(v).split('.')[0])>=5 else 8)" >nul 2>&1
if errorlevel 8 (
  call :say ""
  call :say "STOPPING HERE: the pywebview that installed is too old for this app."
  call :say "Clearing the private build folder and stopping so the next run gets"
  call :say "a fresh one. Run this file again once you are on Python 3.12 or 3.13."
  rmdir /s /q "%VENV%" >nul 2>&1
  call :say ""
  pause
  exit /b 1
)
for /f "delims=" %%V in ('"%VPY%" -m PyInstaller --version 2^>^&1') do call :say "PyInstaller: %%V"

rem --- [3/4] bake the local engine ------------------------------------------
call :say ""
call :say "[3/7] Checking the engine inside the page (it comes baked into ingenium.html)..."
call :runlive "'%VPY%' bake_engine.py ingenium.html standard"
if errorlevel 1 (
  call :say "Engine bake was skipped (usually: no connection). The app can"
  call :say "still add the engine on its Models page, so the build carries on."
)

rem --- who the app is (app.json) ----------------------------------------------
set "APP_FILE=Ingenium"
for /f "usebackq delims=" %%A in (`"%VPY%" tools\app_name.py file_name`) do set "APP_FILE=%%A"

rem --- [4/7] the app's own engine -------------------------------------------
call :say ""
call :say "[4/7] The app's own engine (llama.cpp, for Windows)..."
call :runlive "'%VPY%' tools\get_engine_windows.py engine\windows-x64.zip"
if errorlevel 1 (
  call :say "The engine could not be downloaded (usually: no connection). The app"
  call :say "is built without it and uses the slower engine inside the page."
  if exist "engine\windows-x64.zip" del /f /q "engine\windows-x64.zip"
)

rem --- [5/7] build ----------------------------------------------------------
call :say ""
call :say "[5/7] Building %APP_FILE%.exe..."
call :say "The longest step. Lines appear below as the bundler works."
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
rem R77: this build's own number (the version, the day and the time it was
rem built), shown in About, as the cloud builds show theirs
set "BUILD_NO="
for /f "usebackq delims=" %%B in (`powershell -NoProfile -Command "Get-Date -Format yyyyMMdd.HHmm"`) do set "BUILD_NO=6.1.%%B"
if defined BUILD_NO (
  call :runlive "'%VPY%' tools\stamp_build.py ingenium.html !BUILD_NO!"
  call :say "Build number: !BUILD_NO!"
)
call :runlive "'%VPY%' -m PyInstaller --noconfirm --clean ingenium_win.spec"
if errorlevel 1 goto build_failed
if not exist "dist\%APP_FILE%.exe" goto build_failed

rem --- [6/7] install, with the built-in model --------------------------------
call :say ""
call :say "[6/7] Installing %APP_FILE% with its built-in model..."
set "INST=%LOCALAPPDATA%\Programs\%APP_FILE%"
if not exist "%INST%" mkdir "%INST%"
copy /y "dist\%APP_FILE%.exe" "%INST%\%APP_FILE%.exe" >nul 2>&1
if errorlevel 1 (
  call :say "Could not replace %INST%\%APP_FILE%.exe: is %APP_FILE% still open?"
  call :say "Close it and run this file again; nothing is lost."
  pause
  exit /b 1
)
call :runlive "'%VPY%' tools\prepare_desktop_model.py '%INST%\models'"
if errorlevel 1 (
  call :say "The built-in model could not be downloaded (usually: no connection)."
  call :say "The app works without it; models can be added in the app at any time."
)

rem --- [7/7] test, and the Desktop shortcut ----------------------------------
call :say ""
call :say "[7/7] Testing the app's engine with the built-in model..."
if exist "engine\windows-x64.zip" if exist "%INST%\models\builtin.json" (
  start "" /wait "%INST%\%APP_FILE%.exe" --selftest-model builtin --selftest-out "%FOLDER%\selftest.json"
  if errorlevel 1 (
    call :say "The test did not pass; the app is installed anyway. What it said:"
    type "%FOLDER%\selftest.json"
    type "%FOLDER%\selftest.json" >> "%LOG%"
  ) else (
    for /f "usebackq delims=" %%R in (`"%VPY%" -c "import json;r=json.load(open(r'%FOLDER%\selftest.json'));print('Passed: the model answered %%r at %%.1f words a second' %% (r['reply'], (r.get('timings') or {}).get('predicted_per_second') or 0))"`) do call :say "%%R"
  )
) else (
  call :say "(Skipped: the engine or the built-in model is not in this build.)"
)
for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`) do set "DESK=%%D"
if defined DESK (
  rem the copy earlier builds placed on the Desktop gives way to a shortcut
  if exist "%DESK%\%APP_FILE%.exe" del /f /q "%DESK%\%APP_FILE%.exe" >nul 2>&1
  powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%DESK%\%APP_FILE%.lnk');$s.TargetPath='%INST%\%APP_FILE%.exe';$s.WorkingDirectory='%INST%';$s.Save()" >nul 2>&1
  if exist "%DESK%\%APP_FILE%.lnk" (
    call :say "A shortcut is on your Desktop:  %DESK%\%APP_FILE%.lnk"
  ) else (
    call :say "(Could not make a Desktop shortcut; the app is in %INST%.)"
  )
)

call :say ""
call :say "SUCCESS."
call :say "Your app is installed in:  %INST%"
call :say "(the program, %APP_FILE%.exe, and its models folder)"
call :say ""
call :say "Open it from the Desktop shortcut. If SmartScreen warns the first time,"
call :say "click More info, then Run anyway: it only means the exe is not"
call :say "code-signed, not that anything is wrong. If a window will not open,"
call :say "install the Edge WebView2 runtime; see WINDOWS_README.txt."
call :say ""
start "" "%INST%"
pause
exit /b 0

:tools_failed
call :say ""
call :say "The build tools could not be installed."
call :say ""
call :say "The last lines of the log:"
powershell -NoProfile -Command "Get-Content '%LOG%' -Tail 18" 2>nul
call :say ""
findstr /I /C:"no matching distribution" /C:"could not find a version" "%LOG%" >nul && (
  echo READ THIS FIRST: no build tool exists yet for this version of Python.
  echo PyInstaller lags a few months behind each new Python release. Install
  echo Python 3.12 or 3.13 from python.org alongside what you have ^(the two
  echo live side by side; nothing breaks^), then run this again.
  call :say ""
)
findstr /I /C:"could not resolve" /C:"failed to establish" /C:"network is unreachable" /C:"timed out" "%LOG%" >nul && (
  echo This step needs the internet once, to download PyInstaller and pywebview.
  echo Connect and run this again. To build with no connection, on any
  echo connected PC run:   python -m pip download pyinstaller pywebview certifi setuptools wheel -d wheels
  echo then copy the wheels folder next to this file.
  call :say ""
)
call :say "Full log:  %FOLDER%\%LOG%"
pause
exit /b 1

:build_failed
call :say ""
call :say "The build did not finish. The last lines of the log:"
powershell -NoProfile -Command "Get-Content '%LOG%' -Tail 18" 2>nul
call :say ""
call :say "Full log:  %FOLDER%\%LOG% -- send that file and the cause can be found."
pause
exit /b 1

:say
echo %~1
>> "%LOG%" echo %~1
exit /b 0

:runlive
rem Run one command live on screen AND into the log; keep ITS exit code.
powershell -NoProfile -ExecutionPolicy Bypass -Command "& %~1 2>&1 | Tee-Object -FilePath '%LOG%' -Append; exit $LASTEXITCODE"
exit /b %ERRORLEVEL%
