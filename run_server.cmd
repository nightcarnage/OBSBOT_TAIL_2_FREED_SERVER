@echo off
rem Runs the Tail 2 FreeD fan-out server (keeps this window open with stats).
rem Requires Python 3.8+ on PATH; stdlib only, nothing to install.
cd /d %~dp0
python tail2_freed_server.py %*
if errorlevel 1 pause
