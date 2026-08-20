@echo off
REM Sube a GitHub los cambios de esta carpeta. Doble clic y listo.
REM Se ejecuta siempre en la carpeta donde esta el .bat, asi que da igual
REM desde donde se lance.
cd /d "%~dp0"

echo.
echo   Carpeta: %CD%
echo.
git status --short
echo.

set "MSG="
set /p MSG="Mensaje del commit (Enter para uno generico): "
if "%MSG%"=="" set "MSG=Actualizacion del dashboard"

git add -A
git commit -m "%MSG%"
if errorlevel 1 (
  echo.
  echo   No habia nada que subir, o el commit ha fallado.
)

echo.
git push
if errorlevel 1 (
  echo.
  echo   ERROR al subir. Revisa la conexion o las credenciales de GitHub.
) else (
  echo.
  echo   Subido. Ahora en la Ubuntu:
  echo     cd ~/dpa-dashboard ^&^& git pull ^&^& sudo bash deploy.sh
)

echo.
pause
