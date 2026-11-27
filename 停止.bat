@echo off
chcp 65001 >nul
setlocal

echo ==========================================================
echo   Contract Review System - stopping
echo ==========================================================
echo.

echo Killing backend (port 8210) ...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8210" ^| findstr "LISTENING"') do (
  taskkill /PID %%a /F >nul 2>nul
  echo   killed PID %%a
)

echo Killing frontend (port 3000) ...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":3000" ^| findstr "LISTENING"') do (
  taskkill /PID %%a /F >nul 2>nul
  echo   killed PID %%a
)

echo.
echo Done.
timeout /t 3 /nobreak >nul
endlocal
