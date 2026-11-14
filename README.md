# Datamart + dashboard de Presupuesto Público (MEF · SIAF)

Convierte los archivos de **Presupuesto y Ejecución de Gasto / Ingreso** del MEF en el datamart dimensional
descrito en [`docs/modelo_objetivo.md`](docs/modelo_objetivo.md) (parquet) y en un dashboard **Dash** que responde en
milisegundos, con **DuckDB** como motor. Pensado para volúmenes grandes en una sola máquina.

```
 tus parquet / tablas DuckDB       ETL (DuckDB)                    warehouse/ (parquet)                  Dashboard (Dash)
┌──────────────────────────┐  ┌──────────────────────────┐  ┌────────────────────────────────┐  ┌──────────────────────┐
│ 2012-Gasto … 2026-Gasto  │  │ 1 dims   (DISTINCT crudo)│  │ dim_*  (6 dimensiones del      │  │ carga SOLO los cubos │
│ 2012-Ingreso … 2026-…    │─▶│ 2 facts  (por año)       │─▶│         modelo)                │─▶│ en RAM (~400 MB)     │
│ TB_UBIGEOS.csv           │  │ 3 cubos  (agregados)     │  │ fact_gasto / fact_ingreso      │  │ SQL en ms + caché    │
│ (decenas de millones de  │  │ 4 manifest + vistas      │  │ ext_entidad  (apoyo, fuera del │  │ mapa de distritos    │
│  filas, GBs)             │  └──────────────────────────┘  │         modelo) · cube_* ·     │  │ recarga sola         │
└──────────────────────────┘                                │ mef.duckdb (vistas, ad-hoc)    │  └──────────────────────┘
                                                            └────────────────────────────────┘
```

**Idea central de rendimiento:** el dashboard *nunca* toca los hechos (decenas de millones de filas); consulta cubos
ya agregados (miles a pocos millones de filas) cargados en memoria. Los hechos quedan disponibles en parquet/`mef.duckdb`
para análisis ad-hoc.

---

## El modelo

Es el de tu README: **dos hechos** (`FACT_INGRESO`, `FACT_GASTO`) que comparten **dimensiones conformadas**
(`DIM_TIEMPO`, `DIM_ENTIDAD`, `DIM_UBIGEO`, `DIM_FINANCIAMIENTO`) y tienen las suyas
(`DIM_CLASIF_INGRESO` | `DIM_CLASIF_GASTO` + `DIM_FUNCIONAL`). Las tablas publicadas tienen **exactamente** las columnas
del modelo, en su orden (lo comprueba `python -m scripts.check_spec --strict` y un test).

| Tabla | Grano | Columnas |
|---|---|---|
| `dim_tiempo` | un mes (incluye el mes 0 de apertura) | `sk_tiempo, anio, mes` |
| `dim_entidad` | unidad ejecutora (`SEC_EJEC`) | `sk_entidad, pliego, ejecutora` |
| `dim_ubigeo` | distrito donde se ubica la entidad | `sk_ubigeo, codigo_ubigeo, departamento, provincia, distrito, latitud, longitud` |
| `dim_financiamiento` | fuente + rubro + tipo de recurso | `sk_financiamiento, fuente, rubro, tipo_recurso` |
| `dim_clasif_ingreso` / `dim_clasif_gasto` | específica de detalle (clasificadores **distintos**) | `sk_…, generica, subgenerica, subgenerica_det, especifica, especifica_det` |
| `dim_funcional` | **meta** presupuestal | `sk_funcional, funcion, division_funcional, grupo_funcional, proyecto, inversion, meta` |
| `fact_ingreso` | mes × entidad × ubicación × financiamiento × clasificador | `sk_tiempo, sk_entidad, sk_ubigeo, sk_financiamiento, sk_clasif_ingreso, monto_pia, monto_pim, monto_recaudado` |
| `fact_gasto` | … × meta | `sk_tiempo, sk_entidad, sk_ubigeo_ejec, sk_financiamiento, sk_clasif_gasto, sk_funcional, monto_pia, monto_pim, monto_devengado, monto_girado` |

