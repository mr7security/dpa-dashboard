# Dashboard de Bases de Datos — SolarWinds DPA

Panel en pantalla (PC y televisor) del estado de las instancias SQL Server
monitorizadas por SolarWinds DPA. Mismo esquema que el dashboard de
radioenlaces: un único servicio Python sin dependencias externas, un HTML sin
CDN, vista normal detallada y Modo TV.

## De dónde salen los datos

DPA tiene **dos** interfaces HTTP y hacen cosas distintas:

| | API pública (`/iwc/api`) | REST interna (`/iwc/rest`) |
|---|---|---|
| Autenticación | token (refresh → access) | cookie de sesión web |
| Documentada | sí (Swagger en `/iwc/api-doc`) | no |
| Contenido | administración: registrar instancias, monitores, alertas, licencias | **datos de rendimiento** |

La API pública **no expone ni una métrica de rendimiento**. Todo lo que pinta
este dashboard viene de la REST interna, a la que el colector accede
iniciando sesión con usuario y contraseña, igual que un navegador.

### Login

```
GET  /iwc/login.iwc   -> cookie JSESSIONID + token _csrf del formulario
POST /iwc/login.iwc   -> user, password, useCentral=false, _csrf
```

Si la sesión caduca, DPA devuelve HTML en vez de JSON; el cliente lo detecta,
vuelve a loguearse y reintenta la petición una sola vez.

### Endpoints usados

| Endpoint | Para qué |
|---|---|
| `GET /iwc/rest/databases` | inventario: id, nombre, tipo, estado del monitor, AG |
| `GET /iwc/rest/databases/{id}/super-tab` | nivel de Trends, Tuning, salud del AG y Recursos |
| `GET /iwc/rest/databases/{id}/metrics/categories` | `alarmLevel` por categoría (el semáforo de DPA) |
| `GET /iwc/rest/databases/{id}/metrics/data` | series `{time, value}` de las métricas |
| `GET /iwc/rest/ag-status/summary` | Availability Group: clúster, salud, réplica primaria, rol, nº de réplicas y bases, modo de conmutación |

### La excepción: la lista de sesiones bloqueadas

El detalle de los bloqueos **no existe en ninguna API JSON**. La pantalla
*Current* lo saca de una página HTML normal:

```
GET /iwc/realTimeTab.iwc?db_id=<id>&action=4&cs=1
```

Devuelve la tabla de la pestaña *Blocking* con las columnas crudas de
`sysprocesses`: `spid` (marcado con `(blocker)` o `(waiter)`), `loginame`,
`program_name`, `hostname`, `db_name`, `lastwaittype`, `cmd`, `waittime` (ms),
`status`. El colector la parsea con `html.parser`.

Esta es **la parte frágil del proyecto**: si SolarWinds cambia esa página en
una actualización, deja de parsear. Por eso:

- Solo se pide cuando DPA ya ha dicho que hay sesiones bloqueadas.
- Va en su propio `try`: si falla, se registra en el log, la tarjeta sigue
  viva y simplemente no aparece la lista.

Categorías con `alarmLevel`: CPU, Memory, Disk, Sessions, Waits, TempDB,
Backups, Network, License Compliance. Valores: `NORMAL`, `WARNING`,
`CRITICAL`, `UNKNOWN`.

### Métricas que se piden

Todas en una sola llamada por instancia, ventana de 60 minutos:

`Active Sessions`, `Blocked Sessions`, `Instance Wait Time`,
`CPU Utilization`, `Instance CPU Utilization`, `Signal Waits Percent`,
`Memory Utilization`, `Page Life Expectancy`, `Buffer Cache Hit Ratio`,
`SQL Disk Read Latency`, `SQL Disk Write Latency`,
`Batch Requests Per Sec`, `Transaction Rate`, `Round-trip Time (ms)`.

Granularidades reales de DPA: sesiones cada **60 s**, tiempo de espera cada
**300 s**. Sondear más rápido no da más resolución.

### Sin base de datos propia

