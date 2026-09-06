@echo off
chcp 65001 >nul
setlocal
rem 本地网页一键重启:结束旧进程 → 启动新进程 → 就绪后自动打开页面
rem 用法: restart_web.bat [端口]   (默认 8000;双击运行即用默认端口)
rem 说明: 代码更新后必须重启进程才会生效(uvicorn 启动时加载代码)

set PORT=%~1
if "%PORT%"=="" set PORT=8000
cd /d "%~dp0"

echo [1/3] 检查端口 %PORT% 上的旧进程...
set FOUND=0
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%PORT% " ^| findstr "LISTENING"') do (
    set FOUND=1
    echo     结束旧进程 PID=%%P
    taskkill /PID %%P /F >nul 2>&1
)
if "%FOUND%"=="0" echo     没有正在运行的旧进程
if "%FOUND%"=="1" timeout /t 2 /nobreak >nul

echo [2/3] 启动 uvicorn(端口 %PORT%,日志在新开的窗口里)...
start "GitHub趋势榜知识库 (端口 %PORT%)" cmd /k python -m uvicorn web.app:app --host 127.0.0.1 --port %PORT%

echo [3/3] 等待服务就绪(最多约 20 秒)...
powershell -NoProfile -Command "$ok=$false; foreach($i in 1..10){ try { $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:%PORT%/healthz' -TimeoutSec 3; if ($r.StatusCode -eq 200) { $ok = $true; break } } catch { Start-Sleep -Seconds 2 } }; if ($ok) { exit 0 } else { exit 1 }"
if errorlevel 1 (
    echo     警告: 服务未在预期时间内就绪,请查看服务窗口里的报错日志。
) else (
    echo     服务已就绪,正在打开浏览器...
    start "" http://127.0.0.1:%PORT%/
)
echo 完成。
endlocal
