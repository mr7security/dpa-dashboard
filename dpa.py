#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dashboard de instancias SQL Server monitorizadas con SolarWinds DPA.

Servicio unico: colector (REST interna de DPA, con login de sesion) +
servidor HTTP que sirve el dashboard y su API.

Solo libreria estandar de Python 3.

Uso:
    python3 dpa.py                  arranca el servicio
    python3 dpa.py --once           una lectura y vuelca JSON por stdout
    python3 dpa.py --fixture f.json prueba el parser sin red
"""

import argparse
import http.cookiejar
import json
import logging
import mimetypes
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "dpa.json")
WEB_PATH = os.path.join(BASE_DIR, "dpa.html")
LOGOS = ("logo.png", "logo.svg", "logo.jpg", "logo.jpeg", "logo.gif")

USER_AGENT = "dashboard-dpa/1.0"

# Metricas que pedimos a DPA en una sola llamada por instancia.
# Nombre interno de DPA -> clave nuestra.
METRICAS = {
    "Active Sessions": "sesiones_activas",
    "Blocked Sessions": "sesiones_bloqueadas",
    "Instance Wait Time": "wait_time",
    "CPU Utilization": "cpu",
    "Instance CPU Utilization": "cpu_instancia",
    "Signal Waits Percent": "signal_waits",
    "Memory Utilization": "memoria",
    "Page Life Expectancy": "ple",
    "Buffer Cache Hit Ratio": "cache_hit",
    # Desglose de memoria: el porcentaje global dice poco. El PLE y las
    # recompilaciones son los que de verdad avisan, y el tamano de las caches
    # explica en que se esta yendo la memoria.
    "Buffer Cache Size": "cache_mb",
    "Plan Cache Size": "plan_cache_mb",
    "SQL Compilations Per Sec": "compilaciones_seg",
    "SQL Re-Compilations Per Sec": "recompilaciones_seg",

    # Hipervisor (vSphere). Solo llegan si la instancia esta vinculada a su
    # maquina virtual en DPA; si no, DPA devuelve la serie vacia y se ignoran.
    "vmReadyTime": "vm_ready",
    "vmPercentBallooned": "vm_balloon",
    "vmCpuUsagePercent": "vm_cpu",
    "hostCpuUsagePercent": "host_cpu",
    "hostMemoryUsage": "host_memoria",
    "SQL Disk Read Latency": "disco_lectura",
    "SQL Disk Write Latency": "disco_escritura",
    "Batch Requests Per Sec": "batch_seg",
    "Transaction Rate": "trans_seg",
    "Round-trip Time (ms)": "rtt",

    # Backups. Unidades verificadas contra la GUI de DPA (pestana Resources):
    #   sin_full -> DIAS, sin_log -> HORAS, logs_sin_archivar -> MB,
    #   backup_total -> GB, backups_activos -> numero de trabajos
    "Running Backups": "backups_activos",
    "Longest Time for a DB without a Successful Full Backup": "sin_full_dias",
    "Longest Time for a DB without a Successful Transaction Log Backup": "sin_log_horas",
    "Size of Transaction Logs Not Yet Archived": "logs_sin_archivar_mb",
    "Sum of All Backup Assets Required for Recovery of All DBs": "backup_total_gb",

    # TempDB. tempdb_libre -> MB, tempdb_log_libre -> MB, el pct -> %
    "TempDB Free Space": "tempdb_libre_mb",
    "TempDB Free Log Size": "tempdb_log_libre_mb",
    "TempDB Free Log Size Percentage": "tempdb_log_libre_pct",

    # Disco. cola -> operaciones de E/S de TODA la maquina (no solo de esta
    # instancia); lecturas y escrituras -> paginas por segundo
    "Disk Queue Length": "disco_cola",
    "Page Read per Second": "paginas_leidas_seg",
    "Page Write per Second": "paginas_escritas_seg",
}

# Categorias de alarma que devuelve DPA. El orden de pintado vive en
# dpa.html (CAT_INDICADORES y CAT_DETALLE); aqui solo traducimos los avisos.
CATEGORIA_ES = {
    "CPU": "CPU",
    "Memory": "Memoria",
    "Disk": "Disco",
    "Sessions": "Sesiones",
    "Waits": "Esperas",
    "TempDB": "TempDB",
    "Backups": "Backups",
    "Network": "Red",
    "License Compliance": "Licencias",
}

# Orden de severidad para consolidar
ORDEN_NIVEL = {"ok": 0, "aviso": 1, "critico": 2}

# Solo estas categorias pueden encender el ROJO cuando DPA las marca en
# CRITICAL. El resto (disco, tempdb, backups, licencias, esperas, sesiones)
# se queda en ambar por muy critico que lo pinte DPA: son cosas que mirar
# cuando se pueda, no motivo para saltar de la silla.
CATEGORIAS_ROJAS = ("CPU", "Memory", "Network")

# Cuantas sesiones listamos como maximo en una tarjeta.
MAX_BLOQUEOS = 8
MAX_SESIONES = 60

log = logging.getLogger("dpa")


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def ahora_utc():
    return datetime.now(timezone.utc)


def iso_z(dt):
    """DPA espera 2026-08-20T07:00:00.000Z"""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        "%03dZ" % (dt.microsecond // 1000)


def parsear_ts(txt):
    """'2026-08-20T09:01:21.000+02:00' -> epoch en segundos (float)."""
    if txt is None or txt == "":
        return None
    if isinstance(txt, (int, float)):
        # por si algun dia devuelve epoch en milisegundos
        return float(txt) / 1000.0 if txt > 1e11 else float(txt)
    t = str(txt).strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(t).timestamp()
    except ValueError:
        return None


def num(v):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def json_seguro(obj):
    """allow_nan=False: si algo viene NaN preferimos fallar aqui y no en el navegador."""
    return json.dumps(obj, allow_nan=False, ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------------------
# Cliente de la REST interna de DPA
# ---------------------------------------------------------------------------

class ErrorDPA(Exception):
    pass


class ClienteDPA(object):
    """
    Habla con la REST interna de DPA (/iwc/rest/...), que va con cookie de
    sesion web. Replica el login del formulario:
        GET  /iwc/login.iwc   -> cookie JSESSIONID + token _csrf
        POST /iwc/login.iwc   -> user, password, useCentral, _csrf
    """

    # Spring Security puede poner el token como <input> o <meta>, y con los
    # atributos en cualquier orden. Buscamos primero la etiqueta y luego el valor.
    RE_CSRF_TAG = re.compile(
        r'<(?:input|meta)\b[^>]*\b(?:name|id)\s*=\s*'
        r'(?:"_csrf"|\'_csrf\'|_csrf(?=[\s>]))[^>]*>', re.I)
    RE_CSRF_VAL = re.compile(
        r'\b(?:value|content)\s*=\s*["\']([^"\']*)["\']', re.I)

    def __init__(self, url, usuario, password, timeout=20, verificar_ssl=False):
        self.base = url.rstrip("/")
        self.usuario = usuario
        self.password = password
        self.timeout = timeout
        self._lock = threading.Lock()
        self._opener = None

        # DPA suele venir con certificado autofirmado. Si se usa https y no se
        # ha instalado un certificado de confianza, hay que relajar la
        # verificacion; el trafico sigue yendo cifrado, que es lo que importa
        # para que la contrasena no viaje en claro.
        if self.base.lower().startswith("https") and not verificar_ssl:
            self._ctx = ssl.create_default_context()
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE
            log.info("https sin verificar el certificado (autofirmado)")
        else:
            self._ctx = None

    # -- infraestructura -----------------------------------------------------

    def _nuevo_opener(self):
        cj = http.cookiejar.CookieJar()
        manejadores = [urllib.request.HTTPCookieProcessor(cj)]
        if self._ctx is not None:
            manejadores.append(urllib.request.HTTPSHandler(context=self._ctx))
        op = urllib.request.build_opener(*manejadores)
        op.addheaders = [("User-Agent", USER_AGENT)]
        return op

    def _abrir(self, url, datos=None, opener=None):
        op = opener or self._opener
        req = urllib.request.Request(url)
        if datos is not None:
            req.data = urllib.parse.urlencode(datos).encode("utf-8")
            req.add_header("Content-Type",
                           "application/x-www-form-urlencoded")
        resp = op.open(req, timeout=self.timeout)
        cuerpo = resp.read().decode("utf-8", "replace")
        return resp, cuerpo

    # -- login ---------------------------------------------------------------

    def login(self):
        op = self._nuevo_opener()
        url_login = self.base + "/iwc/login.iwc"

        _, html = self._abrir(url_login, opener=op)
        etiqueta = self.RE_CSRF_TAG.search(html)
        if not etiqueta:
            raise ErrorDPA("no encuentro el token _csrf en el formulario de login")
        valor = self.RE_CSRF_VAL.search(etiqueta.group(0))
        if not valor:
            raise ErrorDPA("el campo _csrf no trae valor")
        csrf = valor.group(1)

        datos = {
            "user": self.usuario,
            "password": self.password,
            "useCentral": "false",
            "_csrf": csrf,
        }
        resp, cuerpo = self._abrir(url_login, datos=datos, opener=op)

        # DPA responde 200 y vuelve a pintar el login si las credenciales fallan.
        if "name=\"password\"" in cuerpo and "login.iwc" in resp.geturl():
            raise ErrorDPA("login rechazado: revisa usuario y contrasena en dpa.json")

        self._opener = op
        log.info("sesion iniciada en DPA como '%s'", self.usuario)
        return True

    # -- peticiones ----------------------------------------------------------

    def _relogin(self, opener_caducado):
        """Renueva la sesion solo si otro hilo no lo ha hecho ya."""
        with self._lock:
            if self._opener is opener_caducado:
                log.info("sesion caducada, reintento login")
                self.login()

    def rest(self, ruta, params=None, reintento=True):
        """GET a /iwc/rest/<ruta>. Devuelve el contenido de 'data'."""
        with self._lock:
            if self._opener is None:
                self.login()
            op = self._opener  # fijamos la sesion de ESTA llamada

        url = self.base + "/iwc/rest/" + ruta.lstrip("/")
        if params:
            url += "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)

        try:
            resp, cuerpo = self._abrir(url, opener=op)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and reintento:
                self._relogin(op)
                return self.rest(ruta, params, reintento=False)
            raise ErrorDPA("HTTP %s en %s" % (e.code, ruta))

        # Si la sesion caduco, DPA redirige al login y devuelve HTML.
        if cuerpo.lstrip().startswith("<"):
            if reintento:
                self._relogin(op)
                return self.rest(ruta, params, reintento=False)
            raise ErrorDPA("respuesta no JSON en %s (sesion perdida)" % ruta)

        try:
            j = json.loads(cuerpo)
        except ValueError:
            raise ErrorDPA("JSON invalido en %s" % ruta)

        if isinstance(j, dict) and "data" in j:
            return j["data"]
        return j

    # -- endpoints concretos -------------------------------------------------

    def instancias(self):
        return self.rest("databases", {
            "include": "id,name,type,monitoringState,linkedToVm,agName",
        })

    def super_tab(self, db_id):
        return self.rest("databases/%s/super-tab" % db_id, {"pmCode": "P"})

    def categorias(self, db_id, inicio, fin):
        return self.rest("databases/%s/metrics/categories" % db_id, {
            "startTime": iso_z(inicio),
            "endTime": iso_z(fin),
            "pm": "P",
            "require": "metrics,status",
        })

    def datos(self, db_id, metricas, inicio, fin):
        return self.rest("databases/%s/metrics/data" % db_id, {
            "metrics": ",".join(metricas),
            "startTime": iso_z(inicio),
            "endTime": iso_z(fin),
            "useResourceGranularities": "true",
            "baseline": "false",
        })

    def ag_estado(self, db_id):
        """Resumen del Availability Group. Solo tiene sentido si la instancia
        pertenece a uno; si no, DPA devuelve los campos a null."""
        return self.rest("ag-status/summary", {
            "db_id": str(db_id),
            "refresh": "false",
        })

    # -- paginas HTML (solo para la tabla de bloqueos) ------------------------

    def pagina(self, ruta, params=None, reintento=True):
        """
        GET a una pagina .iwc de la web de DPA y devuelve su HTML.

        Se usa SOLO para la tabla de bloqueos, que no existe en ninguna API
        JSON. Es la parte fragil del colector: si SolarWinds cambia esa
        pagina en una actualizacion, esto deja de parsear. Por eso quien
        llama debe tratarlo como opcional y no dejar que tumbe la tarjeta.
        """
        with self._lock:
            if self._opener is None:
                self.login()
            op = self._opener

        url = self.base + "/iwc/" + ruta.lstrip("/")
        if params:
            url += "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)

        try:
            resp, cuerpo = self._abrir(url, opener=op)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and reintento:
                self._relogin(op)
                return self.pagina(ruta, params, reintento=False)
            raise ErrorDPA("HTTP %s en %s" % (e.code, ruta))

        if "login.iwc" in resp.geturl() or "name=\"password\"" in cuerpo:
            if reintento:
                self._relogin(op)
                return self.pagina(ruta, params, reintento=False)
            raise ErrorDPA("sesion perdida al pedir %s" % ruta)

        return cuerpo

    def bloqueos(self, db_id):
        """HTML de la pestana 'Blocking' de la pantalla Current."""
        return self.pagina("realTimeTab.iwc", {
            "db_id": str(db_id),
            "action": "4",
            "cs": "1",
        })

    def sesiones(self, db_id):
        """HTML de la pestana 'Sessions': las sesiones activas ahora mismo."""
        return self.pagina("realTimeTab.iwc", {
            "db_id": str(db_id),
            "action": "2",
            "cs": "1",
            "internal": "0",
            "inactive": "0",
        })


# ---------------------------------------------------------------------------
# Parseo y normalizacion
# ---------------------------------------------------------------------------

def series_a_puntos(bloques):
    """
    Convierte la respuesta de metrics/data en {nombre_metrica: [(ts, valor), ...]}
    """
    fuera = {}
    for b in bloques or []:
        meta = b.get("metaData") or {}
        nombre = meta.get("id") or meta.get("name") or b.get("id")
        if not nombre:
            continue

        series = b.get("series") or []
        if not series:
            fuera[nombre] = []
            continue
        # Puede venir mas de una serie (desglose por recurso). Nos quedamos con
        # la que coincide con la metrica pedida y, si no, con la primera.
        elegida = next((s for s in series if s.get("id") == nombre), series[0])

        puntos = []
        for p in elegida.get("dataPoints") or []:
            ts = parsear_ts(p.get("time"))
            v = num(p.get("value"))
            if ts is not None and v is not None:
                puntos.append((ts, v))
        puntos.sort(key=lambda x: x[0])
        fuera[nombre] = puntos
    return fuera


class _ExtractorTablas(HTMLParser):
    """Saca todas las tablas de un HTML como listas de filas de texto."""

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.tablas = []
        # una entrada por tabla abierta: soporta tablas anidadas dentro de
        # una celda (el menu "Action" de DPA) sin destrozar la fila de fuera
        self._pila = []
        self._mudo = False

    def _actual(self):
        return self._pila[-1] if self._pila else None

    # -- cierres tolerantes con HTML mal formado ----------------------------

    def _cerrar_celda(self, t):
        if t["celda"] is not None:
            if t["fila"] is None:  # <td> suelto, o </tr> antes que </td>
                t["fila"] = []
            t["fila"].append(" ".join("".join(t["celda"]).split()))
            t["celda"] = None

    def _cerrar_fila(self, t):
        self._cerrar_celda(t)
        if t["fila"] is not None:
            t["filas"].append(t["fila"])
            t["fila"] = None

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._mudo = True
            return
        if tag == "table":
            self._pila.append({"filas": [], "fila": None, "celda": None})
            return
        t = self._actual()
        if t is None:
            return
        if tag == "tr":
            self._cerrar_fila(t)   # por si la anterior venia sin </tr>
            t["fila"] = []
        elif tag in ("td", "th"):
            self._cerrar_celda(t)  # por si la anterior venia sin </td>
            if t["fila"] is None:
                t["fila"] = []
            t["celda"] = []

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._mudo = False
            return
        t = self._actual()
        if t is None:
            return
        if tag in ("td", "th"):
            self._cerrar_celda(t)
        elif tag == "tr":
            self._cerrar_fila(t)
        elif tag == "table":
            self._cerrar_fila(t)
            self.tablas.append(self._pila.pop()["filas"])

    def close(self):
        HTMLParser.close(self)
        while self._pila:  # tablas que se quedaron sin </table>
            t = self._pila.pop()
            self._cerrar_fila(t)
            self.tablas.append(t["filas"])

    def handle_data(self, data):
        if self._mudo:
            return
        # el texto cae en todas las celdas abiertas, para que la celda de
        # fuera conserve tambien lo que haya en la tabla anidada
        for t in self._pila:
            if t["celda"] is not None:
                t["celda"].append(data)


def parsear_sesiones(html_txt, maximo=MAX_BLOQUEOS, priorizar_bloqueadores=True):
    """
    Convierte el HTML de una tabla de sesiones de DPA en una lista de dicts.

    Sirve tanto para la pestana Blocking como para la de Sessions: ambas
    traen las columnas crudas de sysprocesses (spid, loginame, program_name,
    lastwaittype, waittime...). En la de bloqueos, la celda del spid ademas
    viene marcada con '(blocker)' o '(waiter)'.

    priorizar_bloqueadores=True  -> primero quien bloquea (vista de bloqueos)
    priorizar_bloqueadores=False -> primero quien mas lleva esperando
    """
    ex = _ExtractorTablas()
    try:
        ex.feed(html_txt)
        ex.close()
    except Exception:  # noqa: BLE001
        return []

    for tabla in ex.tablas:
        if len(tabla) < 2:
            continue
        cabecera = [c.strip().lower() for c in tabla[0]]
        if "spid" not in cabecera:
            continue
        idx = {n: i for i, n in enumerate(cabecera)}

        filas = []
        for celdas in tabla[1:]:
            def col(nombre):
                i = idx.get(nombre)
                if i is None or i >= len(celdas):
                    return ""
                return celdas[i].strip()

            crudo = col("spid")
            if not crudo:
                continue

            bajo = crudo.lower()
            if "block" in bajo:
                rol = "bloquea"
            elif "wait" in bajo:
                rol = "espera"
            else:
                rol = ""

            solo_digitos = re.sub(r"\D", "", crudo.split("(")[0])
            espera_ms = num(col("waittime"))

            # 'blocked' es el spid que esta bloqueando a este; 0 = a nadie
            bloqueado_por = col("blocked")
            if bloqueado_por in ("0", ""):
                bloqueado_por = None

            filas.append({
                "spid": solo_digitos or crudo,
                "rol": rol,
                "bloqueado_por": bloqueado_por,
                "programa": col("program_name") or col("loginame") or col("hostname"),
                "login": col("loginame") or col("nt_username"),
                "host": col("hostname"),
                "bd": col("db_name"),
                "espera": col("lastwaittype"),
                "recurso": col("waitresource"),
                "comando": col("cmd"),
                "estado": col("status"),
                "trans_abiertas": num(col("open_tran")),
                "cpu_ms": num(col("cpu")),
                "espera_s": (espera_ms / 1000.0) if espera_ms is not None else None,
            })

        if priorizar_bloqueadores:
            # el que bloquea primero, que es el que hay que mirar
            filas.sort(key=lambda f: 0 if f["rol"] == "bloquea" else 1)
        else:
            # las mas atascadas arriba
            filas.sort(key=lambda f: -(f["espera_s"] or 0))
        return filas[:maximo]

    return []


def parsear_bloqueos(html_txt, maximo=MAX_BLOQUEOS):
    return parsear_sesiones(html_txt, maximo, priorizar_bloqueadores=True)


def resumen(puntos):
    """Ultimo / media / maximo / suma de una serie."""
    if not puntos:
        return {"ultimo": None, "media": None, "max": None, "suma": None, "n": 0}
    vals = [v for _, v in puntos]
    return {
        "ultimo": vals[-1],
        "media": sum(vals) / len(vals),
        "max": max(vals),
        "suma": sum(vals),
        "n": len(vals),
    }


def parsear_ag(datos):
    """Normaliza el resumen del Availability Group."""
    if not isinstance(datos, dict) or not datos.get("listenerName"):
        return None
    det = (datos.get("agSummaryDetails") or [{}])[0]
    return {
        "listener": datos.get("listenerName"),
        "cluster": datos.get("clusterName"),
        "nombre": datos.get("agNameOnListener") or det.get("agName"),
        "salud": datos.get("agHealthStatus"),
        "salud_texto": det.get("agStatusDescription"),
        "rol": det.get("agRoleDesc"),
        "primaria": det.get("primaryReplica"),
        "instancia": det.get("instanceName"),
        "replicas": det.get("replicaCount"),
        "bases": det.get("databaseCount"),
        "conmutacion": det.get("failoverModeDesc"),
    }


def evaluar_estado(inst):
    """
    Semaforo consolidado. Nos apoyamos en los alarmLevel que DPA ya calcula,
    mas un par de reglas propias evidentes.
    Devuelve (nivel, avisos[]).
    """
    avisos = []
    nivel = "ok"

    def subir(n):
        nonlocal nivel
        if ORDEN_NIVEL.get(n, 0) > ORDEN_NIVEL.get(nivel, 0):
            nivel = n

    if inst.get("error"):
        return "caido", ["Sin respuesta de DPA: %s" % inst["error"]]

    estado_monitor = (inst.get("monitor_estado") or "").upper()
    if estado_monitor not in ("RUNNING", "STARTED"):
        return "caido", ["Monitor de DPA parado (%s)" % inst.get("monitor_estado")]

    for cat, lvl in (inst.get("alarmas") or {}).items():
        nombre = CATEGORIA_ES.get(cat, cat)
        if lvl == "CRITICAL":
            subir("critico" if cat in CATEGORIAS_ROJAS else "aviso")
            avisos.append("%s en critico" % nombre)
        elif lvl == "WARNING":
            subir("aviso")
            avisos.append("%s en aviso" % nombre)

    # Cualquier sesion bloqueada enciende el rojo: es LA senal que se vigila
    # en este panel. Va la primera en la lista de avisos.
    bloq = inst.get("sesiones_bloqueadas")
    if bloq:
        subir("critico")
        n = int(bloq)
        avisos.insert(0, "%d %s" % (
            n,
            "sesion bloqueada" if n == 1 else "sesiones bloqueadas"))

    # Tuning queda FUERA del semaforo a proposito: en DPA "tuning critico"
    # significa que hay consejos de optimizacion pendientes, no que la
    # instancia este mal. Como todas las instancias suelen tenerlos, incluirlo
    # dejaria el panel en rojo permanente y el color dejaria de informar.
    # Sigue visible en el detalle de la tarjeta.
    lvl_ag = (inst.get("tabs") or {}).get("ag")
    if lvl_ag == "CRITICAL":
        subir("critico")
        avisos.append("Availability Group en critico")
    elif lvl_ag == "WARNING":
        subir("aviso")
        avisos.append("Availability Group en aviso")

    return nivel, avisos


def parsear_instancia(meta, tabs, cats, datos_series):
    """Normaliza todo lo de una instancia en un dict plano."""
    puntos = series_a_puntos(datos_series)
    m = {}
    for nombre_dpa, clave in METRICAS.items():
        m[clave] = resumen(puntos.get(nombre_dpa) or [])

    alarmas = {}
    for c in cats or []:
        alarmas[c.get("name")] = c.get("alarmLevel")

    tabs = tabs or {}
    inst = {
        "db_id": meta.get("id"),
        "nombre": meta.get("name"),
        "tipo": meta.get("type"),
        "ag": meta.get("agName"),
        "monitor_estado": meta.get("monitoringState"),
        "vinculado_vm": bool(meta.get("linkedToVm")),
        "ts": time.time(),
        "error": None,

        # sesiones (lo grande)
        "sesiones_activas": m["sesiones_activas"]["ultimo"],
        "sesiones_activas_media": m["sesiones_activas"]["media"],
        "sesiones_activas_max": m["sesiones_activas"]["max"],
        "sesiones_bloqueadas": m["sesiones_bloqueadas"]["ultimo"],
        "sesiones_bloqueadas_max": m["sesiones_bloqueadas"]["max"],
        "bloqueos": [],
        "bloqueos_error": None,
        "sesiones_detalle": [],
        "sesiones_detalle_error": None,
        "ag_detalle": None,

        # espera (lo grande, segundo bloque)
        "wait_total_hora": m["wait_time"]["suma"],
        "wait_medio": m["wait_time"]["media"],
        "wait_pico": m["wait_time"]["max"],

        # recursos
        "cpu": m["cpu"]["ultimo"],
        "cpu_media": m["cpu"]["media"],
        "cpu_instancia": m["cpu_instancia"]["ultimo"],
        "signal_waits": m["signal_waits"]["ultimo"],
        "memoria": m["memoria"]["ultimo"],
        "ple": m["ple"]["ultimo"],
        "cache_hit": m["cache_hit"]["ultimo"],

        # desglose de memoria
        "cache_mb": m["cache_mb"]["ultimo"],
        "plan_cache_mb": m["plan_cache_mb"]["ultimo"],
        "compilaciones_seg": m["compilaciones_seg"]["ultimo"],
        "recompilaciones_seg": m["recompilaciones_seg"]["ultimo"],

        # hipervisor (vacias si la instancia no esta vinculada a su VM)
        "vm_ready": m["vm_ready"]["ultimo"],
        "vm_balloon": m["vm_balloon"]["ultimo"],
        "vm_cpu": m["vm_cpu"]["ultimo"],
        "host_cpu": m["host_cpu"]["ultimo"],
        "host_memoria": m["host_memoria"]["ultimo"],

        "disco_lectura": m["disco_lectura"]["ultimo"],
        "disco_escritura": m["disco_escritura"]["ultimo"],
        "batch_seg": m["batch_seg"]["ultimo"],
        "trans_seg": m["trans_seg"]["ultimo"],
        "rtt": m["rtt"]["ultimo"],

        # backups
        "backups_activos": m["backups_activos"]["ultimo"],
        "sin_full_dias": m["sin_full_dias"]["ultimo"],
        "sin_log_horas": m["sin_log_horas"]["ultimo"],
        "logs_sin_archivar_mb": m["logs_sin_archivar_mb"]["ultimo"],
        "backup_total_gb": m["backup_total_gb"]["ultimo"],

        # tempdb
        "tempdb_libre_mb": m["tempdb_libre_mb"]["ultimo"],
        "tempdb_log_libre_mb": m["tempdb_log_libre_mb"]["ultimo"],
        "tempdb_log_libre_pct": m["tempdb_log_libre_pct"]["ultimo"],

        # disco
        "disco_cola": m["disco_cola"]["ultimo"],
        "disco_cola_max": m["disco_cola"]["max"],
        "paginas_leidas_seg": m["paginas_leidas_seg"]["ultimo"],
        "paginas_escritas_seg": m["paginas_escritas_seg"]["ultimo"],

        "alarmas": alarmas,
        "tabs": {
            "trends": tabs.get("trendsTabLevel"),
            "tuning": tabs.get("tuningTabLevel"),
            "ag": tabs.get("agHealthTabLevel"),
            "recursos": tabs.get("resourcesTabLevel"),
        },
        "es_ag": bool(tabs.get("isAgClustered")),
    }

    inst["nivel"], inst["avisos"] = evaluar_estado(inst)
    return inst


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

# ------------------------------------------------------------------ SQL directo
#
# DPA no expone los jobs del Agent por ninguna via: su API REST devuelve 404 en
# /jobs, /agent-jobs y /sqlagent, y SQL Server escribe en program_name el
# job_id en binario, nunca el nombre. Para resolverlo hace falta leer msdb, y
# eso solo se consigue conectando directamente a la instancia.
#
# Todo lo de este bloque es OPCIONAL y esta aislado a proposito: si pymssql no
# esta instalado, si la configuracion no trae credenciales o si la conexion
# falla, el panel sigue funcionando exactamente igual con los datos de DPA.
# Es una herramienta de monitorizacion: nunca debe caerse por un extra.

try:
    import pymssql  # type: ignore
    PYMSSQL = True
except ImportError:
    pymssql = None
    PYMSSQL = False


SQL_NOMBRES_JOB = """
SELECT CONVERT(CHAR(36), job_id) AS id, name
FROM   msdb.dbo.sysjobs
"""

# Jobs corriendo ahora mismo, con su duracion media de los ultimos 7 dias para
# poder senalar los que se estan pasando de lo normal.
#
# OJO con el `%` del modulo de T-SQL: pymssql solo interpola parametros cuando
# se le pasan, y aqui se ejecuta con `cur.execute(sql)` a secas. Por eso va un
# `%` simple. Si algun dia se le pasan parametros habria que duplicarlo a `%%`.
SQL_JOBS_ACTIVOS = """
WITH med AS (
    SELECT h.job_id,
           AVG(h.run_duration/10000*3600 + (h.run_duration/100%100)*60
               + h.run_duration%100) AS media_s
    FROM   msdb.dbo.sysjobhistory h
    WHERE  h.step_id = 0
      AND  h.run_status = 1          -- solo ejecuciones correctas: las
                                     -- abortadas bajarian la media
      AND  h.run_date >= CONVERT(INT, CONVERT(VARCHAR(8),
                                 DATEADD(DAY,-7,GETDATE()), 112))
    GROUP BY h.job_id
)
SELECT TOP 40
       j.name                                                     AS nombre,
       DATEDIFF(SECOND, ja.start_execution_date, GETDATE())       AS segundos,
       ISNULL(med.media_s, 0)                                     AS media_s
