@echo off
REM ============================================================
REM  netctl.cmd - управление китом с Windows (10/11, есть OpenSSH из коробки).
REM  Делает то же, что bin/netctl для Linux/Mac.
REM ============================================================
setlocal enabledelayedexpansion

set "KIT_DIR=%~dp0.."
set "CONF=%KIT_DIR%\config\kit.conf"
if not exist "%CONF%" ( echo Нет %CONF% - скопируй из репозитория и правь под себя. & exit /b 1 )

REM --- выдёргиваем нужные значения из kit.conf ---
for /f "usebackq tokens=1,* delims==" %%A in ("%CONF%") do (
  set "k=%%A" & set "v=%%B"
  set "k=!k: =!"
  if /i "!k!"=="ROUTER_IP"        call :setval ROUTER_IP     !v!
  if /i "!k!"=="ROUTER_SSH_USER"  call :setval SSH_USER      !v!
  if /i "!k!"=="ROUTER_SSH_PORT"  call :setval SSH_PORT      !v!
  if /i "!k!"=="ROUTER_SSH_KEY"   call :setval SSH_KEY       !v!
)
if "%SSH_PORT%"=="" set "SSH_PORT=22"
set "KEYOPT="
if not "%SSH_KEY%"=="" set "KEYOPT=-i %SSH_KEY%"
set "SSH=ssh %KEYOPT% -p %SSH_PORT% -o ConnectTimeout=8 %SSH_USER%@%ROUTER_IP%"
set "SCP=scp %KEYOPT% -P %SSH_PORT% -q"
set "DEST=%SSH_USER%@%ROUTER_IP%"

set "CMD=%1"
if "%CMD%"=="" set "CMD=help"
shift
set "ARGS=%1 %2 %3 %4 %5 %6 %7 %8 %9"

if /i "%CMD%"=="install" ( call :push && %SSH% "netkit install" & goto :eof )
if /i "%CMD%"=="apply"   ( call :push && %SSH% "netkit apply"   & goto :eof )
if /i "%CMD%"=="push"    ( call :push & goto :eof )
if /i "%CMD%"=="pull"    ( call :pull & goto :eof )
if /i "%CMD%"=="status"  ( %SSH% "netkit status"          & goto :eof )
if /i "%CMD%"=="doctor"  ( %SSH% "netkit doctor %ARGS%"   & goto :eof )
if /i "%CMD%"=="info"    ( %SSH% "netkit info"            & goto :eof )
if /i "%CMD%"=="meta"    ( %SSH% "netkit meta"            & goto :eof )
if /i "%CMD%"=="logs"    ( %SSH% "netkit logs %ARGS%"     & goto :eof )
if /i "%CMD%"=="domains" ( %SSH% "netkit domains %ARGS%"  & goto :eof )
if /i "%CMD%"=="nodes"   ( %SSH% "netkit nodes %ARGS%"    & goto :eof )
if /i "%CMD%"=="zapret"  ( %SSH% "netkit zapret %ARGS%"   & goto :eof )
if /i "%CMD%"=="proxy"   ( %SSH% "netkit proxy %ARGS%"    & goto :eof )
if /i "%CMD%"=="geoblock" ( %SSH% "netkit geoblock %ARGS%" & goto :eof )
if /i "%CMD%"=="ssh"     ( %SSH% & goto :eof )

echo netctl - пульт к OpenWrt-киту (Windows).
echo.
echo   netctl install            первичная установка на чистый OpenWrt
echo   netctl apply              залить конфиг и перезапустить сервисы
echo   netctl status             состояние сервисов
echo   netctl doctor [домен]     полный чекап
echo   netctl domains list ^| add D... ^| rm D...
echo   netctl nodes   list ^| test ^| use N
echo   netctl zapret  on ^| off ^| check
echo   netctl proxy   on ^| off
echo   netctl pull ^| push ^| ssh ^| logs
goto :eof

:setval
set "%1=%2"
goto :eof

:push
echo :: заливаю конфиг на роутер (%ROUTER_IP%)...
%SSH% "mkdir -p /etc/netkit"
%SCP% "%KIT_DIR%\config\kit.conf" "%KIT_DIR%\config\nodes.list" "%KIT_DIR%\config\domains.list" "%KIT_DIR%\config\ru-split.list" "%KIT_DIR%\config\telegram-cidr.list" "%KIT_DIR%\config\zapret.conf" %DEST%:/etc/netkit/
%SCP% "%KIT_DIR%\router\netkit" %DEST%:/usr/sbin/netkit
%SSH% "chmod +x /usr/sbin/netkit"
goto :eof

:pull
for %%f in (nodes.list domains.list ru-split.list telegram-cidr.list zapret.conf) do (
  %SCP% %DEST%:/etc/netkit/%%f "%KIT_DIR%\config\%%f" && echo   ^<- %%f
)
goto :eof
