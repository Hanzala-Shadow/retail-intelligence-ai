@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_esg_pipeline_fast.ps1" %*
exit /b %ERRORLEVEL%
