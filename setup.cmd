@echo off
setlocal DisableDelayedExpansion
set "ROOT=%~dp0"
set "PYTHONUTF8=1"

py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 goto run_py

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 goto run_python

>&2 echo Error: Python 3.11 or newer was not found. Install it, then run setup.cmd again.
exit /b 2

:run_py
py -3 "%ROOT%scripts\setup.py" %*
exit /b %errorlevel%

:run_python
python "%ROOT%scripts\setup.py" %*
exit /b %errorlevel%
