@echo off
cd /d "%~dp0"
rem English (EN) instance — separate language, data folder, and port from the Korean one.
set KAKEIBO_LANG=en
set KAKEIBO_HOME=%USERPROFILE%\.kakeibo-en
set FLASK_PORT=5001
python -m kakeibo
