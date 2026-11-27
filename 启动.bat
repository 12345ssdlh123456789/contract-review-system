@echo off
chcp 65001 >nul
setlocal

set ROOT=%~dp0
set BACKEND=%ROOT%backend
set FRONTEND=%ROOT%frontend

echo ==========================================================
echo   Contract Review System - starting
echo ==========================================================
echo.

echo [1/3] Checking Python ...
where python >nul 2>nul
if errorlevel 1 (
  echo   ERROR: python not found in PATH.
  echo   Install Python 3.10+ and add it to PATH.
  pause
  exit /b 1
)
python -c "import fastapi, uvicorn, sqlalchemy" >nul 2>nul
if errorlevel 1 (
  echo   Missing dependencies. Installing from requirements.txt ...
  python -m pip install -r "%BACKEND%\requirements.txt"
  if errorlevel 1 (
    echo   ERROR: pip install failed.
    pause
    exit /b 1
  )
)
echo   OK
echo.

echo [2/3] Starting backend on http://127.0.0.1:8210 ...
start "ContractReview-Backend" /min cmd /c "cd /d "%BACKEND%" && python -m uvicorn app.main:app --host 127.0.0.1 --port 8210"

echo   Waiting for backend to become ready ...
set /a WAIT=0
:WAIT_LOOP
timeout /t 1 /nobreak >nul
set /a WAIT+=1
python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8210/api/health',timeout=2).status==200 else 1)" >nul 2>nul
if not errorlevel 1 goto BACKEND_READY
if %WAIT% GEQ 30 (
  echo   WARNING: backend not responding after 30s. Check the backend window for errors.
  goto BACKEND_READY
)
goto WAIT_LOOP
:BACKEND_READY
echo   Backend ready
echo.

echo [3/3] Starting frontend on http://127.0.0.1:3000 ...
start "ContractReview-Frontend" /min cmd /c "cd /d "%FRONTEND%" && python -m http.server 3000 --bind 127.0.0.1"
timeout /t 2 /nobreak >nul
echo   Frontend ready
echo.

echo ==========================================================
echo   Open:  http://127.0.0.1:3000
echo   API:   http://127.0.0.1:8210/docs
echo.
echo   Demo accounts (password: demo123456)
echo     submitter  - business submitter
echo     reviewer   - legal reviewer
echo     finance    - finance (payment clauses only)
echo     admin      - administrator
echo.
echo   To stop: run the stop script, or close the two minimized windows
echo ==========================================================
echo.

start "" http://127.0.0.1:3000
echo Done. This window can be closed.
timeout /t 5 /nobreak >nul
endlocal