**Fuera del modelo** (no cuentan como columnas del modelo): la carpeta física `anio=YYYY` de las hechos (partición por año),
los `cube_*` (agregados de rendimiento, se pueden borrar y regenerar) y **`ext_entidad`**, una tabla de apoyo con el nivel de
gobierno, sector y códigos de la entidad (`sk_entidad` igual que en `dim_entidad`). El modelo no contempla el nivel de gobierno,
pero el dashboard lo filtra; por eso vive aparte y no ensucia el modelo.

### Dónde el proyecto se aparta, a propósito, del texto del modelo

| Punto | Modelo | Proyecto | Por qué |
|---|---|---|---|
| Claves sustitutas | `INT` | hash de 64 bits, **determinista** | permite cargar año por año y reconstruir un año sin tablas de mapeo ni renumerar |
| Montos | `FLOAT` | `DECIMAL(18,2)` | los `FLOAT` suman con error de redondeo; los totales deben cuadrar al centavo |
| `latitud`, `longitud` | `VARCHAR(50)` | `DOUBLE` | son números (la tabla de ubigeos trae coma decimal: se convierte) y los mapas los necesitan numéricos |
| `sk_funcional` | se genera de `SEC_EJEC + SEC_FUNC` | `AÑO + SEC_EJEC + SEC_FUNC` | `META` es "secuencial desde 00001" según el diccionario del MEF: no se sabe si se reasigna cada año. Sin el año, una meta reasignada atribuiría mal las funciones de los años viejos; con el año nunca se equivoca. `etl.validate` mide cuántas metas cambian entre años |
| `dim_tiempo.mes` | "del 1 al 12" | 0 a 12 | el propio modelo registra `monto_pim` de gasto en el "mes 0 (apertura)": sin esa fila las hechos quedarían huérfanas |
| `dim_ubigeo` | — | solo las ubicaciones que aparecen en los datos del MEF | el modelo la define como "ubicación de la entidad"; las coordenadas vienen de la tabla de ubigeos |

Decisión de diseño sobre la conversión **acumulado → flujo** (Paso 3): se hace al **grano nativo** (el más fino que publica el
MEF, incluyendo columnas que el modelo no conserva, como el programa presupuestal) y *después* se agrega al grano del modelo. Sumar
primero y restar después crea saltos artificiales cuando una serie desaparece unos meses; hay un test con un caso hecho a mano
(110, 50, 80 en vez de 110, 40, 90).

---

## Paso 0 · Requisitos e instalación

