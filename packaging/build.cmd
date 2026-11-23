@echo off
rem Build the dictate installer: PyInstaller onedir, then Inno Setup.
rem Needs: Python 3.12 with requirements.txt + pyinstaller installed (set PY to
rem pick one, default "python"), and Inno Setup 6 (ISCC.exe).
rem Output: build\installer\dictate-setup-<version>.exe
setlocal
cd /d "%~dp0.."

if not defined PY set "PY=python"

for /f "delims=" %%v in ('%PY% -c "import dictate; print(dictate.__version__)"') do set "VER=%%v"
if not defined VER (echo Could not read the version from dictate\__init__.py & exit /b 1)

%PY% -m PyInstaller packaging\dictate.spec --noconfirm --distpath build\dist --workpath build\work || exit /b 1

set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (echo Inno Setup 6 not found & exit /b 1)

"%ISCC%" /Qp /DAppVersion=%VER% packaging\dictate.iss || exit /b 1
echo Built build\installer\dictate-setup-%VER%.exe
endlocal
