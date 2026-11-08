@echo off
rem PowerShell 7 passes its PSModulePath to child processes; Windows PowerShell 5.1 then cannot load its own modules.
setlocal
set "PSModulePath="
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Stop-AgentLens.ps1" %*
