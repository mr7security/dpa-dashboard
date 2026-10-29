# ATENEA2 — Puntos a revisar en el código

**Para:** equipo de desarrollo / software
**Instancia:** SRVPRORPDB01 (ATENEA2) · SQL Server 2022 · 16 vCPU · 128 GB
**Mediciones:** 22–29 de octubre de 2026 · acumulado de 27 días desde el arranque
**Documento completo:** `informe-atenea2-consolidado.tex`

---

## Qué es esto, y qué no es

La instancia ATENEA2 tiene sesiones bloqueadas el **88 % del tiempo** durante la
jornada laboral. El resto del parque está entre el 0 % y el 2 %.

El diagnóstico descartó, por medición directa, que fuera un problema de
infraestructura o de configuración: paralelismo, TempDB, disco, hipervisor,
memoria del host y grupo de disponibilidad están todos correctos. **Lo que
queda está en el código y en los procesos programados.**

Este documento recoge **solo lo accionable desde desarrollo**. Quedan fuera las
tareas de administración de base de datos (subir `max server memory`, activar
`READ_COMMITTED_SNAPSHOT`, programar estadísticas), que van por otra vía.

### Cómo leer cada punto

Cada entrada lleva **qué se midió** (dato real, no estimación), **por qué
importa** y **qué mirar**. Donde hay una hipótesis sin confirmar, está dicho.

| Prioridad | Significado |
|---|---|
| 🔴 | Coste medido muy alto y causa identificada. Empezar por aquí. |
| 🟠 | Coste alto, requiere análisis funcional antes de tocar. |
| 🟡 | Mejora clara, menor impacto o mayor incertidumbre. |

---

## 🔴 1. `INSERT INTO StnERPArticuloLoteLinea` — sin parametrizar

**BD:** `Grupo`

| Medida | Valor |
|---|---|
| Variantes del mismo INSERT en caché | **6** |
| Ejecuciones (27 días) | 2.147 |
| **CPU total** | **31.000 s (8 h 36 min)** |
| CPU por ejecución | ~14 s |
| Núcleos usados por ejecución | 2,5 (plan paralelo) |
| Memoria concedida | 71 MB por ejecución |
| **Filas devueltas** | **0** |

**Por qué importa.** Es el mayor consumidor de CPU paralela de toda la
instancia. Catorce segundos de CPU repartidos entre dos núcleos y medio, para
no devolver ninguna fila.

Que la misma sentencia aparezca **seis veces** en la caché de planes significa
que **no está parametrizada**: se está construyendo el SQL concatenando
literales, así que cada variante genera su propio plan, se optimiza por
separado y ocupa su propia entrada en caché.

**Qué mirar.**

- Localizar dónde se construye ese `INSERT`. Buscar concatenación de cadenas o
  interpolación de valores directamente en el texto SQL.
- Sustituir por comandos parametrizados (`SqlParameter`, no
  `"... WHERE x = '" + valor + "'"`).
- Revisar por qué se ejecuta 2.147 veces devolviendo cero filas: si el
  `INSERT ... SELECT` no encuentra nada que insertar, conviene comprobar la
  condición antes con una consulta barata en lugar de lanzar el plan completo.

**Cómo verificar.** Tras el cambio, esa sentencia debe aparecer **una sola vez**
en `sys.dm_exec_query_stats`, y el CPU total debe caer de forma evidente.

---

## 🔴 2. `ACTUALIZA_EMPRESAS_DIRECCIONES_FICHAS_POR_PROVEEDOR`

**BD:** `Cenusa` · procedimiento almacenado

| Medida | Valor |
|---|---|
| Ejecuciones (27 días) | 336 |
| CPU total | 4.828 s |
| **Memoria concedida por ejecución** | **1.171 MB** |
| **Aprovechada** | **17,4 %** |
| **Lecturas lógicas por ejecución** | **500.544 páginas (~3,8 GB)** |

