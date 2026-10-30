@echo off
chcp 65001 >nul
title 拼多多榜单页结构探针
cd /d "%~dp0"

set PY=D:\python\python.exe
if not exist "%PY%" set PY=python

if "%~1"=="" (
  echo 用法: probe_pdd.bat "https://mobile.yangkeduo.com/sjs_cat_rank_list.html?list_id=...&list_sn=..."
  pause
  exit /b 1
)

"%PY%" pdd_crawler.py probe "%~1"
echo.
pause
