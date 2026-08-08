@echo off
REM 云小圈质检 · MySQL 每日备份（mysqldump → backups\yunxiaoquan_qc_YYYYMMDD.sql）
REM 手动跑或用任务计划每日触发；保留最近 30 天由 forfiles 清理。
REM 需 mysqldump 在 PATH（MySQL 安装自带）。密码从参数或写死，建议按现场改。
setlocal
cd /d "%~dp0\.."

set DB=yunxiaoquan_qc
set DBUSER=root
set DBPASS=123456
set OUTDIR=%CD%\backups
if not exist "%OUTDIR%" mkdir "%OUTDIR%"

REM 取日期 YYYYMMDD（依赖区域设置；如不对请改用 wmic os get localdatetime）
for /f "tokens=1-3 delims=/- " %%a in ("%date%") do set DAY=%%a%%b%%c
set STAMP=%DAY:~0,8%
set FILE=%OUTDIR%\%DB%_%STAMP%.sql

echo [%date% %time%] dumping %DB% -> %FILE%
mysqldump -u %DBUSER% -p%DBPASS% --single-transaction --default-character-set=utf8mb4 %DB% > "%FILE%"
if errorlevel 1 (
  echo [ERROR] mysqldump 失败，检查 mysqldump 是否在 PATH、账号密码是否正确
) else (
  echo [OK] 备份完成：%FILE%
)

REM 清理 30 天前的备份
forfiles /p "%OUTDIR%" /m *.sql /d -30 /c "cmd /c del @path" 2>nul
endlocal
