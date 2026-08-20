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
    "SQL Disk Read Latency": "disco_lectura",
    "SQL Disk Write Latency": "disco_escritura",
    "Batch Requests Per Sec": "batch_seg",
    "Transaction Rate": "trans_seg",
    "Round-trip Time (ms)": "rtt",
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

# Cuantas sesiones bloqueadas listamos como maximo en una tarjeta.
MAX_BLOQUEOS = 8

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

    def __init__(self, url, usuario, password, timeout=20):
        self.base = url.rstrip("/")
        self.usuario = usuario
        self.password = password
        self.timeout = timeout
        self._lock = threading.Lock()
        self._opener = None

    # -- infraestructura -----------------------------------------------------

    def _nuevo_opener(self):
        cj = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cj))
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


def parsear_bloqueos(html_txt, maximo=MAX_BLOQUEOS):
    """
    Convierte el HTML de la pestana Blocking en una lista de sesiones.

    La tabla trae las columnas crudas de sysprocesses (spid, loginame,
    program_name, lastwaittype, waittime...) y marca cada fila con
    '(blocker)' o '(waiter)' dentro de la celda del spid.
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

            filas.append({
                "spid": solo_digitos or crudo,
                "rol": rol,
                "programa": col("program_name") or col("loginame") or col("hostname"),
                "login": col("loginame"),
                "host": col("hostname"),
                "bd": col("db_name"),
                "espera": col("lastwaittype"),
                "comando": col("cmd"),
                "estado": col("status"),
                "espera_s": (espera_ms / 1000.0) if espera_ms is not None else None,
            })

        # el que bloquea primero, que es el que hay que mirar
        filas.sort(key=lambda f: 0 if f["rol"] == "bloquea" else 1)
        return filas[:maximo]

    return []


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
        "disco_lectura": m["disco_lectura"]["ultimo"],
        "disco_escritura": m["disco_escritura"]["ultimo"],
        "batch_seg": m["batch_seg"]["ultimo"],
        "trans_seg": m["trans_seg"]["ultimo"],
        "rtt": m["rtt"]["ultimo"],

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

class Monitor(object):

    def __init__(self, cfg):
        self.cfg = cfg
        self.cliente = ClienteDPA(cfg["url"], cfg["usuario"], cfg["password"],
                                  timeout=cfg.get("timeout_segundos", 20))
        self.intervalo = int(cfg.get("intervalo_segundos", 15))
        self.ventana_min = int(cfg.get("ventana_minutos", 60))
        self.excluir = set(cfg.get("excluir", []))
        self._estado = {"actualizado": None, "intervalo": self.intervalo,
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
