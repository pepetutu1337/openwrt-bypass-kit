@echo off
rem ============================================================
rem  netgui-web.cmd - localnaya veb-panel kita (Windows 10/11).
rem  Podnimaet Python-server (tolko stdlib) i otkryvaet brauzer.
rem  Nuzhen Python 3 (python.org / Microsoft Store) i ssh (est v Win10/11).
rem ============================================================
setlocal
set "KIT_DIR=%~dp0.."
set "SERVER=%KIT_DIR%\webgui\server.py"

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%SERVER%" %*
  goto :eof
)
where python >nul 2>nul
if %errorlevel%==0 (
  python "%SERVER%" %*
  goto :eof
)
echo Nuzhen Python 3. Ustanovi s https://www.python.org/ (galochka "Add to PATH")
echo ili iz Microsoft Store: "Python 3".
pause