**Por qué importa.** Es **la sentencia más cara de toda la instancia**. Pide
más de un gigabyte de memoria cada vez que se ejecuta y usa menos de una quinta
parte. Esa concesión se resta del *buffer pool* mientras dura, lo que expulsa
páginas útiles de memoria y contribuye directamente al *page life expectancy*
de 28 segundos que mide la instancia.

Recorrer 3,8 GB de datos en cada ejecución apunta a recorridos completos de
tabla, no a búsquedas por índice.

**Qué mirar.**

- Predicados **no SARGables**: funciones aplicadas a la columna
  (`WHERE UPPER(campo) = ...`, `WHERE CONVERT(...) = ...`,
  `WHERE campo + '' = ...`), que impiden usar índices.
- Tipos que no casan entre la columna y el parámetro, que fuerzan conversión
  implícita y también anulan el índice.
- `DISTINCT`, `ORDER BY` o `GROUP BY` sobre conjuntos grandes, que son lo que
  dispara una concesión de memoria tan alta.
- Si procesa fila a fila con cursor, reescribir en conjunto.

**Cómo verificar.** Objetivo: bajar de 1.171 MB concedidos y de 500.544
lecturas por ejecución. Ambos se leen directamente de
`sys.dm_exec_query_stats`.

---

## 🔴 3. Los 575 disparadores de `Cenusa`

**BD:** `Cenusa` (46 GB) · **575 disparadores sobre tablas, solo 4 desactivados**

| Tabla | Disparadores | Modificaciones (27 días) |
|---|---|---|
| `entEMPRESA` | **9** | 8.759.807 |
| `entALBARAN` | **8** | 26.778.967 |
| `entMOVIMIENTO` | **8** | 5.007.080 |
| `entPEDIDO` | **8** | 24.107.314 |
| `entPEDIDO_DETALLE` | **8** | **37.738.378** |
| `entARTICULO` | 6 | 11.772.885 |

**Por qué importa, y es el punto clave de todo el informe.** Un disparador
**se ejecuta dentro de la transacción de quien lo dispara**. No es trabajo que
ocurra «después»: es tiempo durante el cual la sentencia original sigue
reteniendo sus bloqueos.

Cada `UPDATE` sobre `entPEDIDO_DETALLE` arrastra ocho disparadores antes de
poder confirmar. Con 37,7 millones de modificaciones, la amplificación es
enorme. **Esta es la vía más directa por la que el código alarga las
transacciones y genera bloqueo.**

### Los más caros, medidos

| Objeto | Ejecuciones | CPU | Detalle |
|---|---|---|---|
| `tU_entMOVIMIENTO` | 212.512 × 6 sentencias | **10.268 s** | seis sentencias en el mismo cuerpo; varias usan el 0 % de la memoria que se les concede |
| `tU_entALBARAN` | 21.365 | 2.927 s | **31.135 lecturas por disparo** (~665 M acumuladas) |
| `tU_entALBARAN` | **1.700.015** | 1.851 s | otra sentencia del mismo disparador |
| `update entFACTURA` ×2 | 432.729 c/u | — | 58 MB concedidos, **1,5 % aprovechado** |

### `AUDITORIA2`: la tabla que lo explica

| Medida | Valor |
|---|---|
| Filas | **29.708.129** |
| **Modificaciones en 9 horas** | **89.149.199** (3× su propio tamaño) |
| Muestreo de estadísticas | **1,0 %** |

Los disparadores escriben auditoría en cada modificación. Esa escritura ocurre
dentro de la misma transacción y bajo los mismos bloqueos.

**Qué mirar.**

1. **Empezar por `tU_entMOVIMIENTO` y `tU_entALBARAN`**, que son los que más
   CPU consumen. En `tU_entALBARAN`, la sentencia de 31.135 lecturas por
   disparo es el objetivo concreto: casi seguro le falta un índice o tiene un
   predicado no SARGable.
2. **Consolidar.** Ocho o nueve disparadores sobre una tabla se ejecutan en
   orden no garantizado y cada uno paga su propio coste de arranque. Fundirlos
   en uno por tabla y por operación es un trabajo mecánico y de bajo riesgo.
