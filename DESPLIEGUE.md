# Despliegue en el servidor definitivo

Servidor de producción: **10.50.210.22** (`SESRVMONITORING`), usuario `monitoring`.

Ahí conviven ya otros dos dashboards, así que todo lo de este va con nombres y
puerto propios:

| | Sensores | Radioenlaces | **DPA (este)** |
|---|---|---|---|
| Puerto | 8000 | 8080 | **9000** |
| Servicio | `dashboard-sensores` | `dashboard-radioenlaces` | `dashboard-dpa` |
| Carpeta | `/opt/dashboard-sensores` | `/opt/dashboard-radioenlaces` | `/opt/dashboard-dpa` |
| Usuario | propio | `dashboard-radio` | `dashboard-dpa` |

---

## Paso 0 — Comprobación previa (hazla ANTES de instalar nada)

El servidor de producción está en `10.50.210.0/24` y DPA en `10.0.0.136`. Son
segmentos distintos; que funcionara desde el servidor de pruebas
(`10.50.69.121`) **no garantiza** que funcione desde aquí.

```bash
ssh monitoring@10.50.210.22

# ¿llegamos a DPA?
curl -s -o /dev/null -w "http  %{http_code}\n"  --max-time 5 http://10.0.0.136:8123/iwc/login.iwc
curl -sk -o /dev/null -w "https %{http_code}\n" --max-time 5 https://10.0.0.136:8124/iwc/login.iwc

# ¿está libre el puerto 9000?
sudo ss -lntp | grep -E ':(8000|8080|9000)\s' || echo "9000 libre"
```

- Los dos `curl` deben dar **200**. Si dan `000`, hay cortafuegos por medio:
  usa el documento `Requerimiento_conectividad_dpa.md` para pedir el flujo a IT
  y **para aquí**.
- Si el 9000 aparece ocupado, cámbialo en `dpa.json` y en la regla de `ufw`.

---

## Paso 1 — Clonar y desplegar

```bash
cd ~
git clone https://github.com/mr7security/dpa-dashboard.git
cd dpa-dashboard
sudo bash deploy.sh
```

`deploy.sh` es idempotente: crea el usuario de servicio, instala en
`/opt/dashboard-dpa`, registra la unidad de systemd, la habilita para que
arranque sola tras un reinicio, y la lanza.

## Paso 2 — Configurar

```bash
sudo nano /opt/dashboard-dpa/dpa.json
```

```json
  "url": "https://10.0.0.136:8124",
  "usuario": "dashboard",
  "password": "LA_CONTRASENA",
  "verificar_ssl": false,
  "puerto": 9000,
  "intervalo_segundos": 30,
```

Usa **https por el 8124**. Con http la contraseña del usuario de DPA viaja en
claro por la red. Si el 8124 no fuera accesible, `http://10.0.0.136:8123`
funciona igual, pero es peor.

El usuario de DPA es el de **solo lectura** que creaste en Options → User
Administration. No uses `dpaADMIN`.

## Paso 3 — Probar antes de abrir el puerto

```bash
sudo -u dashboard-dpa python3 /opt/dashboard-dpa/dpa.py --once 2>&1 | head -30
```

Tiene que aparecer `sesion iniciada en DPA como 'dashboard'` y a continuación
el JSON con las 5 instancias. Si sale `login rechazado`, revisa las
credenciales; si sale un error de red, vuelve al paso 0.

## Paso 4 — Arrancar y abrir el puerto

```bash
sudo systemctl restart dashboard-dpa
sudo systemctl status dashboard-dpa --no-pager | head -12
sudo ufw allow 9000/tcp
```

## Paso 5 — Logo

Si el `logo.jpg` no viajó en el repo, cógelo del dashboard de sensores:

```bash
sudo cp /opt/dashboard-sensores/logo.jpg /opt/dashboard-dpa/logo.jpg
sudo chown dashboard-dpa:dashboard-dpa /opt/dashboard-dpa/logo.jpg
```

## Paso 6 — Verificar

Abre `http://10.50.210.22:9000/` y comprueba:

- [ ] Salen las 5 instancias con datos, no con guiones
- [ ] El logo aparece arriba a la izquierda
- [ ] Los indicadores de colores tienen sentido (compara con la pantalla principal de DPA)
- [ ] El Modo TV entra a pantalla completa y no desborda
- [ ] El pie dice "SGP · Departamento de IT"
- [ ] Reinicia el servidor y comprueba que el dashboard vuelve solo

---

## Comprobación cruzada contra la GUI de DPA

Antes de darlo por bueno, coteja **una instancia** valor a valor. Abre en DPA
la pantalla *Current* de esa instancia y compara con la tarjeta:

| Dashboard | Dónde mirarlo en DPA |
|---|---|
| Sesiones activas / bloqueadas | *Current* → recuadro Sessions |
| Espera de usuario (1 h) | *Resources* → Waits → Instance Wait Time |
| CPU, memoria, PLE, latencias | *Resources* → pestañas CPU / Memory / Disk |
| Backups y TempDB | *Resources* → pestañas Backups / TempDB |
| Availability Group | pestaña *AG Status* |
| Lista de bloqueos | *Current* → Show blocked sessions |

Ten en cuenta al comparar: las sesiones se muestrean **cada minuto** y la
espera **cada 5 minutos**, así que una diferencia pequeña entre el dashboard y
DPA en el mismo instante es normal, no un error.

---

## Mantenimiento

```bash
# actualizar tras un cambio
cd ~/dpa-dashboard && git pull && sudo bash deploy.sh

# ver el log
journalctl -u dashboard-dpa -f

# lectura suelta para diagnosticar
sudo -u dashboard-dpa python3 /opt/dashboard-dpa/dpa.py --once | sed -n '/^{/,$p'
```

`deploy.sh` nunca pisa `dpa.json`, así que la configuración y la contraseña
sobreviven a las actualizaciones.

## Qué hacer con el servidor de pruebas

En `10.50.69.121` quedó una instalación completa. Cuando producción esté
validada, o la paras para que no consulte a DPA por duplicado:

```bash
sudo systemctl disable --now dashboard-dpa
```

o la dejas como entorno de pruebas para futuros cambios, sabiendo que suma
carga a DPA.
