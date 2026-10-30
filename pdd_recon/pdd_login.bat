@echo off
chcp 65001 >nul
title 拼多多登录态（一次性）
cd /d "%~dp0"

set PY=D:\python\python.exe
if not exist "%PY%" set PY=python

echo ============================================
echo  打开拼多多登录窗口，扫码/短信登录一次
echo  登录态保存在 pdd-profile\ ，之后复用
echo ============================================
echo.

"%PY%" pdd_crawler.py login

echo.
"%PY%" pdd_crawler.py check
echo.
pause