A diferencia del dashboard de radioenlaces, aquí **no hay SQLite**. DPA ya
guarda el histórico, así que `/api/historico` es una pasarela a
`metrics/data` con caché de 55 s. Ventaja: las gráficas de 24 h salen
llenas desde el primer minuto, sin periodo de "recopilando histórico".

## Lo que se ve

Por instancia, en grande:

- **Sesiones** — activas y bloqueadas, lado a lado, con el pico de la última hora debajo.
- **Espera de usuario (1 h)** — suma de `Instance Wait Time`, que es el total
  que agrega el gráfico de barras de la pantalla *Current* de DPA.

En la vista normal, además: CPU del servidor y de la instancia, signal waits,
memoria, page life expectancy, buffer cache hit, latencias de disco, batch
requests, transacciones, round-trip, las etiquetas de las 9 categorías de
alarma y la lista de avisos.

### Las tres vistas

| Vista | Para qué | Qué muestra |
|---|---|---|
| **Normal** | mirar desde el PC | un servidor por fila, a todo el ancho, con las tres zonas: métricas y medidores, detalle y AG, gráficas y disco. Debajo, bloqueos y sesiones activas |
| **Modo TV** | pantalla de sala | las cinco instancias a la vez, solo sesiones e indicadores, y un panel de bloqueos abajo que ocupa el hueco restante |
| **Presentación** | rotación desatendida | una instancia cada vez a pantalla completa con todo el detalle, cambiando cada 15 s (30 s si está en crítico) |

En Presentación: flechas para moverse, espacio para pausar, Escape para salir,
y clic en cualquier nombre de la tira superior para saltar a esa instancia.

### Semáforo

| Nivel | Cuándo |
|---|---|
| `caido` | monitor de DPA parado, o sin respuesta |
| `critico` | alguna categoría en `CRITICAL`, Tuning/AG en `CRITICAL`, o ≥5 sesiones bloqueadas |
| `aviso` | alguna categoría en `WARNING`, o 1-4 sesiones bloqueadas |
| `ok` | resto |

Los umbrales de las categorías son los de DPA, no inventados aquí.

## Instalación

```bash
git clone https://github.com/mr7security/dpa-dashboard.git ~/dpa-dashboard
cd ~/dpa-dashboard
sudo bash deploy.sh
sudo nano /opt/dashboard-dpa/dpa.json     # usuario y contrasena de DPA
sudo systemctl restart dashboard-dpa
```

- Carpeta: `/opt/dashboard-dpa`
- Usuario de servicio: `dashboard-dpa` (sin shell, sin home)
- Puerto: **9000** (el 8000 y el 8080 ya están ocupados en ese servidor)
- Firewall: `sudo ufw allow 9000/tcp`

El servicio corre sin privilegios, así que el puerto tiene que ser ≥1024.

## Actualizar

```bash
cd ~/dpa-dashboard && git pull && sudo bash deploy.sh
```

Repo: <https://github.com/mr7security/dpa-dashboard>

`deploy.sh` **no pisa** `dpa.json`. En el navegador, Ctrl+F5.

## Seguridad

- `dpa.json` va con modo 640 y está en `.gitignore`. **Nunca** al repo.
- Usa un usuario de DPA dedicado y de solo lectura, no `dpaADMIN`.
- Si el repo es público, ojo: el README y la config llevan nombres de host internos.

## API propia

| Ruta | Devuelve |
|---|---|
| `/` | el dashboard |
| `/logo` | el logo corporativo, si existe un `logo.*` junto al `.py` |
| `/api/estado` | instantánea completa en JSON |
| `/api/historico?db=<id>&metrica=<nombre>&horas=<n>` | serie histórica |

Solo se aceptan métricas de la lista blanca del colector.

## Diagnóstico

```bash
journalctl -u dashboard-dpa -f
sudo -u dashboard-dpa python3 /opt/dashboard-dpa/dpa.py --once | sed -n '/^{/,$p'
```

`--once` mezcla los logs con el JSON por eso el `sed`.