- Python 3.10+ (probado en 3.12). Unos GB de RAM libres; DuckDB derrama a disco si hace falta.

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt  # (o requirements.txt si no vas a correr tests)
python -m pytest                     # 80 tests: debe terminar en verde
```

## Paso 1 · Pruébalo con datos de muestra (5 minutos)

Antes de tocar tus datos reales, confirma que todo funciona con datos **sintéticos** (mismo esquema que el MEF, montos
inventados, pero ubicados en **distritos reales** tomados de la tabla de ubigeos):

```bash
python -m scripts.make_sample_data --out data/raw --years 2023 2024 --entidades 300
python -m etl.profile      # diagnóstico
python -m etl.run          # construye el warehouse
python -m etl.validate     # controles de calidad
python -m scripts.check_spec --strict   # ¿coincide con el modelo del equipo?
python -m dashboard.app    # abre http://127.0.0.1:8050
```

Los datos de muestra imitan lo que documenta tu README (`--style spec`): en gasto, PIA/PIM solo en el **mes 0**; en ingreso,
PIA una sola vez y PIM como **modificaciones** del periodo. Con `--style snapshot` se generan PIA/PIM repetidos cada mes, y con
`--ubigeo-style 4-6` los códigos de ubicación en la otra convención posible. Los `WARN` de "cuadre PIM" y de "1 ubicación sin
coordenadas" son **esperados** (los datos sintéticos no están balanceados y 19 distritos de la tabla no tienen coordenadas).

Cuando termines, borra lo sintético: `rm -rf data/raw/gasto/* data/raw/ingreso/* warehouse/*`.

## Paso 2 · Conecta tus datos reales

Copia `.env.example` a `.env` y elige **una** de estas dos formas:

**A) Parquet en disco** (lo más simple y rápido: DuckDB lee solo las columnas y años necesarios):

```ini
MEF_RAW_GASTO=D:/mef/parquet/gasto/*.parquet
MEF_RAW_INGRESO=D:/mef/parquet/ingreso/*.parquet
```

**B) Tablas dentro de un `.duckdb` que ya tienes** (se adjunta en solo lectura; no se modifica):

```ini
MEF_ATTACH=raw=D:/mef/mi_base.duckdb
MEF_RAW_GASTO=raw.gasto
MEF_RAW_INGRESO=raw.ingreso
```

Requisitos del origen:
- Las columnas deben llamarse como en el diccionario del MEF (mayúsculas o minúsculas da igual). Si las tuyas se llaman
  distinto, crea una **vista** con los nombres del diccionario y apunta a ella. `etl.run` y `etl.profile` dicen cuáles faltan.
- `ANO_EJE`/`MES_EJE` (gasto) y `ANO_DOC`/`MES_DOC` (ingreso) son el año y mes; el ETL los unifica en `sk_tiempo = año*100 + mes`.
- **No mezcles `*-Mensual` con `*-Diario`** (2025–2026): duplicarías los montos. Con parquet, el ETL ignora por defecto todo archivo
  cuyo nombre contenga "diario" (`MEF_EXCLUDE_FILES_REGEX`). Con tablas (opción B) no hay nombre de archivo: carga solo los mensuales.
- La **tabla de ubigeos** (`data/ref/TB_UBIGEOS.csv`, separador `;`, coma decimal) ya viene incluida; si usas otra, `MEF_UBIGEO_TABLE`.

## Paso 3 · ⚠️ Tres decisiones que tus datos reales deben confirmar

`python -m etl.profile` las mide por ti. **No pude descargar los archivos reales del MEF para comprobarlas**, así que el proyecto
no las asume: las diagnostica.

### 3a · `MEF_EXEC_MODE`: ¿los montos de ejecución vienen acumulados o como flujo?

El diccionario no dice si `MONTO_DEVENGADO`, `MONTO_GIRADO` (gasto) y `MONTO_RECAUDADO` (ingreso) vienen *acumulados al mes* o como
*flujo del mes*; y el MEF publica otros datasets con convenciones distintas. Si te equivocas, las cifras salen ~6 veces mal.

1. **`➜ Sugerencia: MEF_EXEC_MODE=…`**: test automático: mira si las series (misma clave, meses distintos) *bajan* alguna vez. Un
   acumulado casi nunca baja (`cumulative`); un flujo sí (`flow`).
2. **Prueba definitiva (2 minutos):** el profile imprime la *suma del último mes* y la *suma de todos los meses*. Compárala con el
   devengado total del mismo año en **Consulta Amigable**: si coincide con el último mes → `cumulative`; con la suma de los 12 → `flow`.

Con `cumulative` el ETL convierte a flujo mensual (`flujo(mes) = acumulado(mes) − acumulado(mes anterior presente de esa serie)`); con
`flow` lo deja igual. Desde ahí todo trabaja con flujos: "acumulado a junio" es `SUM(mes <= 6)`.

### 3b · `MEF_BUDGET_MODE_*`: ¿cómo vienen PIA y PIM? (y no pierdas el mes 0)

El **mes 0 (apertura)** puede traer PIA/PIM sin ejecución (así lo documenta tu modelo para el gasto). El ETL lo **conserva** (acepta
meses 0–12) y `validate` avisa si descarta alguna fila por mes inválido. PIA y PIM pueden venir registrados de dos maneras:

| Cómo vienen en el origen | `MEF_BUDGET_MODE_<DATASET>` | Vigente al corte |
|---|---|---|
| **Saldo**: repetido cada mes, o solo en el mes 0 de apertura | `snapshot` | valor del último mes ≤ corte con saldo |
| **Una vez** (PIA) o como **modificaciones** del periodo (PIM) | `sum` | `SUM(mes ≤ corte)` |

Por defecto `gasto = snapshot` (PIM en el mes 0) e `ingreso = sum` (PIA una vez, PIM como modificaciones), como describe tu modelo.
El profile imprime las dos lecturas para PIA y PIM: compáralas con Consulta Amigable; la que coincida es tu modo. El modo queda en
`manifest.json`, así el dashboard siempre usa el mismo que el ETL.

### 3c · `MEF_UBIGEO_CODE`: ¿el MEF usa ubigeo INEI o RENIEC?

El ETL arma `codigo_ubigeo` (6 dígitos) con `DEPARTAMENTO_/PROVINCIA_/DISTRITO_EJECUTORA` y lo cruza con la tabla de ubigeos para traer
latitud/longitud. El MEF puede entregar los códigos como **2-2-2** (`15`,`01`,`01`) o como **4-6** (`1501`, `150101`); ambos se aceptan.
Pero **INEI y RENIEC no coinciden en la mayoría de los distritos**: cruzar con el código equivocado pegaría coordenadas de *otro*
distrito sin dar error. La sección `[3b]` del profile mide qué porcentaje de tus filas existe en la tabla con cada código y sugiere
`MEF_UBIGEO_CODE`; si ninguno pasa del 95 % avisa. Con una tabla distinta o códigos propios del MEF, el cruce puede fallar: se verá como
"ubicaciones sin coordenadas" en `validate` y en el mapa, nunca como coordenadas equivocadas silenciosas.

## Paso 4 · Construye el datamart

```bash
python -m etl.run                       # primera vez: todos los años
python -m etl.run --years 2025 2026     # después: solo lo que cambió (incremental)
python -m etl.run --dataset gasto --only cubes --years 2026   # solo un paso
python -m etl.run --full                # borra todo y reconstruye (obligatorio si cambias MEF_EXEC_MODE o actualizas el proyecto)
```

Qué pasa por dentro, por año:

1. **dims** — una pasada que lee *solo* columnas de código/nombre, hace `DISTINCT` sobre lo crudo y recién entonces normaliza. Une los años
   (Tipo 1: gana el nombre más reciente), enriquece `dim_ubigeo` con coordenadas y deja `dim_funcional` particionada por año.
2. **facts** — otra pasada que lee *solo* códigos + montos; convierte acumulado→flujo al grano nativo, agrega al grano del modelo y escribe
   `fact_*/anio=YYYY/` (reemplaza solo esa partición, de forma atómica).
3. **cubos** — agrega cada fact a los cubos del dashboard.
4. **manifest + vistas** — `manifest.json` (el dashboard lo vigila) y `mef.duckdb` con vistas para SQL ad-hoc (DBeaver, CLI).

Reglas de oro operativas:
- Los años cerrados se construyen **una vez**. Actualiza solo el año en curso con `--years`.
- El warehouse se puede leer **mientras** el ETL corre: cada partición se escribe aparte y se intercambia al final.
- Si no te alcanza la RAM, baja `MEF_MEMORY_LIMIT` y procesa por años; DuckDB usa disco temporal.
- Si bajas una versión nueva del proyecto con otra estructura de tablas, el ETL se niega a mezclar (`--full`).

## Paso 5 · Valida y verifica la conformidad

```bash
python -m etl.validate          # segundos
python -m etl.validate --deep   # además comprueba "una fila por grano del modelo"
python -m scripts.check_spec --strict   # columnas, orden y largos de VARCHAR(n) vs. tu modelo
```

| Control | Qué detecta | Si falla |
|---|---|---|
| `sk única` / `hash sin colisiones` | colisión de claves hash | **FAIL** |
| `fact.sk_* existe en dim_*` (incluye `dim_tiempo`) | hechos huérfanos | **FAIL**: re-correr dims |
| `cube == fact` | cubo mal agregado | **FAIL** |
| `saldo fact == crudo` (PIA/PIM) | nada se pierde ni se duplica al agregar | **FAIL** |
| `fact vs crudo` (ejecución) | que el flujo reconstruya el crudo | WARN en `cumulative` = series que no aparecen en el último mes: revisa `profile` |
| `filas con mes inválido` | filas descartadas por mes fuera de 0–12 | WARN |
| `ubicaciones SIN coordenadas` | distritos sin lat/long y cuánto devengado concentran | WARN: ver 3c |
| `SEC_EJEC identifica a una sola entidad` | un código con nombres distintos en un año | WARN |
| `metas que cambian de atributos entre años` | si SEC_FUNC se reasigna (justifica el año en la clave) | informativo |
| `cuadre PIM ingreso vs gasto` | presupuesto equilibrado | WARN si >1 % |

`check_spec` también verifica que el dato real **no desborde los `VARCHAR(n)`** que declara tu modelo (útil antes de crear tablas en un
motor con esos largos). El cuadre de PIM es un control de coherencia, no una garantía: si en tus datos reales la diferencia es grande y
sistemática, es una pregunta para el MEF sobre qué incluye cada extracto, no un bug del ETL.

## Paso 6 · Dashboard

```bash
python -m dashboard.app                                  # desarrollo
gunicorn dashboard.app:server -w 2 --threads 4 -b 0.0.0.0:8050   # producción (Linux/macOS)
```

(En Windows, `gunicorn` no corre; `waitress-serve --listen=0.0.0.0:8050 dashboard.app:server` debería servir — **no lo probé**.)

- **Qué muestra:** KPIs de gasto (PIM, devengado, avance, girado) e ingreso (PIM, recaudado, avance); del presupuesto al pago (PIA → PIM →
  devengado → girado); avance mensual; gasto por función, producto/proyecto, entidad, departamento y genérica; ingreso por genérica; y un
  **mapa de distritos** (devengado, avance, recaudado, o *recaudado − devengado*) que usa las coordenadas de `dim_ubigeo`.
- **El mapa no necesita internet ni mapa base:** dibuja un punto por distrito (longitud/latitud); ~1,900 distritos delinean el país. Los
  distritos sin coordenadas no se dibujan y la app lo informa con el monto que representan.
- **Departamento = el de la entidad ejecutora**, igual en gasto e ingreso (como tu modelo: `sk_ubigeo_ejec`): así se compara lo que una región
  recauda con lo que ejecuta. El gasto de entidades nacionales se ubica donde está su sede (casi siempre Lima).
- **Se recarga solo** cuando el ETL termina (cambia `manifest.json`). **Rápido por diseño:** cubos en RAM, un cursor de DuckDB por petición,
  caché de consultas y memoización del resultado completo de cada callback por (versión de datos, filtros).
- Cada gunicorn worker carga sus propios cubos (~400 MB en mis pruebas con 12 M de filas de gasto): dimensiona `-w` según tu RAM.
- Prueba de carga: `python -m scripts.load_test --url http://127.0.0.1:8050`.

