@echo off
rem Deploys the closed-loop PTZ console to this user's Desktop:
rem   OBSBOT_TAIL_2 PTZ_CLOSED_LOOP.pyw   the console itself (pythonw -> no
rem                                       console window behind it)
rem   OBSBOT_TAIL_2 PTZ_CLOSED_LOOP.bat   double-click launcher
rem Copies the repo's tools/ptz_console.py -- re-run this after any update.
rem Requires Python (pythonw on PATH -- a stock python.org install qualifies).
setlocal
set "SRC=%~dp0ptz_console.py"
set "DST=%USERPROFILE%\Desktop\OBSBOT_TAIL_2 PTZ_CLOSED_LOOP.pyw"
set "BAT=%USERPROFILE%\Desktop\OBSBOT_TAIL_2 PTZ_CLOSED_LOOP.bat"

if not exist "%SRC%" (
  echo ERROR: "%SRC%" not found - run this from the repo's tools\ folder.
  exit /b 1
)

copy /y "%SRC%" "%DST%" >nul
if errorlevel 1 (
  echo ERROR: could not copy to "%DST%".
  exit /b 1
)

> "%BAT%" echo @echo off
>> "%BAT%" echo start "" pythonw "%%~dp0OBSBOT_TAIL_2 PTZ_CLOSED_LOOP.pyw"

echo deployed:
echo   %DST%
echo   %BAT%
echo Launch by double-clicking either file (the .bat keeps no console open).
echo Close the console window to quit.
endlocal
