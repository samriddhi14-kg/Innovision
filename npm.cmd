@echo off
setlocal

:: Locate the real npm installation
set "REAL_NPM="
if exist "%ProgramFiles%\nodejs\npm.cmd" set "REAL_NPM=%ProgramFiles%\nodejs\npm.cmd"
if not defined REAL_NPM (
  for /f "skip=1 delims=" %%I in ('where npm.cmd 2^>nul') do (
    if not defined REAL_NPM set "REAL_NPM=%%I"
  )
)
if not defined REAL_NPM set "REAL_NPM=npm"

:: If user typed 'npm run' with no script specified, automatically run dev
if "%~1"=="run" if "%~2"=="" (
  "%REAL_NPM%" run dev
  exit /b %ERRORLEVEL%
)

:: Otherwise forward all arguments directly to real npm
"%REAL_NPM%" %*
exit /b %ERRORLEVEL%