---

## Cómo se leen las métricas

| Medida | Tipo | Cómo se agrega |
|---|---|---|
| `PIA`, `PIM` | saldo **o** registrado una vez / modificaciones, según `MEF_BUDGET_MODE_*` | `snapshot`: valor del último mes ≤ corte (incluye el mes 0). `sum`: `SUM(mes ≤ corte)` |
| `DEVENGADO` → `GIRADO` (gasto) | **flujo** | `SUM(mes ≤ corte)` |
| `RECAUDADO` (ingreso) | **flujo** (única fase del ingreso) | `SUM(mes ≤ corte)` |

KPIs: **avance de ejecución = devengado / PIM**, **avance de recaudación = recaudado / PIM**, **girado / devengado**. En el ingreso
`GENERICA…ESPECIFICA_DET` es el *clasificador de ingresos* y en el gasto el *clasificador de gastos*: mismo nombre de columna, códigos distintos
→ dos dimensiones separadas. Las medidas `certificado`, `comprometido` y `comprometido_anual` del MEF **no están en tu modelo** y no se cargan.

## Cómo extender (todo sale de `etl/model.py`)

- **Otra medida del MEF** (p. ej. `certificado`): agrégala a `flow` o `stock` del `Dataset`, reconstruye con `--full`… y actualiza
  `docs/modelo_objetivo.md`, porque `check_spec --strict` la marcará `EXTRA` (es justo para lo que sirve).
