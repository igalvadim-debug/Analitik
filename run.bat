@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "PYTHON=%~dp0immovenv\Scripts\python.exe"

echo.
echo   ==================================================
echo     ZVG-Portal Downloader - NRW Courts
echo   ==================================================
echo.
echo    [1]  Aachen            [14] Gelsenkirchen     [27] Moenchengladbach
echo    [2]  Arnsberg           [15] Guetersloh        [28] Muenster
echo    [3]  Bielefeld          [16] Hagen             [29] Neuss
echo    [4]  Bochum             [17] Hamm              [30] Paderborn
echo    [5]  Bonn               [18] Hattingen         [31] Recklinghausen
echo    [6]  Borken             [19] Herford           [32] Siegburg
echo    [7]  Bottrop            [20] Iserlohn          [33] Siegen
echo    [8]  Detmold            [21] Kleve             [34] Soest
echo    [9]  Dortmund           [22] Koeln             [35] Solingen
echo   [10]  Duesseldorf        [23] Krefeld           [36] Unna
echo   [11]  Duisburg           [24] Lemgo             [37] Viersen
echo   [12]  Essen              [25] Lippstadt         [38] Wuppertal
echo   [13]  Euskirchen         [26] Luedenscheid
echo.
echo   --------------------------------------------------
echo    [A]  ALL 38 courts
echo    [D]  Duesseldorf + Koeln (default)
echo    [0]  Exit
echo   --------------------------------------------------
echo.
echo    Enter numbers separated by spaces: 1 5 22
echo.

set /p CHOICE="> Choice: "

if "%CHOICE%"=="" goto :default
if "%CHOICE%"=="0" goto :eof
if /i "%CHOICE%"=="A" goto :alle
if /i "%CHOICE%"=="D" goto :default

set "GERICHTE="
for %%i in (%CHOICE%) do (
    if "%%i"=="1"  set "GERICHTE=!GERICHTE! Aachen"
    if "%%i"=="2"  set "GERICHTE=!GERICHTE! Arnsberg"
    if "%%i"=="3"  set "GERICHTE=!GERICHTE! Bielefeld"
    if "%%i"=="4"  set "GERICHTE=!GERICHTE! Bochum"
    if "%%i"=="5"  set "GERICHTE=!GERICHTE! Bonn"
    if "%%i"=="6"  set "GERICHTE=!GERICHTE! Borken"
    if "%%i"=="7"  set "GERICHTE=!GERICHTE! Bottrop"
    if "%%i"=="8"  set "GERICHTE=!GERICHTE! Detmold"
    if "%%i"=="9"  set "GERICHTE=!GERICHTE! Dortmund"
    if "%%i"=="10" set "GERICHTE=!GERICHTE! Duesseldorf"
    if "%%i"=="11" set "GERICHTE=!GERICHTE! Duisburg"
    if "%%i"=="12" set "GERICHTE=!GERICHTE! Essen"
    if "%%i"=="13" set "GERICHTE=!GERICHTE! Euskirchen"
    if "%%i"=="14" set "GERICHTE=!GERICHTE! Gelsenkirchen"
    if "%%i"=="15" set "GERICHTE=!GERICHTE! Guetersloh"
    if "%%i"=="16" set "GERICHTE=!GERICHTE! Hagen"
    if "%%i"=="17" set "GERICHTE=!GERICHTE! Hamm"
    if "%%i"=="18" set "GERICHTE=!GERICHTE! Hattingen"
    if "%%i"=="19" set "GERICHTE=!GERICHTE! Herford"
    if "%%i"=="20" set "GERICHTE=!GERICHTE! Iserlohn"
    if "%%i"=="21" set "GERICHTE=!GERICHTE! Kleve"
    if "%%i"=="22" set "GERICHTE=!GERICHTE! Koeln"
    if "%%i"=="23" set "GERICHTE=!GERICHTE! Krefeld"
    if "%%i"=="24" set "GERICHTE=!GERICHTE! Lemgo"
    if "%%i"=="25" set "GERICHTE=!GERICHTE! Lippstadt"
    if "%%i"=="26" set "GERICHTE=!GERICHTE! Luedenscheid"
    if "%%i"=="27" set "GERICHTE=!GERICHTE! Moenchengladbach"
    if "%%i"=="28" set "GERICHTE=!GERICHTE! Muenster"
    if "%%i"=="29" set "GERICHTE=!GERICHTE! Neuss"
    if "%%i"=="30" set "GERICHTE=!GERICHTE! Paderborn"
    if "%%i"=="31" set "GERICHTE=!GERICHTE! Recklinghausen"
    if "%%i"=="32" set "GERICHTE=!GERICHTE! Siegburg"
    if "%%i"=="33" set "GERICHTE=!GERICHTE! Siegen"
    if "%%i"=="34" set "GERICHTE=!GERICHTE! Soest"
    if "%%i"=="35" set "GERICHTE=!GERICHTE! Solingen"
    if "%%i"=="36" set "GERICHTE=!GERICHTE! Unna"
    if "%%i"=="37" set "GERICHTE=!GERICHTE! Viersen"
    if "%%i"=="38" set "GERICHTE=!GERICHTE! Wuppertal"
)

if "%GERICHTE%"=="" (
    echo Invalid choice.
    pause
    goto :eof
)

echo.
echo.
echo [1/2] Download PDFs...
%PYTHON% immo.py%GERICHTE%
if errorlevel 1 (
    echo FEHLER: immo.py abgebrochen
    pause
    goto :eof
)
echo.
echo [2/2] PDF -> Markdown...
%PYTHON% pdf_to_md.py zvg_duss_koeln
echo.
echo Fertig! MD-Dateien in zvg_duss_koeln\md\
goto :eof

:alle
echo.
echo Running ALL 38 courts...
%PYTHON% immo.py Aachen Arnsberg Bielefeld Bochum Bonn Borken Bottrop Detmold Dortmund Duesseldorf Duisburg Essen Euskirchen Gelsenkirchen Guetersloh Hagen Hamm Hattingen Herford Iserlohn Kleve Koeln Krefeld Lemgo Lippstadt Luedenscheid Moenchengladbach Muenster Neuss Paderborn Recklinghausen Siegburg Siegen Soest Solingen Unna Viersen Wuppertal
goto :eof

:default
echo.
echo Running: Duesseldorf + Koeln (default)
%PYTHON% immo.py Duesseldorf Koeln
goto :eof
