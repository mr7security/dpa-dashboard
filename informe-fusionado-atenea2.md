# ATENEA2 — Diagnóstico consolidado

**Fusión de dos auditorías independientes sobre la misma instancia**

| | Auditoría A | Auditoría B |
|---|---|---|
| **Título** | Diagnóstico de bloqueos en la instancia ATENEA2 | Auditoría de bloqueos ERP STN |
| **Autor** | Departamento de IT (infraestructura) | Equipo de software |
| **Fecha** | 29/10/2026 | 29/10/2026 |
| **Acceso** | DPA, DMV, vCenter. **Sin código fuente** | Repositorio `00_00_01_ERPGrupoSTN` + DMV |
| **Alcance de BD** | Instancia completa; profundiza en `Grupo` y `Cenusa` | Base `Grupo` y el flujo pedido → OC → carga → albarán |
| **Fuerte en** | Plataforma, hipervisor, memoria, disco, `Cenusa` | Mecanismo, código, triggers, transacciones |

Las dos se hicieron **sin conocimiento una de la otra**. Este documento las
contrasta y se queda con lo mejor de cada una.

---

## 1. Lo primero: qué dice la convergencia

Siete mediciones hechas por separado, con horas de diferencia:

| Medida | Auditoría B | Auditoría A |
|---|---|---|
| `entEMPRESA` — espera por fila | 83.509 s | 83.106 s |
| `entEMPRESA` — espera por página | 53.332 s | 55.258 s |
| Escalados a bloqueo de tabla, `entPEDIDO` | 16.560 | 18.464 |
| Escalados, `entPEDIDO_DETALLE` | 12.541 | 14.044 |
| Escalados, `entMOVIMIENTO` | 10.190 | 11.281 |
| `D365_SINCRONIZAR_OMS_DISP_MAX` | 134 s cada 2 min | 142 s cada 120 s |
| `D365_SYNC_TRANSFORMAR_DISP_MAX` | 160 s cada 3 min | 173 s cada 180 s |

Las diferencias son el desfase entre tomas. **Dos análisis independientes que
convergen así no se discuten: se ejecutan.**

Coinciden además, por caminos distintos, en estas cinco conclusiones:

1. **RCSI desactivado** es un problema estructural, y los `NOLOCK` repartidos
   son un parche que añade lecturas sucias.
2. **Hay jobs que duran más que su propio intervalo** y por tanto no paran nunca.
3. **`TRUNCATE` frecuente** provoca bloqueos de esquema que encolan lecturas.
4. **El SQL no está parametrizado** y la caché de planes está llena de planes
   de un solo uso.
5. **`CommandTimeout = 0`** en la aplicación convierte un bloqueo en una
   pantalla congelada para siempre.

---

## 2. Dónde la auditoría B corrige a la A

Esto es lo más importante del contraste. **En la causa raíz, B tiene razón y A
se equivocó.**

### 2.1 El paralelismo no viene de las estadísticas: viene de barridos reales

**Lo que decía A.** Estadísticas permanentemente obsoletas → estimaciones de
cardinalidad infladas → el optimizador elige planes paralelos que no hacen
falta. Evidencia: `entEFECTO_FacturasCobradas365` con 26× su tamaño en
modificaciones y muestreo al 11,8 %; consultas paralelas devolviendo cero filas.

**Lo que encuentra B.** El 64 % del tiempo de espera es paralelismo porque hay
**barridos masivos literales** en el código:

| Objeto | Lecturas por ejecución | Frecuencia |
|---|---|---|
| `STN_ERP_CARGAR_ANALISIS_VENTAS_DIARIO` | **108 M** | cada 15 min, 06–23 h |
| `stn_actualizar_codigo_unico` | 10,2 M | cada 15 min, 08:00–16:45 |
| `STNtI_entALBARAN` | 32.027 por albarán | en cada inserción |
| `CARGAR_CARTERA` | 649.588 | desde un trigger |

**Veredicto: la explicación de B es mejor.** Es más parsimoniosa y está
apoyada en objetos con nombre que hacen recorridos completos. La de A requería
una cadena inferencial de tres pasos para llegar al mismo sitio.

**Lo que sigue siendo válido de A:** el muestreo de estadísticas al 1–1,4 % en
tablas de millones de filas es real y conviene corregirlo. Pero es un factor
agravante, no el motor.

### 2.2 `entEMPRESA`: la causa no son los jobs