FROM   msdb.dbo.sysjobactivity ja
JOIN   msdb.dbo.sysjobs j ON j.job_id = ja.job_id
LEFT JOIN med ON med.job_id = ja.job_id
WHERE  ja.session_id = (SELECT MAX(session_id) FROM msdb.dbo.syssessions)
  AND  ja.start_execution_date IS NOT NULL
  AND  ja.stop_execution_date  IS NULL
ORDER BY ja.start_execution_date
"""


# Esperas acumuladas desde el arranque. La lista de exclusion es
# imprescindible: sin ella HADR_WORK_QUEUE por si sola aporta 81 millones de
# segundos de espera INACTIVA (hilos de fondo sin trabajo) y distorsiona todos
# los porcentajes. Son esperas que no significan nada sobre el rendimiento.
SQL_ESPERAS = """
SELECT TOP 6 wait_type,
       wait_time_ms/1000 AS espera_s,
       -- el NULLIF evita el error 8134 si todas las esperas supervivientes
       -- tuvieran 0 ms (instancia recien arrancada)
       CAST(100.0*wait_time_ms/NULLIF(SUM(wait_time_ms) OVER(), 0)
            AS DECIMAL(5,2)) AS pct
FROM   sys.dm_os_wait_stats
WHERE  waiting_tasks_count > 0
  AND  wait_type NOT IN (
       'CLR_SEMAPHORE','LAZYWRITER_SLEEP','RESOURCE_QUEUE','SLEEP_TASK',
       'SLEEP_SYSTEMTASK','SQLTRACE_BUFFER_FLUSH','WAITFOR','LOGMGR_QUEUE',
       'CHECKPOINT_QUEUE','REQUEST_FOR_DEADLOCK_SEARCH','XE_TIMER_EVENT',
       'BROKER_TO_FLUSH','BROKER_TASK_STOP','CLR_MANUAL_EVENT',
       'DIRTY_PAGE_POLL','SP_SERVER_DIAGNOSTICS_SLEEP','QDS_ASYNC_QUEUE',
       'QDS_SHUTDOWN_QUEUE','XE_DISPATCHER_WAIT','BROKER_EVENTHANDLER',
       'BROKER_RECEIVE_WAITFOR','BROKER_TRANSMITTER','FT_IFTS_SCHEDULER_IDLE_WAIT',
       'ONDEMAND_TASK_QUEUE','SQLTRACE_INCREMENTAL_FLUSH_SLEEP',
       'HADR_WORK_QUEUE','HADR_TIMER_TASK','HADR_NOTIFICATION_DEQUEUE',
       'HADR_FILESTREAM_IOMGR_IOCOMPLETION','HADR_LOGCAPTURE_WAIT',
       'HADR_CLUSAPI_CALL','PARALLEL_REDO_WORKER_WAIT_WORK',
       'PARALLEL_REDO_DRAIN_WORKER','PWAIT_EXTENSIBILITY_CLEANUP_TASK',
       'VDI_CLIENT_OTHER','PREEMPTIVE_XE_GETTARGETSTATE')
