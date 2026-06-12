@echo off
cd /d "%~dp0"
set "PYTHON=%~dp0immovenv\Scripts\python.exe"

echo.
echo   ==================================================
echo     ZVG - Index.html Generator (DeepSeek API)
echo   ==================================================
echo.

echo [1/2] Dependencies...
%PYTHON% -m pip install requests PyMuPDF --quiet 2>nul

echo [2/2] Analyse + HTML + GitHub...
echo.

%PYTHON% build_index.py zvg_duss_koeln\md --upload

if errorlevel 1 (
    echo.
    echo FEHLER. DeepSeek-Key in .env?
    pause
    goto :eof
)

echo.
echo Fertig!
pause
