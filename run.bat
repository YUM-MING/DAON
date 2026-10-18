@echo off
REM DAON integrated server - Windows launcher
REM   run.bat               phone camera/mic (config.yaml)
REM   run.bat --tunnel      + Cloudflare tunnel -> public https URL (phone camera needs https)
REM   run.bat --mock        no sensor workers, fake events
REM   other args are passed to server.main (e.g. --port 8000)
setlocal EnableDelayedExpansion
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if not exist .venv\Scripts\python.exe (
  echo [run] .venv not found. Create it first:
  echo    py -3.12 -m venv .venv ^&^& .venv\Scripts\activate ^&^& pip install -r requirements.txt
  exit /b 1
)
call .venv\Scripts\activate.bat

set TUNNEL=0
set PORT=8000
set ARGS=
:parse
if "%~1"=="" goto run
if "%~1"=="--tunnel" ( set TUNNEL=1 ) else if "%~1"=="--port" ( set ARGS=!ARGS! --port %~2& set PORT=%~2& shift ) else ( set ARGS=!ARGS! %~1 )
shift
goto parse

:run
if not exist logs mkdir logs
if "%TUNNEL%"=="1" (
  where cloudflared >nul 2>nul
  if errorlevel 1 (
    echo [run] cloudflared not found. Install:  winget install Cloudflare.cloudflared   ^(then open a new terminal^)
    exit /b 1
  )
  start "DAON tunnel" /min cmd /c "cloudflared tunnel --url http://localhost:%PORT% --no-autoupdate > logs\tunnel.log 2>&1"
  echo [run] opening tunnel... the public URL will be printed below in a few seconds
  set URL=
  for /l %%i in (1,1,30) do (
    if "!URL!"=="" (
      timeout /t 1 >nul
      for /f "tokens=*" %%u in ('findstr /r "https://[a-z0-9-]*\.trycloudflare\.com" logs\tunnel.log 2^>nul') do (
        for %%w in (%%u) do ( echo %%w | findstr /r "^https://.*trycloudflare\.com" >nul && set URL=%%w )
      )
    )
  )
  echo ==================================================================
  if "!URL!"=="" (
    echo   tunnel URL not found yet - check logs\tunnel.log
  ) else (
    echo   PUBLIC URL :  !URL!
    echo   robot phone:  !URL!/   -^> user mode -^> enter home code
    echo   guardian   :  !URL!/   -^> guardian mode -^> login ^(demo: demo@daon.local / daon1234, home code 123456^)
  )
  echo ==================================================================
)
python -m server.main %ARGS%
endlocal