- **Otra dimensión en un cubo:** añade `(tabla, columna, alias)` al `Cube` y corre `python -m etl.run --only cubes`.
- **Otro cubo:** un `Cube(...)` en `CUBES`, una consulta en `dashboard/data.py` y un gráfico en `dashboard/figures.py`.
- Si `SEC_FUNC` resulta estable entre años y quieres la clave exacta del modelo, quita `"anio"` de `FUNCIONAL.keys` y `by_year=True`.
- Un cubo por pregunta es mejor que un cubo gigante: mantén cada uno bajo unos pocos millones de filas.

## Rendimiento medido

Máquina de pruebas **modesta: 1 núcleo, 3 GB de RAM** (`MEF_MEMORY_LIMIT=2GB`). Datos **sintéticos** de 1 año de gasto: 12.1 M de filas
(428 MB de parquet). Con más núcleos el ETL baja casi linealmente. *El ingreso no se midió con el modelo alineado en este volumen.*

| Etapa (gasto, 1 año) | Tiempo | Notas |
|---|---|---|
| dims (incl. `dim_funcional` a grano de meta) | 10 s | |
| facts | 40 s | convierte acumulado→flujo al grano nativo y agrega al grano del modelo |
| cubos (6) | 57 s | |
| **ETL total** | **118 s** | pico 2.08 GB de RAM (la versión anterior, con más dimensiones y hechos más anchos: 167 s) |
| Tamaño en disco | `fact_gasto` 238 MB · `dim_funcional` 13 MB · cubos ~30 MB | zstd |

