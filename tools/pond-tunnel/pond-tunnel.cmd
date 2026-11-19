@echo off
"%~dp0..\..\python\python.exe" -X utf8 -m boring_pondnt.pond_tunnel --settings "%LOCALAPPDATA%\pond-tunnel\settings.json"
if errorlevel 1 pause