3. **Verificar que son de conjunto, no fila a fila.** El patrón
   `UPDATE x SET ... FROM inserted` es correcto; un cursor sobre `inserted`,
   no. Un disparador que asume una sola fila es además un error latente ante
   actualizaciones masivas.
4. **Sacar la auditoría del camino síncrono.** Si `AUDITORIA2` no necesita ser
   transaccionalmente consistente con el dato, escribirla fuera de la
   transacción (cola, Service Broker, proceso aparte) elimina de golpe una
   parte importante del tiempo de bloqueo.

**Advertencia honesta.** 575 disparadores son una decisión de arquitectura, no
un parámetro. **No se revisan en una tarde** y las decisiones sobre cuáles
tocar requieren conocimiento funcional que este análisis no tiene. Los puntos 1
y 2 son los acotados; el 4 es el que más efecto tendría y el que más análisis
exige.

---

## 🟠 4. Jobs que tardan más que su propio intervalo

De **140 planificaciones sub-diarias activas** (seis cada minuto, tres cada
diez segundos), estas se solapan consigo mismas:

| Job | Programado | Media real | Ocupación |
|---|---|---|---|
| `D365_SINCRONIZAR_OMS_DISP_MAX` | 120 s | 142 s | **118 %** |
| `D365_SYNC_TRANSFORMAR_DISP_MAX` | 180 s | 173 s | **96 %** |
| `D365_ENVIO_RECEPCIONES` | 60 s | 47 s | 78 % |
| `stnSCALE_COPIA_SHIPPING_LOAD` | 10 s | 7 s | 70 % |
| `D365_SINCRONIZAR_DATOS_COMPRAS` | 300 s | 200 s | 67 % |

**Por qué importa.** `D365_SINCRONIZAR_OMS_DISP_MAX` está programado cada dos
minutos y tarda dos minutos y veintidós segundos de media. El «cada dos
minutos» es ficción: SQL Agent no solapa un job consigo mismo, así que
**arranca de nuevo en cuanto termina, de forma continua, todo el día**.

**Qué mirar.** Estos procesos muy probablemente reprocesan todo el conjunto en
cada pasada. Lo que hay que revisar:

- ¿Hay marca de agua (`rowversion`, fecha de modificación, tabla de
  pendientes) para procesar **solo lo que cambió**?
- ¿Se puede hacer idempotente e incremental en lugar de completo?
- Si el volumen justifica los 142 s, entonces el intervalo de 2 minutos es el
  que está mal y hay que negociarlo con negocio.

**Cómo verificar.** La consulta de ocupación está en el informe completo
(sección 8). Objetivo: por debajo del 50 % en todos.

---

## 🟠 5. `entEFECTO_FacturasCobradas365` — vaciar y recargar

**BD:** `Grupo`

| Medida | Valor |
|---|---|
| Filas | 1.184.877 |
| **Modificaciones en 9 horas** | **31.476.588** (26× su tamaño) |

Jobs implicados: `D365 CARGAR FACTURAS COBRADAS` (cada hora),
`ACTUALIZAR_FECHACOBRO_EFECTOS_DESDE_CONCILIADOS` (cada 30 min),
`Limpiar Fechas Efectos` (cada hora).

**Por qué importa.** Veintiséis veces el tamaño de la tabla en cambios, en
nueve horas. Es el patrón clásico de tabla de paso que se vacía y recarga
entera. Dos consecuencias medidas:

1. Las estadísticas de esa tabla **nunca son representativas**. El umbral de
   actualización automática se cruza en minutos, y eso lleva al optimizador a
   estimar mal y elegir planes paralelos que no hacen falta. El paralelismo
   supone el **58,7 %** de todo el tiempo de espera de la instancia.
2. Es la candidata más probable al bloqueo de esquema de **392 segundos** que
   se observó —seis minutos y medio con una tabla inaccesible—, porque
   `TRUNCATE TABLE` toma un bloqueo `Sch-M` incompatible con cualquier lectura.
   **Esto es una hipótesis, no está confirmado**: demostrarlo requiere capturar
   el *blocked process report*.