| Dashboard (mismo warehouse) | Medida |
|---|---|
| Arranque (cargar cubos a RAM) | ~1.6 s · ~400 MB por proceso |
| Callback completo de gasto, combinación nueva de filtros | 104–173 ms |
| Callback repetido (memoizado) | ~7 ms (gasto) · ~2 ms (mapa) |
| gunicorn 2 workers × 4 hilos, 12 clientes simultáneos | **155 callbacks/s, mediana 47 ms, 0 errores** (vistas ya vistas) · 17 callbacks/s, mediana 451 ms con combinaciones de filtros nuevas |
| RAM (master + 2 workers) | ~890 MB |

**Extrapolación (no medida):** 15 años reales ≈ 15× la primera carga (≈ 30 min en 1 núcleo, mucho menos con más núcleos); después, solo el
año en curso. Las cardinalidades reales (metas, entidades, distritos) cambian el tamaño de `dim_funcional` y de los cubos.

## Qué se verificó y qué no

**Verificado (80 tests automáticos y ejecuciones reales):**
- Las tablas publicadas tienen exactamente las 54 columnas de tu modelo, en su orden, sin sobrantes y sin desbordar ningún `VARCHAR(n)`.
- El mismo dato como acumulado o como flujo produce **exactamente** el mismo datamart; el flujo coincide con un oráculo independiente
  (diferencia de acumulados del crudo); y la conversión al grano nativo da lo correcto donde "sumar primero" no.
- PIA/PIM con el comportamiento que documenta tu modelo (mes 0, PIA una vez, PIM como modificaciones) coincide con el cálculo directo
  sobre el crudo en varios cortes; el mes 0 ya no se pierde.
- Las coordenadas de `dim_ubigeo` coinciden con la tabla (coma decimal incluida); las convenciones de código 2-2-2 y 4-6 producen la misma
  dimensión; cruzar con RENIEC en vez de INEI baja la cobertura y se informa.
