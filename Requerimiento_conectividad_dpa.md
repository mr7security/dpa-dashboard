# Petición de conectividad — Dashboard de bases de datos

**Solicitante:** Miguel Rebolledo — Departamento de IT
**Fecha:** 20/08/2026

## Motivo

Puesta en marcha de un panel de monitorización de las instancias SQL Server
que se muestra en pantalla en el departamento. El panel consulta en modo
**solo lectura** al servidor de SolarWinds DPA que ya tenemos en producción,
y no accede directamente a ninguna base de datos.

## Flujo solicitado

| Origen | Destino | Puerto | Protocolo | Sentido |
|---|---|---|---|---|
| 10.50.210.22 (`SESRVMONITORING`) | 10.0.0.136 (`srvdpa.central.local`) | 8124/tcp | HTTPS | Saliente, retorno stateful |

**Alternativa** si el 8124 no estuviera disponible:

| Origen | Destino | Puerto | Protocolo | Sentido |
|---|---|---|---|---|
| 10.50.210.22 (`SESRVMONITORING`) | 10.0.0.136 (`srvdpa.central.local`) | 8123/tcp | HTTP | Saliente, retorno stateful |

Se prefiere el 8124 porque va cifrado; por el 8123 las credenciales viajarían
en claro por la red interna.

## Detalle técnico

- El servidor origen ya aloja otros dos paneles equivalentes (sensores y
  radioenlaces), aprobados previamente.
- El acceso lo realiza un servicio desatendido con una cuenta de **solo
  lectura** creada expresamente en DPA, sin privilegios administrativos.
- Frecuencia: una consulta cada 30 segundos.
- No se requiere ningún flujo entrante hacia el servidor DPA distinto del
  indicado, ni acceso a las instancias SQL Server monitorizadas.
- El panel se publica en `10.50.210.22:9000/tcp`, accesible solo desde la red
  interna corporativa.

## Comprobación

Una vez aplicado, desde `10.50.210.22`:

```bash
curl -sk -o /dev/null -w "%{http_code}\n" --max-time 5 https://10.0.0.136:8124/iwc/login.iwc
```

Debe devolver `200`.
