@echo off
setlocal DisableDelayedExpansion
set "ROOT=%~dp0"
set "PYTHONUTF8=1"
set "PYTHON=%ROOT%.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  >&2 echo Error: SourceLedger is not installed. Run setup.cmd first.
  exit /b 2
)
"%PYTHON%" "%ROOT%scripts\launch.py" %*
exit /b %errorlevel%