**Qué mirar.** Sustituir el vaciado completo por una carga incremental
(`MERGE` o inserción/actualización solo de lo que cambió). Si el `TRUNCATE` es
imprescindible, al menos acotarlo a una ventana sin usuarios.

---

## 🟠 6. Transacciones abiertas sin cerrar

**Medido:** 10 sesiones en estado `sleeping` **con transacción abierta**,
reteniendo bloqueos sin ejecutar nada.

**Por qué importa.** Una sesión dormida con transacción abierta mantiene sus
bloqueos indefinidamente mientras la aplicación «piensa», espera una respuesta
del usuario o hace una llamada de red. Es el peor caso posible: bloquea sin
estar haciendo trabajo.

**Qué mirar.**

- Transacciones que abarcan interacción con el usuario o llamadas externas
  (HTTP, servicios, colas). La transacción debe abrirse lo más tarde posible y
  cerrarse lo antes posible.
- Rutas de error sin `ROLLBACK`: una excepción que escapa sin cerrar la
  transacción deja la sesión así.
- Uso de `TransactionScope` sin `Complete()` en algún camino.
- Confirmar que el `using`/`finally` cierra la conexión en todos los casos.

---

## 🟡 7. Consultas que trabajan mucho para no devolver nada

| Sentencia | BD | Ejecuciones | CPU | Núcleos | Filas |
|---|---|---|---|---|---|
| `UPDATE stnERPORDENPREPARACION SET Estado = CASE ...` | Grupo | 1.122 | 1.340 s | **3,6** | **0** |
| `UPDATE stnERPORDENPREPARACION SET Estado = 2 WHERE idorden NOT IN (...)` | Grupo | 1.122 | 919 s | 2,8 | **0** |
| `SELECT a.SourceKey FROM OMS..InventTransOriginBiEntities ...` | OMS | 164 | 581 s | 2,0 | **1** (con **506 MB** concedidos) |

**Por qué importa.** Son planes paralelos, con concesiones de memoria grandes,
que acaban devolviendo cero o una fila. El `NOT IN (SELECT ...)` del segundo
caso es un patrón conocido de mal rendimiento (y de comportamiento sorprendente
si la subconsulta devuelve `NULL`): conviene sustituirlo por `NOT EXISTS`.

El tercero —506 MB de memoria concedida para devolver **una fila**— es el
ejemplo más claro de estimación de cardinalidad equivocada de todo el informe.

**Qué mirar.** Comprobar primero si hay algo que hacer con una consulta barata
antes de lanzar la actualización completa. Y revisar `NOT IN` → `NOT EXISTS`.

---

## 🟡 8. `DELETE` cruzado guiado por `NOLOCK`

```sql
DELETE b FROM [D365Eventos]..STNAppSuiteInventTrans a WITH (NOLOCK)
INNER JOIN ...
```

591 y 597 ejecuciones, ~2.000 s y ~1.255 s de CPU, planes paralelos.

**Por qué importa.** Más allá del coste, hay un problema de corrección:
`WITH (NOLOCK)` permite **lecturas sucias** —filas no confirmadas, filas leídas
dos veces o ninguna por movimiento de páginas—. Usar eso para decidir **qué
borrar** significa que se pueden estar borrando filas a partir de datos que
nunca llegaron a existir.

**Qué mirar.** Quitar el `NOLOCK` de la rama que determina el borrado. Si se
puso para evitar bloqueos, la solución correcta es `READ_COMMITTED_SNAPSHOT`
(tarea de DBA, ya planificada), no leer sucio.

---

## 🟡 9. Índice ausente de mayor impacto

| Tabla | Columna sugerida | Impacto estimado | Búsquedas (27 días) |
|---|---|---|---|
| `Grupo.cmCargaConfirmadaLogs` | `[TipoAccion]` | **45.515.122.075** | **14.889.907** |

Es **56 veces** mayor que el siguiente candidato de la lista.

