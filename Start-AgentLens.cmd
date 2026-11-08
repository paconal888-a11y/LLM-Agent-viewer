@echo off
rem 실행 정책(Restricted/RemoteSigned)과 다운로드 표식 때문에 .ps1이 막힐 때 쓰는 진입점입니다. 정책을 바꾸지 않고 이 호출에만 Bypass를 적용합니다.
rem PowerShell 7 passes its PSModulePath to child processes; Windows PowerShell 5.1 then cannot load its own modules.
setlocal
set "PSModulePath="
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-AgentLens.ps1" %*
