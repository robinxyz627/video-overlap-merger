@echo off
setlocal
set PYTHONUTF8=1
where pythonw >nul 2>nul
if errorlevel 1 goto try_python
start "" pythonw "%~dp0merge_gui.py" %*
exit /b 0

:try_python
where python >nul 2>nul
if errorlevel 1 goto no_python
start "" python "%~dp0merge_gui.py" %*
exit /b 0

:no_python
echo Python 3.11+ was not found in PATH.
pause
exit /b 1
