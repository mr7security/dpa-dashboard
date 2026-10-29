#!/usr/bin/env bash
# Instalador / actualizador del Dashboard de Bases de Datos (DPA).
# Idempotente: se puede lanzar tantas veces como haga falta.
set -euo pipefail

DESTINO="/opt/dashboard-dpa"
SERVICIO="dashboard-dpa"
USUARIO="dashboard-dpa"
APP="dpa.py"
WEB="dpa.html"
CONF="dpa.json"

verde(){ printf "\033[0;32m%s\033[0m\n" "$*"; }
amar(){ printf "\033[0;33m%s\033[0m\n" "$*"; }
rojo(){ printf "\033[0;31m%s\033[0m\n" "$*"; }

ORIGEN="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
  rojo "Ejecutalo como root:  sudo bash deploy.sh"
  exit 1
fi

if [[ "$ORIGEN" == "$DESTINO" ]]; then
  rojo "No lances deploy.sh desde dentro de $DESTINO. Clona el repo en tu HOME."
  exit 1
fi

for f in "$APP" "$WEB" "${CONF}.example" "${SERVICIO}.service"; do
  [[ -f "$ORIGEN/$f" ]] || { rojo "Falta $f en $ORIGEN"; exit 1; }
done

if ! command -v python3 >/dev/null 2>&1; then
  amar "Instalando python3..."
  apt-get update -qq && apt-get install -y -qq python3
fi

# pymssql solo hace falta si se activa el bloque "sql_directo" de la config:
# sirve para resolver el nombre de los jobs del Agent y leer los que estan en
# ejecucion, cosas que DPA no expone. Si no se puede instalar no pasa nada, el
# panel arranca igual sin esos extras.
if python3 -c "import pymssql" >/dev/null 2>&1; then
  verde "pymssql ya disponible (SQL directo soportado)"
else
  amar "Instalando pymssql para el SQL directo (opcional)..."
  if ! python3 -m pip --version >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y -qq python3-pip || true
  fi
  # --break-system-packages hace falta en Debian 12 / Ubuntu 24 (PEP 668)
  python3 -m pip install --quiet pymssql --break-system-packages 2>/dev/null \
    || python3 -m pip install --quiet pymssql 2>/dev/null \
    || apt-get install -y -qq python3-pymssql 2>/dev/null \
    || amar "No he podido instalar pymssql. El panel funcionara igual, pero
         sin nombres de job ni jobs en ejecucion. Para activarlo luego:
           sudo python3 -m pip install pymssql --break-system-packages"
fi

if ! id "$USUARIO" >/dev/null 2>&1; then
  verde "Creando usuario de servicio $USUARIO"
  useradd --system --no-create-home --shell /usr/sbin/nologin "$USUARIO"
fi

mkdir -p "$DESTINO"
install -m 644 "$ORIGEN/$APP" "$DESTINO/$APP"
install -m 644 "$ORIGEN/$WEB" "$DESTINO/$WEB"
install -m 644 "$ORIGEN/${CONF}.example" "$DESTINO/${CONF}.example"

for opcional in README.md ejemplo_respuesta.json logo.png logo.svg logo.jpg logo.jpeg logo.gif; do
  [[ -f "$ORIGEN/$opcional" ]] && install -m 644 "$ORIGEN/$opcional" "$DESTINO/$opcional"
done

if [[ -f "$DESTINO/$CONF" ]]; then
  amar "$CONF ya existe: se conserva tu configuracion"
else
  verde "Creando $CONF desde la plantilla — EDITALO con usuario y contrasena de DPA"
  install -m 640 "$ORIGEN/${CONF}.example" "$DESTINO/$CONF"
fi

chown -R "$USUARIO":"$USUARIO" "$DESTINO"
chmod 640 "$DESTINO/$CONF"

install -m 644 "$ORIGEN/${SERVICIO}.service" "/etc/systemd/system/${SERVICIO}.service"
systemctl daemon-reload
systemctl enable "$SERVICIO" >/dev/null 2>&1 || true
systemctl restart "$SERVICIO"

PUERTO=$(python3 - "$DESTINO/$CONF" <<'PY'
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("puerto", 9000))
except Exception:
    print(9000)
PY
)

sleep 3
if systemctl is-active --quiet "$SERVICIO"; then
  IP=$(hostname -I | awk '{print $1}')
  verde "Servicio activo."
  verde "Dashboard:  http://${IP}:${PUERTO}/"
else
  rojo "El servicio NO ha arrancado. Mira el log:"
  rojo "  journalctl -u ${SERVICIO} -n 50 --no-pager"
  exit 1
fi

cat <<EOF

Comandos utiles
---------------
  journalctl -u ${SERVICIO} -f          ver el log en vivo
  systemctl status ${SERVICIO}          estado del servicio
  systemctl restart ${SERVICIO}         reiniciar
  sudo nano ${DESTINO}/${CONF}          editar configuracion
  sudo -u ${USUARIO} python3 ${DESTINO}/${APP} --once | sed -n '/^{/,\$p'
                                        una lectura suelta en JSON
EOF
