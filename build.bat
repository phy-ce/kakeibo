@echo off
cd /d "%~dp0"
pyinstaller --clean --onefile --name kakeibo ^
  --add-data "kakeibo/templates;kakeibo/templates" ^
  --add-data "kakeibo/static;kakeibo/static" ^
  --hidden-import "google.auth.transport.requests" ^
  --hidden-import "google.genai" ^
  kakeibo/__main__.py
echo.
echo Build complete: dist\kakeibo.exe
