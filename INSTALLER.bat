@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" python -m venv .venv
".venv\Scripts\python.exe" -m pip install -r requirements-desktop.txt
if errorlevel 1 goto error
echo Installation terminee. Lancez LANCER.bat.
echo Les modeles se telechargent depuis la section Modeles de Renkavi.
pause
exit /b 0
:error
echo Installation interrompue. Consultez le message ci-dessus.
pause
exit /b 1
