@echo off
REM INGENIUM  ^|  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
REM NO LICENCE IS GRANTED. See LICENCE.txt. Every distributed copy carries a
REM build marker.
REM
REM Ingenium - run the app in its own window WITHOUT building an exe.
REM
REM Use this when the built exe will not start, or before building one at all.
REM It needs Python 3 on this PC. Unlike the exe, this window STAYS OPEN and
REM prints the reason if anything goes wrong, so a fault can be read instead
REM of guessed at. (This is the same no-build runner the Forge package carries.)
REM
REM This file must keep Windows line endings.

setlocal
cd /d "%~dp0"

echo Ingenium - starting in its own window
echo.

where py >nul 2>nul
if errorlevel 1 goto :use_python_word
set "PY=py"
goto :have_py
:use_python_word
set "PY=python"
:have_py

%PY% --version >nul 2>nul
if errorlevel 1 goto :no_python
for /f "delims=" %%v in ('%PY% --version 2^>^&1') do echo Python:  %%v

REM A Python newer than the one the window component follows would install an
REM old, incompatible copy. Say so plainly instead of failing strangely.
%PY% -c "import sys,os;sys.exit(0 if sys.version_info[:2] <= (3,13) else 7)" >nul 2>nul
if errorlevel 7 goto :python_too_new

REM pywebview draws the window. Install it once if it is absent, into the user's
REM own Python, which needs no administrator rights.
%PY% -c "import webview" >nul 2>nul
if errorlevel 1 goto :install_webview
goto :run

:install_webview
echo Installing the one component that draws the window. A minute, once only.
%PY% -m pip install --user --disable-pip-version-check "pywebview>=5"
if errorlevel 1 goto :install_failed
REM On Windows the .NET backend also wants pythonnet. Not fatal if it refuses:
REM Edge WebView2 works without it.
%PY% -m pip install --user --disable-pip-version-check "pythonnet>=3"
goto :run

:run
echo.
echo Opening Ingenium...
%PY% ingenium_app.py
if errorlevel 1 goto :ran_badly
echo.
echo Ingenium has closed.
goto :finish

:ran_badly
echo.
echo Ingenium stopped with an error. The reason is printed above, and also
echo written to ingenium_crash.log beside this file.
echo.
echo Whatever happens, ingenium.html in this folder IS the whole app.
goto :finish

:python_too_new
echo.
echo This Python is newer than 3.13, and the window component (pywebview) does
echo not support it yet. Install Python 3.13 from python.org alongside this one
echo (the two live side by side; nothing breaks), then run this file again.
goto :finish

:no_python
echo Python 3 was not found on this PC.
echo Install Python 3.13 from https://www.python.org/downloads/ and tick
echo "Add Python to PATH" on the first screen.
goto :finish

:install_failed
echo.
echo The window component could not be installed.
echo Try:  %PY% -m pip install --user "pywebview>=5"
goto :finish

:finish
echo.
pause
endlocal
exit /b
