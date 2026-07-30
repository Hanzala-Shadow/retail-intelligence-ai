@echo off
setlocal

if /I not "%~1"=="RUN" if /I not "%~1"=="PREVIEW" goto :usage

set "MODE=%~1"
set "PARSER_WORKERS=%~2"
set "CHUNK_WORKERS=%~3"
if "%PARSER_WORKERS%"=="" set "PARSER_WORKERS=4"
if "%CHUNK_WORKERS%"=="" set "CHUNK_WORKERS=6"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_esg_pymupdf_full_corpus.ps1" -ConfirmRun "%MODE%" -ParserWorkers %PARSER_WORKERS% -ChunkWorkers %CHUNK_WORKERS%
exit /b %ERRORLEVEL%

:usage
echo Usage:
echo   %~nx0 PREVIEW [parser_workers] [chunk_workers]
echo   %~nx0 RUN     [parser_workers] [chunk_workers]
echo.
echo Example:
echo   %~nx0 RUN 4 6
exit /b 2