- `sk_entidad` es la misma en gasto e ingreso aunque `SEC_EJEC` venga como texto en uno y número en el otro; la meta no se atribuye mal si
  `SEC_FUNC` se reasigna entre años.
- ETL incremental por año == ETL completo; idempotente; los `*-Diario` se ignoran; el filtro de año llega al scan del parquet; origen parquet == origen tabla `.duckdb`.
- Cubos == hechos al centavo; integridad referencial (incluida `dim_tiempo`); sin colisiones de claves.
- Dashboard: layout y callbacks consistentes (también el mapa y sus 4 métricas), KPIs == consulta directa a la fact con y sin filtros,
  lo dibujado en el mapa + lo que no tiene coordenadas == total, recarga automática, renderizado real en Firefox (escritorio y móvil) y carga concurrente con gunicorn.
- Los tests de las decisiones de diseño **fallan** si reintroduzco los errores correspondientes (se comprobó con 5 mutaciones).

**No verificado (necesitas confirmarlo con tus datos):**
- ❗ Si tus montos son acumulados o flujo (3a), cómo vienen PIA/PIM (3b) y si el MEF usa ubigeo INEI o RENIEC (3c). Es lo primero que debes correr.
- Que `SEC_EJEC` identifique a una sola entidad en todo el país y que las 19 ubicaciones sin coordenadas de la tabla no concentren monto relevante.
- Los tiempos y tamaños con **datos reales**, el volumen de **ingreso**, Windows (rutas y `waitress`) y otras versiones de librerías.

## Solución de problemas

| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| `faltan columnas en el origen` | nombres distintos | crea una vista con los nombres del diccionario |
| `exec_mode='x' … corre con --full` | cambiaste `MEF_EXEC_MODE` | `python -m etl.run --full` |
| `estructura de tablas anterior … --full` | actualizaste el proyecto | `python -m etl.run --full` |
| Devengado ~6× mayor que el portal | `exec_mode` equivocado | 3a |
| PIA/PIM del gasto en 0 | modo equivocado o filas del mes 0 ausentes | 3b y `profile` |
| PIM de ingreso muy bajo / ~12× el del portal | `budget_mode` invertido | 3b |
| Totales ×2 en 2025/2026 | mezclaste Mensual y Diario | `MEF_EXCLUDE_FILES_REGEX`, `profile` [2] |
| Códigos "01" aparecen como "1" | el parquet perdió ceros | reexporta esa columna como texto (`profile` [3]) |
| Mapa vacío o con pocos puntos | sin tabla de ubigeos o código equivocado | 3c, `validate` |
| `Out of Memory` | año muy grande | baja `MEF_MEMORY_LIMIT`, un año por corrida |
| "Todavía no hay datos" | no hay warehouse | `python -m etl.run` |

## Estructura

```
config.py               configuración (variables de entorno / .env)
etl/model.py            ★ modelo dimensional declarativo (dims, facts, cubos)
etl/pipeline.py         ETL: dims → facts → cubos → manifest
etl/run.py              CLI del ETL
etl/profile.py          diagnóstico previo (acumulado/flujo, PIA/PIM, ubigeo INEI/RENIEC, ceros perdidos)
etl/validate.py         controles de calidad posteriores
dashboard/data.py       capa de datos (cubos en RAM, consultas, caché, recarga, mapa)
dashboard/figures.py    gráficos Plotly y formato de cifras
dashboard/app.py        layout + callbacks (create_app)
dashboard/assets/       CSS
scripts/check_spec.py   verifica el warehouse contra docs/modelo_objetivo.md
scripts/                datos sintéticos, prueba de carga
data/ref/TB_UBIGEOS.csv tabla de ubigeos (latitud/longitud)
docs/modelo_objetivo.md el modelo de datamart del equipo
docs/diccionarios/      los diccionarios del MEF que usan los tests
tests/                  80 tests
```