**A decía:** tres jobs (`BLOQUEAR EMPRESAS CON IMPAGOS`, su gemelo `MAIL`, y
`DesbloquearEmpresasSaldoNegativo`) reescriben la tabla cada 30 minutos.

**B encuentra:** `CALCULO_RIESGO`, invocado **desde los triggers** de
`entPEDIDO` y `entPEDIDO_DETALLE`, **4.784.173 veces en 27 días**, escribiendo
en `entEMPRESA.stnRiesgoPedidos` en cada cambio de línea. Y si el riesgo se
supera, hace **`ROLLBACK TRANSACTION` dentro del trigger**.

**Veredicto: B tiene razón.** Los jobs contribuyen, pero el volumen está en los
triggers. A midió el efecto y atribuyó mal la causa.

### 2.3 El bloqueo de esquema: mecanismo correcto, objeto equivocado

A planteó como **hipótesis no confirmada** que el bloqueo `Sch-M` de 392 s
viniera de un `TRUNCATE` sobre una tabla de paso, y señaló
`entEFECTO_FacturasCobradas365`.

B lo confirma y lo concreta: `stnSCALE_COPIA_SHIPPING_LOAD` trunca
`SHIPPING_LOAD_COPIA` **cada 10 segundos dentro de una transacción**, y
`PREVISION_CARGA_CARGADATOS` trunca cuatro tablas cada 3 minutos.
`LCK_M_SCH_M` acumula **25.761 esperas con un máximo de 4,3 horas**.

**Veredicto: mecanismo acertado, tabla equivocada.** Y el problema real es
mucho mayor de lo que A midió: 4,3 horas frente a 392 segundos.

---

## 3. Lo que solo tiene la auditoría B

Todo esto requiere leer el código. No es deducible desde las DMV.

### 3.1 La cascada de triggers, con la prueba más limpia del conjunto

En `entPEDIDO`, el trigger que sella `modified_date_time` se ejecutó
**1.187.673 veces** y los demás triggers de UPDATE de la tabla **2.358.453**:
exactamente el doble. Porque el trigger hace un segundo `UPDATE` sobre la
misma fila que vuelve a dispararlos todos.

Hay **744 triggers en `Grupo`** (703 activos), 96 sobre las 21 tablas del flujo.

### 3.2 Transacciones que esperan a que el usuario escriba

`CONFSolicitud.vb:4589` abre la transacción. En la línea **4644 muestra un
`InputBox`** pidiendo un comentario. El `COMMIT` llega en la 4700.

**Esto explica un hallazgo de A que había quedado sin causa:** A midió
*«10 sesiones dormidas con transacción abierta»* y solo pudo recomendar
«revisad transacciones que abarcan interacción con el usuario». B encontró la
línea exacta.

### 3.3 Transacciones SERIALIZABLE por accidente

`New TransactionScope()` sin opciones equivale a **SERIALIZABLE** en .NET.
Durante la revisión había **8 sesiones en nivel de aislamiento 4**, y el nivel
se queda pegado a la conexión del *pool*.

### 3.4 Numeración con `MAX()+1`

En pedidos, OC, albaranes y en `STN_TRASPASAR_ALBARANES2`. Dos usuarios leen
el mismo máximo y uno acaba en interbloqueo.

### 3.5 Tres tablas enormes sin índice agrupado

`entMOVIMIENTO` (50 M de filas, **89 GB**), `entMOVIMIENTO_LOTE` (50 M) y
`entALBARAN` (3,3 M) son **montículos**. A nunca comprobó esto: es un fallo de
cobertura.

### 3.6 Correo electrónico dentro de la transacción del usuario

`sp_send_dbmail` invocado desde `STNtU_entORDENCARGA`, después de que
`CARGAR_CARTERA` borre y recargue una tabla entera.

### 3.7 Seguridad

La cadena de conexión principal usa **`sa` con la contraseña en claro en
`app.config`**, versionada en el repositorio. Y el SQL se construye
concatenando texto con campos que escribe el usuario (`EXPPrincipal.vb:925`):
riesgo de inyección.

### 3.8 Un histórico de bloqueos que ya existía

`Grupo.dbo.RUMTRANSACCIONES`, alimentada por el job `REVISAR BLOQUEOS`:
**60 días, 6.221 muestras**. A observó que ese job existía y dedujo que *«el
equipo ya conocía el problema»*, pero no usó sus datos.

---

## 4. Lo que solo tiene la auditoría A

### 4.1 La plataforma está descartada, y eso cierra discusiones

B no menciona hipervisor, disco ni grupo de disponibilidad. A los midió:

| Hipótesis | Medición | Veredicto |
|---|---|---|
| Contención de CPU del hipervisor | *Ready time* 0,09 %, *co-stop* 0 % | Descartado |
| Presión de memoria del host | *Ballooning* 0, intercambio 0, 128 GB concedidos | Descartado |
| Host sobrecargado | CPU 40 %, memoria 39 % | Descartado |
| Disco (invitado) | Peor latencia 9 ms; escrituras de log 0–2 ms | Descartado |
| Disco (host) | Los 7 discos de fibra por debajo de 1,5 ms | Descartado |
| AG en confirmación síncrona | `ASYNCHRONOUS_COMMIT`, retraso 0 s | Descartado |
| Proceso ajeno consumiendo CPU | 3–6 % | Descartado |

**Sin esto, cualquiera puede decir «será la máquina» y perder dos semanas.**

### 4.2 Hay 25 GB de RAM sin usar — y es gratis

B cita el PLE de 85 s y el tope de 96 GB, pero no detecta que:

- El servidor tiene **131.071 MB** físicos y el tope está en **96.896 MB**.
- Quedan **25.657 MB libres**.
- El sistema operativo informa *«Available physical memory is high»*.
- El hipervisor confirma que esa memoria es real (sin *ballooning*).

**Subir `max server memory` a unos 107 GB es gratis, no requiere parada, es
reversible y sube el PLE.** Es la acción de mejor relación esfuerzo/beneficio
de las dos auditorías, y no está en el plan de B.

### 4.3 `Cenusa` no está cubierta por B

El alcance de B es la base `Grupo`. `Cenusa` es otra base de 46 GB en la misma
instancia, con:

- **575 disparadores** sobre tablas (4 desactivados). Nueve sobre `entEMPRESA`,
  ocho sobre `entPEDIDO_DETALLE`.
- `AUDITORIA2`: **29,7 M de filas con 89 M de modificaciones en nueve horas**,
  muestreada al 1 %.
- `ACTUALIZA_EMPRESAS_DIRECCIONES_FICHAS_POR_PROVEEDOR`: **1.171 MB de
  concesión de memoria por ejecución, aprovechados al 17,4 %**, y 500.544
  lecturas por vez.

**Sumando: más de 1.300 triggers entre las dos bases.**

### 4.4 Un índice ausente que B no menciona

`Grupo.cmCargaConfirmadaLogs` sobre `[TipoAccion]`: **14,9 millones de
búsquedas** en 27 días, impacto estimado 56 veces mayor que el siguiente
candidato de la lista.

---

## 5. Plan de acción unificado

Reordenado por relación entre esfuerzo, riesgo y efecto medido. **Un cambio
cada vez, con 48 horas de observación entre uno y otro.**

### Fase 0 · Medir antes de tocar (1–3 días, sin riesgo)

| | Acción | Origen |
|---|---|---|
| 0.1 | Activar **Query Store** en `Grupo` | B |
| 0.2 | **Extended Events** con `blocked_process_report` (umbral 10 s) y `xml_deadlock_report` | B |
| 0.3 | **`Application Name`** distinto por aplicación — hoy todo es `.Net SqlClient Data Provider` | B |
| 0.4 | Activar **`optimize for ad hoc workloads`** | B |

### Fase 1 · Rápido, reversible, sin parada (1–2 semanas)

| | Acción | Origen | Efecto esperado |
|---|---|---|---|
| 1.1 | **Subir `max server memory` a ~107 GB** | **A** | PLE de 85 s a >300 s. Gratis |
| 1.2 | Limitar a `inserted` el `UPDATE` de `STNtI_entALBARAN` | B | Deja de recorrer todos los albaranes desde 2019 |
| 1.3 | Sacar `STN_CORREO_PROBLEMAS_CARGAS_POROC_VR2` del trigger | B | Elimina correo y recarga de cartera de la transacción |
| 1.4 | `ACTUALIZAR CODIGO UNICO`: condición de cambio y fuera de horario | B | Libera `entARTICULO` en jornada |
| 1.5 | Análisis de ventas a la **réplica secundaria** o a la noche | B | Quita 108 M de lecturas cada 15 min |
| 1.6 | `TransactionScope` a `ReadCommitted` con *timeout* | B | Fin de las sesiones SERIALIZABLE accidentales |
| 1.7 | Sacar `InputBox` y correo de la transacción de solicitud de carga | B | Fin de las transacciones que esperan al usuario |
| 1.8 | `Using` en conexiones, *timeouts* finitos (30–120 s) | B | Fin de las pantallas congeladas |
| 1.9 | Filtro `TRIGGER_NESTLEVEL` y salida temprana sin filas | B | Corta la mitad de las ejecuciones de trigger |
| 1.10 | **`LOCK_ESCALATION = DISABLE`** en `entEMPRESA`, `entORDENCARGA`, `entALMACEN_LOTE` | A | Medida intermedia antes de RCSI |