ORDER BY wait_time_ms DESC
"""

# Dos contadores baratos: cuantos planes hay en cache (sube cuando el SQL no
# va parametrizado) y como ha ido el SQL Agent hoy.
# Una sola pasada por cada origen: la cache de planes puede tener cientos de
# miles de entradas y no conviene recorrerla dos veces cada dos minutos.
SQL_RESUMEN = """
SELECT p.planes, p.planes_adhoc, j.jobs_hoy, j.jobs_fallidos
FROM (
    SELECT COUNT(*) AS planes,
           SUM(CASE WHEN objtype IN ('Adhoc','Prepared') THEN 1 ELSE 0 END)
               AS planes_adhoc
    FROM   sys.dm_exec_cached_plans
) AS p
CROSS JOIN (
    SELECT COUNT(*) AS jobs_hoy,
           SUM(CASE WHEN run_status <> 1 THEN 1 ELSE 0 END) AS jobs_fallidos
    FROM   msdb.dbo.sysjobhistory
    WHERE  step_id = 0
      AND  run_date = CONVERT(INT, CONVERT(VARCHAR(8), GETDATE(), 112))
) AS j
"""


class ClienteSQL(object):
    """
    Conexion de solo lectura a una instancia monitorizada. Abre y cierra en
    cada consulta a proposito: son cuatro consultas ligeras, cada una con su
    propio intervalo (nombres de job 900 s, jobs activos 30 s, esperas 300 s,
    resumen 120 s), y mantener conexiones vivas contra produccion no compensa
    el riesgo.
    """

    def __init__(self, host, puerto, usuario, password, timeout=8):
        self.host = host
        self.puerto = int(puerto or 1433)
        self.usuario = usuario
        self.password = password
        self.timeout = int(timeout)

    def consulta(self, sql):
        conn = pymssql.connect(
            server=self.host, port=self.puerto,
            user=self.usuario, password=self.password,
            database="msdb", timeout=self.timeout,
            login_timeout=self.timeout, as_dict=True,
            appname="dpa-dashboard")
        try:
            # as_dict ya va en connect(): las versiones antiguas de pymssql no
            # aceptan kwargs en cursor()
            cur = conn.cursor()
            cur.execute(sql)
            return cur.fetchall()
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


class SQLDirecto(object):
    """
    Envoltorio sobre las conexiones directas de todas las instancias. Mantiene
    las caches y se traga cualquier error: el consumidor nunca tiene que
    comprobar nada.
    """

    def __init__(self, cfg):
        raw = cfg.get("sql_directo") or {}
        self.activo = bool(raw.get("activo")) and PYMSSQL
        self.motivo = ""
        if raw.get("activo") and not PYMSSQL:
            self.motivo = ("pymssql no esta instalado: "
                           "pip3 install pymssql")
            log.warning("SQL directo pedido pero %s", self.motivo)

        self.int_nombres = int(raw.get("intervalo_nombres_segundos", 900))
        self.int_jobs = int(raw.get("intervalo_jobs_segundos", 30))
        timeout = int(raw.get("timeout_segundos", 8))

        self.clientes = {}
        for nombre, c in (raw.get("instancias") or {}).items():
            if not c.get("host") or not c.get("usuario"):
                continue
            self.clientes[nombre] = ClienteSQL(
                c.get("host"), c.get("puerto", 1433),
                c.get("usuario"), c.get("password"), timeout)

        self.int_esperas = int(raw.get("intervalo_esperas_segundos", 300))
        self.int_resumen = int(raw.get("intervalo_resumen_segundos", 120))

        self._nombres = {}    # instancia -> (ts, {job_id_upper: nombre})
        self._jobs = {}       # instancia -> (ts, [ {...} ])
        self._esperas = {}    # instancia -> (ts, [ {...} ])
        self._resumen = {}    # instancia -> (ts, {...})
        self._fallo = {}      # instancia -> texto del ultimo error
        self._lock = threading.Lock()

    def habilitado(self, instancia):
        return self.activo and instancia in self.clientes

    def _cacheado(self, cache, instancia, intervalo, consulta, transforma):
        ahora = time.time()
        with self._lock:
            hit = cache.get(instancia)
            if hit and ahora - hit[0] < intervalo:
                return hit[1]
        try:
            filas = self.clientes[instancia].consulta(consulta)
            valor = transforma(filas)
            with self._lock:
                # la marca de tiempo se pone al terminar, no al empezar: la
                # consulta puede tardar segundos y el dato nacería viejo
                cache[instancia] = (time.time(), valor)
                self._fallo.pop(instancia, None)
            return valor
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self._fallo[instancia] = str(e)
                hit = cache.get(instancia)
            log.warning("SQL directo (%s): %s", instancia, e)
            # se devuelve lo ultimo bueno si lo hay: mejor dato viejo que nada
            return hit[1] if hit else None

    def nombres_job(self, instancia):
        """{job_id en mayusculas: nombre}. Los jobs cambian poco: cache larga."""
        if not self.habilitado(instancia):
            return {}
        v = self._cacheado(
            self._nombres, instancia, self.int_nombres, SQL_NOMBRES_JOB,
            lambda filas: dict(
                (str(f["id"]).upper().strip(), f["name"]) for f in filas))
        return v or {}

    def jobs_activos(self, instancia):
        if not self.habilitado(instancia):
            return None
        def _t(filas):
            out = []
            for f in filas:
                seg = int(f.get("segundos") or 0)
                media = int(f.get("media_s") or 0)
                out.append({
                    "nombre": f.get("nombre"),
                    "segundos": seg,
                    "media_s": media or None,
                    # "se esta pasando": mas del doble de su media y al menos
                    # un minuto. Sin el minimo, un job de 2 s que tarda 5
                    # saldria marcado y seria ruido.
                    "lento": bool(media and seg > media * 2 and seg >= 60),
                })
            return out
        return self._cacheado(self._jobs, instancia, self.int_jobs,
                              SQL_JOBS_ACTIVOS, _t)

    def esperas(self, instancia):
        """
        Top de esperas acumuladas desde el arranque. Es el dato que mejor
        explica donde se va el tiempo, pero OJO: es acumulado de semanas, no
        dice lo que pasa ahora mismo.
        """
        if not self.habilitado(instancia):
            return None
        def _t(filas):
            return [{"tipo": f.get("wait_type"),
                     "segundos": int(f.get("espera_s") or 0),
                     "pct": float(f.get("pct") or 0)} for f in filas]
        return self._cacheado(self._esperas, instancia, self.int_esperas,
                              SQL_ESPERAS, _t)

    def resumen(self, instancia):
        if not self.habilitado(instancia):
            return None
        def _t(filas):
            if not filas:
                return None
            f = filas[0]
            return {"planes": f.get("planes"),
                    "planes_adhoc": f.get("planes_adhoc"),
                    "jobs_hoy": f.get("jobs_hoy"),
                    "jobs_fallidos": f.get("jobs_fallidos")}
        return self._cacheado(self._resumen, instancia, self.int_resumen,
                              SQL_RESUMEN, _t)

    def error(self, instancia):
        with self._lock:
            return self._fallo.get(instancia)


# SQL Server guarda el job_id como uniqueidentifier, pero en program_name lo
# escribe en binario: los tres primeros grupos van en little-endian. Hay que
# darles la vuelta byte a byte para recomponer el GUID.
RE_JOBSTEP = re.compile(
    r"SQLAgent\s*-\s*TSQL\s*JobStep\s*\(\s*Job\s+0x([0-9A-Fa-f]{32})", re.I)


def guid_desde_hex(hex_txt):
    h = hex_txt.upper()
    b = [h[k * 2:k * 2 + 2] for k in range(16)]
    return (b[3] + b[2] + b[1] + b[0] + "-" + b[5] + b[4] + "-" +
            b[7] + b[6] + "-" + b[8] + b[9] + "-" + "".join(b[10:16]))


def resolver_jobs(filas, nombres):
    """
    Anade `job_nombre` a las filas cuyo `programa` sea un paso de SQL Agent.
    Si no hay mapa de nombres no hace nada: el panel ya sabe degradar.
    """
    if not filas or not nombres:
        return filas
    for f in filas:
        m = RE_JOBSTEP.search(f.get("programa") or "")
        if not m:
            continue
        guid = guid_desde_hex(m.group(1))
        nom = nombres.get(guid)
        if nom:
            f["job_nombre"] = nom
    return filas


class Monitor(object):

    def __init__(self, cfg):
        self.cfg = cfg
        self.cliente = ClienteDPA(cfg["url"], cfg["usuario"], cfg["password"],
                                  timeout=cfg.get("timeout_segundos", 20),
                                  verificar_ssl=cfg.get("verificar_ssl", False))
        self.intervalo = int(cfg.get("intervalo_segundos", 15))
        self.ventana_min = int(cfg.get("ventana_minutos", 60))
        self.excluir = set(cfg.get("excluir", []))
        self.listar_sesiones = bool(cfg.get("listar_sesiones_activas", True))
        self.max_sesiones = int(cfg.get("max_sesiones", MAX_SESIONES))
        # La tabla de sesiones activas son MEGAS de HTML por instancia (3 MB
        # con 87 sesiones), asi que va a su propio ritmo, mucho mas lento que
        # el resto del sondeo.
        self.intervalo_sesiones = int(cfg.get("intervalo_sesiones_segundos", 120))
        self._cache_sesiones = {}
        self._cache_ses_lock = threading.Lock()
        # Recuento de bloqueos del dia. DPA da el numero de sesiones
        # bloqueadas instante a instante, pero no dice QUE se repite. Como ya
        # sondeamos la lista en cada ciclo, la vamos tabulando aqui. Se
        # reinicia solo al cambiar de dia.
        self._bloq_hoy = {}
        self._bloq_dia = None
        self._bloq_lock = threading.Lock()
        # Opcional: conexion directa a las instancias para lo que DPA no da
        # (nombres de job y jobs en ejecucion). Si no esta configurado o
        # pymssql no esta, queda inerte y el panel funciona igual.
        self.sql = SQLDirecto(cfg)
        # Nombre de la maquina donde corre el dashboard, para pintarlo en la
        # cabecera del modo presentacion. Se puede fijar a mano en dpa.json.
        try:
            anfitrion = socket.gethostname()
        except Exception:  # noqa: BLE001
            anfitrion = ""
        self.servidor = cfg.get("nombre_servidor") or anfitrion or "dashboard"

        self._estado = {"actualizado": None, "intervalo": self.intervalo,
                        "servidor": self.servidor,
                        "instancias": [], "error": None}
        self._lock = threading.Lock()
        self._hilos = {}
        self._ultimos = {}
        self._cache_hist = {}
        self._cache_lock = threading.Lock()

    # -- lectura -------------------------------------------------------------

    def leer_instancia(self, meta, destino):
        db_id = meta.get("id")
        fin = ahora_utc()
        inicio = fin - timedelta(minutes=self.ventana_min)
        try:
            tabs = self.cliente.super_tab(db_id)
            cats = self.cliente.categorias(db_id, inicio, fin)
            datos = self.cliente.datos(db_id, list(METRICAS.keys()), inicio, fin)
            inst = parsear_instancia(meta, tabs, cats, datos)

            # Solo las instancias que pertenecen a un AG
            if meta.get("agName") or inst.get("es_ag"):
                try:
                    inst["ag_detalle"] = parsear_ag(self.cliente.ag_estado(db_id))
                except Exception as e:  # noqa: BLE001
                    log.warning("%s: no pude leer el estado del AG: %s",
                                meta.get("name"), e)

            # La lista de bloqueos solo se pide si DPA dice que hay alguno, y
            # va en su propio try: es HTML parseado, la parte fragil. Si falla,
            # la tarjeta sigue viva y solo se queda sin la lista.
            if inst.get("sesiones_bloqueadas"):
                try:
                    inst["bloqueos"] = parsear_bloqueos(
                        self.cliente.bloqueos(db_id))
                except Exception as e:  # noqa: BLE001
                    log.warning("%s: no pude leer los bloqueos: %s",
                                meta.get("name"), e)
                    inst["bloqueos"] = []
                    inst["bloqueos_error"] = str(e)

            # Detalle de las sesiones activas. Se puede desactivar en la
            # configuracion: es la peticion mas pesada de todas, sobre todo en
            # instancias con muchas sesiones concurrentes.
            if self.listar_sesiones and inst.get("sesiones_activas"):
                try:
                    inst["sesiones_detalle"] = self.detalle_sesiones(db_id)
                except Exception as e:  # noqa: BLE001
                    log.warning("%s: no pude leer las sesiones activas: %s",
                                meta.get("name"), e)
                    inst["sesiones_detalle_error"] = str(e)

            # Extras por conexion directa. Van en su propio try y despues de
            # haber montado la instancia: si esto falla, la tarjeta ya esta
            # completa con lo que da DPA y solo se queda sin los nombres.
            nombre_inst = meta.get("name")
            if self.sql.habilitado(nombre_inst):
                try:
                    nombres = self.sql.nombres_job(nombre_inst)
                    if nombres:
                        resolver_jobs(inst.get("sesiones_detalle"), nombres)
                        resolver_jobs(inst.get("bloqueos"), nombres)
                        # el recuento se hace DESPUES de resolver, para que la
                        # etiqueta guarde el nombre del job y no el binario
                    jobs = self.sql.jobs_activos(nombre_inst)
                    if jobs is not None:
                        inst["jobs_activos"] = jobs
                    esp = self.sql.esperas(nombre_inst)
                    if esp:
                        inst["esperas_top"] = esp
                    res = self.sql.resumen(nombre_inst)
                    if res:
                        inst["resumen_sql"] = res
                    err = self.sql.error(nombre_inst)
                    if err:
                        inst["sql_error"] = err
                except Exception as e:  # noqa: BLE001
                    log.warning("%s: SQL directo: %s", nombre_inst, e)
                    inst["sql_error"] = str(e)

            # Recuento de bloqueos del dia. Va al final y fuera del try del
            # SQL directo: no depende de el y debe funcionar igual sin el.
            try:
                self._tabular_bloqueos(db_id, inst.get("bloqueos"))
                top = self.top_bloqueos(db_id)
                if top:
                    inst["bloqueos_top"] = top
            except Exception as e:  # noqa: BLE001
                log.warning("%s: recuento de bloqueos: %s", meta.get("name"), e)

            destino[db_id] = inst
        except Exception as e:  # noqa: BLE001
            log.warning("%s: %s", meta.get("name"), e)
            destino[db_id] = {
                "db_id": db_id,
                "nombre": meta.get("name"),
                "tipo": meta.get("type"),
                "ag": meta.get("agName"),
                "monitor_estado": meta.get("monitoringState"),
                "ts": time.time(),
                "error": str(e),
                "alarmas": {},
                "tabs": {},
                "nivel": "caido",
                "avisos": ["Sin respuesta de DPA"],
            }

    # -- recuento de bloqueos del dia ---------------------------------------

    def _tabular_bloqueos(self, db_id, lista):
        """
        Acumula que se repite. La clave intenta ser lo mas informativa posible:
        el recurso bloqueado si DPA lo da, y si no el programa (que para los
        jobs ya viene resuelto a nombre). Se guarda por dia local.
        """
        if not lista:
            return
        ahora = time.time()
        with self._bloq_lock:
            self._reset_si_cambia_dia()
            tabla = self._bloq_hoy.setdefault(db_id, {})
            for f in lista:
                # solo nos interesa quien causa el bloqueo, no quien lo sufre
                if f.get("rol") == "espera":
                    continue
                # El nombre del job o el programa identifican al culpable y se
                # repiten; el recurso bloqueado es casi unico por incidente y
                # llenaria la tabla de entradas de "1 vez". Va el ultimo.
                etiqueta = (f.get("job_nombre") or f.get("programa")
                            or f.get("recurso") or f.get("comando") or "")
                etiqueta = str(etiqueta).strip()
                if not etiqueta:
                    continue
                bd = (f.get("bd") or "").strip()
                clave = (bd, etiqueta)
                e = tabla.get(clave)
                if e is None:
                    # tope de seguridad: si la instancia genera etiquetas muy
                    # variadas, no dejamos crecer la tabla sin limite
                    if len(tabla) >= 500:
                        continue
                    tabla[clave] = {"bd": bd, "etiqueta": etiqueta,
                                    "veces": 1, "ultima": ahora,
                                    "espera_max": f.get("espera_s") or 0}
                else:
                    # Sondeamos cada pocos segundos: un mismo bloqueo que dure
                    # diez minutos aparecera decenas de veces. Solo se cuenta
                    # como incidente nuevo si hubo un hueco de mas de un
                    # minuto; si no, se actualiza la espera maxima y ya.
                    if ahora - (e.get("ultima") or 0) > 60:
                        e["veces"] += 1
                    e["ultima"] = ahora
                    if (f.get("espera_s") or 0) > (e["espera_max"] or 0):
                        e["espera_max"] = f.get("espera_s")

    def _reset_si_cambia_dia(self):
        """Vacia el recuento al cambiar de dia. Llamar con _bloq_lock tomado."""
        hoy = time.strftime("%Y-%m-%d")
        if self._bloq_dia != hoy:
            self._bloq_hoy = {}
            self._bloq_dia = hoy

    def top_bloqueos(self, db_id, maximo=6):
        with self._bloq_lock:
            # tambien aqui: si no hubo bloqueos en todo el dia nuevo, sin esto
            # seguiriamos sirviendo el recuento de ayer bajo el rotulo "hoy"
            self._reset_si_cambia_dia()
            filas = [dict(x) for x in (self._bloq_hoy.get(db_id) or {}).values()]
        filas.sort(key=lambda x: (-x["veces"], -x["ultima"]))
        return filas[:maximo]

    def detalle_sesiones(self, db_id):
        """
        Sesiones activas, con cache propia. La respuesta de DPA son megas de
        HTML, asi que no tiene sentido pedirla en cada ciclo: se refresca cada
        `intervalo_sesiones_segundos` y entre medias se sirve la ultima.
        """
        ahora = time.time()
        with self._cache_ses_lock:
            hit = self._cache_sesiones.get(db_id)
            if hit and ahora - hit[0] < self.intervalo_sesiones:
                return hit[1]

        lista = parsear_sesiones(self.cliente.sesiones(db_id),
                                 maximo=self.max_sesiones,
                                 priorizar_bloqueadores=False)
        with self._cache_ses_lock:
            self._cache_sesiones[db_id] = (ahora, lista)
        return lista

    def ciclo(self):
        try:
            metas = self.cliente.instancias()
        except Exception as e:  # noqa: BLE001
            log.error("no puedo listar instancias: %s", e)
            with self._lock:
                self._estado["error"] = str(e)
                self._estado["actualizado"] = time.time()
            return

        if not isinstance(metas, list):
            log.error("respuesta inesperada al listar instancias")
            return

        metas = [m for m in metas
                 if isinstance(m, dict) and m.get("id") is not None
                 and m.get("name") not in self.excluir]
        metas.sort(key=lambda m: (m.get("name") or "").upper())

        destino = {}
        hilos = []
        for meta in metas:
            db_id = meta["id"]
            viejo = self._hilos.get(db_id)
            if viejo is not None and viejo.is_alive():
                log.info("%s: sondeo anterior aun en curso, salto este ciclo",
                         meta.get("name"))
                continue
            h = threading.Thread(target=self.leer_instancia,
                                 args=(meta, destino), daemon=True)
            self._hilos[db_id] = h
            hilos.append(h)
            h.start()

        # plazo global, no por hilo: si varias instancias se cuelgan no queremos
        # que el ciclo tarde N x timeout
        limite = time.time() + self.cfg.get("timeout_segundos", 20) * 3
        for h in hilos:
            h.join(timeout=max(0.1, limite - time.time()))

        # conservamos la ultima lectura buena de las instancias que este ciclo
        # no han contestado, para que su tarjeta no desaparezca del panel.
        # No iteramos 'destino' directamente: un hilo rezagado podria estar
        # escribiendo en el mientras tanto.
        for m in metas:
            v = destino.get(m["id"])
            if v is not None:
                self._ultimos[m["id"]] = v

        vivos = {m["id"] for m in metas}
        self._ultimos = {k: v for k, v in self._ultimos.items() if k in vivos}
        instancias = [self._ultimos[m["id"]] for m in metas
                      if m["id"] in self._ultimos]

        with self._lock:
            if instancias:
                self._estado["instancias"] = instancias
            self._estado["error"] = None
            self._estado["actualizado"] = time.time()

    def bucle(self):
        while True:
            t0 = time.time()
            try:
                self.ciclo()
            except Exception as e:  # noqa: BLE001
                log.exception("error en el ciclo: %s", e)
            espera = self.intervalo - (time.time() - t0)
            time.sleep(max(1.0, espera))

    def instantanea(self):
        with self._lock:
            return dict(self._estado)

    # -- historico (pasarela a DPA, con cache corta) --------------------------

    def historico(self, db_id, metrica, horas):
        clave = (db_id, metrica, horas)
        ahora = time.time()
        with self._cache_lock:
            hit = self._cache_hist.get(clave)
            if hit and ahora - hit[0] < 55:
                return hit[1]

        fin = ahora_utc()
        inicio = fin - timedelta(hours=horas)
        datos = self.cliente.datos(db_id, [metrica], inicio, fin)
        puntos = series_a_puntos(datos).get(metrica) or []
        # diezmamos a 720 puntos como mucho
        if len(puntos) > 720:
            paso = len(puntos) // 720 + 1
            puntos = puntos[::paso]
        salida = [{"t": t, "v": v} for t, v in puntos]
        with self._cache_lock:
            # purga de entradas viejas para que la cache no crezca sin limite
            self._cache_hist = {k: v for k, v in self._cache_hist.items()
                                if ahora - v[0] < 300}
            self._cache_hist[clave] = (ahora, salida)
        return salida


# ---------------------------------------------------------------------------
# Servidor HTTP
# ---------------------------------------------------------------------------

def crear_handler(monitor):

    class Handler(BaseHTTPRequestHandler):
        server_version = "dashboard-dpa/1.0"

        def log_message(self, *_args):
            pass

        def _enviar(self, codigo, cuerpo, tipo="application/json; charset=utf-8"):
            self.send_response(codigo)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(cuerpo)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(cuerpo)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _error(self, codigo, mensaje):
            self._enviar(codigo, json_seguro({"error": mensaje}))

        def do_GET(self):  # noqa: N802
            partes = urllib.parse.urlparse(self.path)
            ruta = partes.path
            q = urllib.parse.parse_qs(partes.query)

            if ruta in ("/", "/index.html", "/dpa.html", "/dashboard.html"):
                try:
                    with open(WEB_PATH, "rb") as f:
                        self._enviar(200, f.read(), "text/html; charset=utf-8")
                except OSError:
                    self._error(500, "no encuentro dpa.html")
                return

            if ruta == "/logo":
                for nombre in LOGOS:
                    p = os.path.join(BASE_DIR, nombre)
                    if os.path.exists(p):
                        tipo = mimetypes.guess_type(p)[0] or "image/png"
                        with open(p, "rb") as f:
                            self._enviar(200, f.read(), tipo)
                        return
                self._error(404, "sin logo")
                return

            if ruta == "/api/estado":
                self._enviar(200, json_seguro(monitor.instantanea()))
                return

            if ruta == "/api/historico":
                try:
                    db_id = int(q.get("db", ["0"])[0])
                except ValueError:
                    self._error(400, "db invalido")
                    return
                metrica = (q.get("metrica") or ["Active Sessions"])[0]
                if metrica not in METRICAS:
                    self._error(400, "metrica no permitida")
                    return
                try:
                    horas = float(q.get("horas", ["24"])[0])
                except ValueError:
                    horas = 24.0
                # cuantizamos: si no, cada valor distinto salta la cache
                horas = round(max(0.5, min(horas, 168.0)), 1)

                validos = {str(i.get("db_id")) for i in
                           monitor.instantanea().get("instancias", [])}
                if str(db_id) not in validos:
                    self._error(400, "instancia desconocida")
                    return
                try:
                    self._enviar(200, json_seguro(
                        {"db": db_id, "metrica": metrica,
                         "puntos": monitor.historico(db_id, metrica, horas)}))
                except Exception as e:  # noqa: BLE001
                    log.warning("historico db=%s metrica=%s: %s", db_id, metrica, e)
                    self._error(502, "no se pudo consultar el historico")
                return

            self._error(404, "no encontrado")

    return Handler


# ---------------------------------------------------------------------------
# Arranque
# ---------------------------------------------------------------------------

def cargar_config():
    if not os.path.exists(CONFIG_PATH):
        raise SystemExit("falta %s (copia dpa.json.example y rellenalo)" % CONFIG_PATH)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for clave in ("url", "usuario", "password"):
        if not cfg.get(clave):
            raise SystemExit("dpa.json: falta '%s'" % clave)
    return cfg


def main():
    ap = argparse.ArgumentParser(description="Dashboard de instancias DPA")
    ap.add_argument("--once", action="store_true",
                    help="una lectura y volcado JSON por stdout")
    ap.add_argument("--fixture", help="parsea un JSON guardado, sin red")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    if args.fixture:
        with open(args.fixture, "r", encoding="utf-8") as f:
            crudo = json.load(f)
        inst = parsear_instancia(crudo.get("meta", {}), crudo.get("tabs", {}),
                                 crudo.get("cats", []), crudo.get("datos", []))
        print(json.dumps(inst, indent=2, ensure_ascii=False))
        return

    cfg = cargar_config()
    monitor = Monitor(cfg)

    if args.once:
        monitor.ciclo()
        print(json.dumps(monitor.instantanea(), indent=2, ensure_ascii=False))
        return

    hilo = threading.Thread(target=monitor.bucle, daemon=True)
    hilo.start()

    puerto = int(cfg.get("puerto", 9000))
    servidor = ThreadingHTTPServer(("0.0.0.0", puerto), crear_handler(monitor))
    log.info("dashboard escuchando en el puerto %d", puerto)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        log.info("parando")


if __name__ == "__main__":
    main()