**Por qué importa.** Casi quince millones de búsquedas en 27 días sobre una
tabla sin índice adecuado significa recorridos completos repetidos. Y un
recorrido completo es justo lo que dispara un plan paralelo.

**Qué mirar.** Dos preguntas, en este orden:

1. **¿Por qué se consulta esa tabla 15 millones de veces?** Si es una tabla de
   log consultada en un bucle, el arreglo bueno está en el código, no en el
   índice.
2. El índice en sí **no debe aplicarse tal cual**. Las sugerencias de
   `dm_db_missing_index_*` son heurísticas: las produce el optimizador sobre
   planes sueltos, sin tener en cuenta los índices que ya existen ni el coste
   de mantenerlos en escritura. Un índice de una sola columna de baja
   cardinalidad como `[TipoAccion]` rara vez es el definitivo correcto. Hay que
   diseñarlo a partir de las consultas reales.

---

## 🟡 10. Tiempos de espera del cliente

Se observaron esperas de **200 segundos** en conexiones
`.Net SqlClient Data Provider` desde puestos de usuario, y un caso de
**392 segundos**.

**Por qué importa.** Eso no se percibe como lentitud: se percibe como que la
aplicación se ha colgado. El usuario cierra, vuelve a entrar, y **añade otra
sesión a la misma cola**. Es un bucle de realimentación que empeora el atasco.

**Qué mirar.**

- `CommandTimeout` explícito y razonable en las operaciones interactivas. El
  valor por defecto de 30 s es mejor que esperar 200.
- Mensaje al usuario cuando se agota, en lugar de una pantalla congelada.
- Reintento con espera creciente solo donde la operación sea idempotente.

---

## Lo que NO hay que tocar

Para que nadie persiga fantasmas. Todo esto se **midió** y está correcto:

| Hipótesis | Resultado |
|---|---|
| Configuración de paralelismo | MAXDOP 8 sobre 16 núcleos, *cost threshold* 100. Correcto |
| Estimador de cardinalidad | Nivel de compatibilidad 160, estimador moderno |
| Dimensionamiento de TempDB | 8 ficheros, 99 % libre |
| Almacenamiento | Peor latencia 9 ms; discos del host por debajo de 1,5 ms |
| Hipervisor | *Ready time* 0,09 %, sin *ballooning*, host al 40 % |
| Grupo de disponibilidad | `ASYNCHRONOUS_COMMIT`, retraso 0 s |
| Falta de CPU u otro proceso | Procesos ajenos a SQL Server: 3–6 % |

**El hardware no es el problema. La configuración tampoco.**

---

## Cómo medir si ha funcionado

| Indicador | Hoy | Objetivo |
|---|---|---|
| % de muestras con bloqueo (7 días) | 88 % | < 30 % |
| Media de sesiones bloqueadas | 4,9 | < 1 |
| Pico a las 08:00 | 9,0 | < 3 |
| Esperas `CX*` sobre el total | 58,7 % | < 30 % |
| *Page life expectancy* medio | 124 s | > 300 s |

Las consultas que producen estas cifras están en la sección 8 del informe
completo, y son todas de solo lectura.

**Una regla:** un cambio cada vez, con 48 horas de observación entre uno y
otro. Si se tocan varias cosas el mismo día, el resultado será mejor o peor
pero nadie sabrá por qué, y no habrá forma de deshacer solo la parte que falló.

---

## Un apunte sobre el método

La tesis de este análisis cambió tres veces. La primera sospecha fue
«paralelismo mal configurado» y resultó **falsa**: estaba bien configurado. Lo
que la sustituyó —paralelismo *innecesario*, provocado por estimaciones
erróneas— explica los mismos síntomas pero tiene un remedio completamente
distinto.

Por eso cada punto de este documento lleva su medición y su forma de
verificarlo. **Las recomendaciones valen lo que valen las mediciones que las
sostienen**, no la plausibilidad del razonamiento. Si al abrir el código algo
no encaja con lo que aquí se dice, el código manda: avisad y lo revisamos.
