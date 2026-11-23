@echo off
rem Length check: read five passages aloud, then see which clip length makes the fewest mistakes.
rem Double-click to run the test; "lengthcheck.cmd report" just prints the results so far.
cd /d "%~dp0"
if "%~1"=="" (python -m dictate.lengthcheck test) else (python -m dictate.lengthcheck %*)
pause
