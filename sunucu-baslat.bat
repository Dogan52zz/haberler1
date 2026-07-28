@echo off
cd /d "%~dp0"
start "Haber Sunucusu" cmd /k python server.py
timeout /t 2 /nobreak >nul
start http://localhost:8080/haber-akisi.html.html
