@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo Сборка SnapOCR...
python -m PyInstaller --noconfirm --clean build.spec
if errorlevel 1 (
    echo.
    echo Ошибка сборки. Проверьте вывод выше.
    pause
    exit /b 1
)

echo.
echo Готово: dist\SnapOCR.exe
pause