### Fase 2 · Diseño acotado (1–2 meses)

| | Acción | Origen |
|---|---|---|
| 2.1 | **Activar `READ_COMMITTED_SNAPSHOT`** en `Grupo` tras prueba | **A + B** |
| 2.2 | `CALCULO_RIESGO` asíncrono por cola de clientes | B |
| 2.3 | Asignador de números común (`SEQUENCE`) en todos los módulos | B |
| 2.4 | Generación de albaranes en un procedimiento único, en conjunto | B |
| 2.5 | Procedimientos de guardado de línea y de generación de OC | B |
| 2.6 | Recálculo de colas por cola y día; reserva de muelle atómica | B |
| 2.7 | Publicación de tablas auxiliares por `SWITCH` en vez de `TRUNCATE` | B |
| 2.8 | Estadísticas `FULLSCAN` varias veces al día en las tablas de alta rotación | A |
| 2.9 | Índice de `cmCargaConfirmadaLogs`, diseñado desde las consultas reales | A |
| 2.10 | **Análisis propio de `Cenusa`** — no cubierta por ninguna de las dos a fondo | A |

### Fase 3 · Estructural (un trimestre)

| | Acción | Origen |
|---|---|---|
| 3.1 | Un solo trigger por tabla y evento | B |
| 3.2 | Integraciones y avisos por colas fuera de la transacción del usuario | B |
| 3.3 | **Índices agrupados** en `entMOVIMIENTO`, `entMOVIMIENTO_LOTE`, `entALBARAN` | B |
| 3.4 | Lecturas de pantallas e informes a la réplica secundaria | B |
| 3.5 | Revisar los 575 disparadores de `Cenusa` y sacar `AUDITORIA2` del camino síncrono | A |
| 3.6 | Sustituir `sa` por una cuenta de permisos mínimos y rotar la contraseña | B |

---

## 6. Cómo medir si funciona

| Indicador | Hoy | Objetivo | Fuente |
|---|---|---|---|
| % de muestras con bloqueo (7 días) | 88 % | < 30 % | Panel |
| Media de sesiones bloqueadas | 4,9 | < 1 | Panel |
| Pico a las 08:00 | 9,0 | < 3 | Panel |
| Esperas `CX*` sobre el total | 58–64 % | < 30 % | `dm_os_wait_stats` |
| *Page life expectancy* | 25–124 s | > 300 s | Panel |
| Espera de bloqueo en `entEMPRESA` | 83.509 s | < 20.000 s | `dm_db_index_operational_stats` |
| Ejecuciones de `CALCULO_RIESGO` | 4,78 M / 27 d | < 100.000 | `dm_exec_procedure_stats` |
| Planes ad-hoc en caché | 97 % | < 50 % | `dm_exec_cached_plans` |

El panel de `http://10.50.69.121:9000` ya muestra en vivo los cinco primeros.

---

## 7. Lectura honesta del contraste

**La auditoría B es mejor que la A donde más importa**, y conviene decirlo sin
adornos: identificó el mecanismo, lo localizó en el código con número de línea
y propuso correcciones concretas. En los dos puntos donde las dos discrepan
—el origen del paralelismo y la causa del bloqueo de `entEMPRESA`— la
explicación de B está mejor sostenida.

**La auditoría A aporta tres cosas que B no tiene**, y ninguna es menor:
descarta la plataforma entera por medición (lo que evita semanas de
especulación), encuentra 25 GB de memoria sin usar que son la mejora más
barata disponible, y cubre `Cenusa`, que queda fuera del alcance de B.

**Y hay una lección de método.** La tesis de A cambió tres veces durante su
elaboración, y la primera versión —«paralelismo mal configurado»— resultó
falsa. Ahora el contraste con B corrige una cuarta. Eso no invalida el
trabajo: lo que invalida un diagnóstico es no poder comprobarlo. Las dos
auditorías dejan por escrito la consulta que produce cada cifra, y ese es el
motivo por el que se pueden fusionar en lugar de tener que elegir entre ellas.

**Si al aplicar algo de aquí la realidad no coincide con lo escrito, manda la
realidad.**
