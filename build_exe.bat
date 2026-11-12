@echo off
rem Build a standalone .exe. Internet access is required to install PyInstaller.
rem Output: dist\AnimaTranslator.exe
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  set "PYTHON=python"
) else (
  set "PYTHON=py -3"
)

%PYTHON% -c "import PyInstaller" 2>nul || (
  %PYTHON% -m pip --version >nul 2>nul || (
    %PYTHON% -m ensurepip --upgrade
    %PYTHON% -m pip --version >nul 2>nul || (
      echo ERROR: pip is not available for the selected Python interpreter.
      goto :failed
    )
  )
  echo Installing PyInstaller...
  %PYTHON% -m pip install --user --upgrade pyinstaller || goto :failed
)

%PYTHON% -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name AnimaTranslator ^
  --add-data "prompts.py;." ^
  --add-data "tags;tags" ^
  --add-data "anima;anima" ^
  AnimaTranslator.pyw || goto :failed

echo.
echo Built: %cd%\dist\AnimaTranslator.exe
goto :end

:failed
echo.
echo Build failed. Review the messages above.

:end
pause
