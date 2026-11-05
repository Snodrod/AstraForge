@echo off
setlocal
cd /D "%~dp0..\.."
if not exist data\runtime mkdir data\runtime
dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart > data\runtime\wsl-install.log 2>&1
set "code=%ERRORLEVEL%"
if %code%==0 dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart >> data\runtime\wsl-install.log 2>&1
if %code%==0 set "code=%ERRORLEVEL%"
> data\runtime\wsl-install-exit.txt echo %code%
exit /B %code%
